"""The tools own every gate. These tests prove the model cannot talk its way past one."""

from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.agents import tools as tools_module
from dealsieve.agents.tools import (
    ProcessingSession,
    make_tools,
    perform_draft_broker_questions,
    perform_notify_human,
    perform_record_claims,
    perform_skeptic_review,
    perform_underwrite,
)
from dealsieve.evidence.reconcile import DetectedChange, MissingInputs
from dealsieve.schemas import (
    Actor,
    EventType,
    IdentityKeys,
    OpportunityStatus,
    ResolutionResult,
)

from .conftest import fake_threshold_alert, make_run, make_skeptic_report, make_working_values


@pytest.fixture
def session(fake_repo, policy, recording_notifier, inbound_message) -> ProcessingSession:
    return ProcessingSession(
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        message=inbound_message,
        model_backend="scripted",
    )


@pytest.fixture(autouse=True)
def stub_w1_w2_w4(monkeypatch):
    """W1/W2/W4 collaborators, replaced with predictable stand-ins."""
    monkeypatch.setattr(
        tools_module, "extract_identity_keys", lambda message, claims: IdentityKeys(normalized_address="X")
    )
    monkeypatch.setattr(
        tools_module,
        "normalize_address",
        lambda line, city=None, state=None, postal=None: "8330 POWER INN RD SACRAMENTO CA 95826",
    )
    monkeypatch.setattr(
        tools_module,
        "resolve",
        lambda keys, repo: ResolutionResult(
            opportunity_id=None, property_id=None, created=False, confidence=0.0
        ),
    )
    monkeypatch.setattr(
        tools_module, "reconcile", lambda existing, claims: (make_working_values(), [])
    )
    monkeypatch.setattr(tools_module, "format_threshold_alert", fake_threshold_alert)


def record(session, claims):
    return perform_record_claims(session, claims)


def underwrite_as(session, monkeypatch, status, **kwargs):
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id, status, trigger_event_id=trigger_event_id, **kwargs
        ),
    )
    return perform_underwrite(session)


# --------------------------------------------------------------------------- record_claims


def test_record_claims_creates_the_opportunity_and_appends_events_in_order(session, claims, fake_repo):
    result = record(session, claims)

    assert result["created"] is True
    assert result["deal_number"] == 101
    assert session.claims_recorded is True
    assert session.status_before is None, "a brand new opportunity has no prior status"

    assert fake_repo.event_types(session.opportunity_id) == [
        EventType.MESSAGE_RECEIVED.value,
        EventType.DOCUMENT_ADDED.value,
        EventType.DEAL_DISCOVERED.value,
        EventType.CLAIMS_EXTRACTED.value,
    ]
    assert [e.seq for e in fake_repo.list_events(session.opportunity_id)] == [1, 2, 3, 4]
    assert session.events_created == [e.event_id for e in fake_repo.list_events(session.opportunity_id)]
    assert fake_repo.list_evidence(session.opportunity_id), "evidence is persisted with provenance"
    assert fake_repo.documents and fake_repo.documents[0][2] == "Power_Inn_OM.md"
    assert fake_repo.find_opportunity_id_by_message_id(session.message.message_id) == session.opportunity_id


def test_record_claims_on_an_existing_opportunity_captures_the_prior_status(
    session, claims, fake_repo, monkeypatch
):
    record(session, claims)
    existing_id = session.opportunity_id
    opp = fake_repo.get_opportunity(existing_id)
    opp.status = OpportunityStatus.WATCH
    fake_repo.save_opportunity(opp)

    monkeypatch.setattr(
        tools_module,
        "resolve",
        lambda keys, repo: ResolutionResult(
            opportunity_id=existing_id, property_id=None, created=False, confidence=1.0
        ),
    )
    monkeypatch.setattr(
        tools_module,
        "reconcile",
        lambda existing, c: (
            make_working_values(Decimal("1250000")),
            [
                DetectedChange(
                    type=EventType.ASKING_PRICE_CHANGED,
                    summary="Asking price $1,550,000 -> $1,250,000 (-19.4%)",
                    payload={"from": "1550000", "to": "1250000"},
                )
            ],
        ),
    )
    second = ProcessingSession(
        repo=fake_repo,
        policy=session.policy,
        notifier=session.notifier,
        message=session.message.model_copy(update={"message_id": "<reply@example>"}),
        model_backend="scripted",
    )
    result = perform_record_claims(second, claims)

    assert result["created"] is False
    assert second.status_before == OpportunityStatus.WATCH
    assert result["changes"] == ["Asking price $1,550,000 -> $1,250,000 (-19.4%)"]
    assert EventType.ASKING_PRICE_CHANGED.value in fake_repo.event_types(existing_id)
    assert fake_repo.get_opportunity(existing_id).current_asking_price == Decimal("1250000")


def test_record_claims_notes_a_possible_duplicate_when_the_resolver_is_unsure(
    session, claims, fake_repo, monkeypatch
):
    monkeypatch.setattr(
        tools_module,
        "resolve",
        lambda keys, repo: ResolutionResult(
            opportunity_id=None,
            property_id=None,
            created=False,
            confidence=0.62,
            needs_human=True,
            ambiguous_candidates=[{"opportunity_id": "opp_x", "deal_number": 104}],
        ),
    )
    record(session, claims)
    notes = [e for e in fake_repo.list_events(session.opportunity_id) if e.type == EventType.NOTE]
    assert notes and "possible duplicate of #104 (confidence 0.62)" == notes[0].summary


def test_record_claims_reports_missing_inputs_instead_of_guessing(session, claims, fake_repo, monkeypatch):
    def boom(existing, c):
        raise MissingInputs(["gross_scheduled_income"])

    monkeypatch.setattr(tools_module, "reconcile", boom)
    result = record(session, claims)

    assert result["missing"] == ["gross_scheduled_income"]
    assert session.claims_recorded is False
    assert EventType.NOTE.value in fake_repo.event_types(session.opportunity_id)


# --------------------------------------------------------------------------- underwrite


def test_underwrite_stores_an_immutable_run_and_updates_derived_state(
    session, claims, fake_repo, monkeypatch
):
    record(session, claims)
    result = underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert result["status"] == "WATCH"
    assert result["threshold_crossed"] is False
    assert "no human attention is justified" in result["next_step"]

    opp = fake_repo.get_opportunity(session.opportunity_id)
    assert opp.status == OpportunityStatus.WATCH
    assert opp.previous_status == OpportunityStatus.NEW
    assert opp.latest_run_id == result["run_id"]
    assert opp.viability is not None and opp.human_attention_required is False
    assert fake_repo.get_underwriting_run(result["run_id"]).status == OpportunityStatus.WATCH
    assert fake_repo.policy_versions, "the policy version behind the run is recorded"

    types = fake_repo.event_types(session.opportunity_id)
    assert types[-2:] == [EventType.UNDERWRITING_COMPLETED.value, EventType.STATUS_CHANGED.value]


def test_underwrite_links_the_run_to_the_triggering_event(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    last_claims_event = fake_repo.list_events(session.opportunity_id)[-1]
    result = underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)
    assert fake_repo.get_underwriting_run(result["run_id"]).trigger_event_id == last_claims_event.event_id


def test_threshold_crossed_only_on_entry_into_review(session, claims, monkeypatch):
    record(session, claims)

    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)
    assert session.threshold_crossed is False

    session.run_after = None
    result = underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    assert session.threshold_crossed is True
    assert result["threshold_crossed"] is True
    assert "request_skeptic_review" in result["next_step"]

    session.run_after = None
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    assert session.threshold_crossed is False, "staying in REVIEW is not a new crossing"


def test_underwrite_without_claims_returns_an_error(session):
    assert "error" in perform_underwrite(session)


def test_dead_never_asks_for_attention(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    result = underwrite_as(
        session,
        monkeypatch,
        OpportunityStatus.DEAD,
        max_viable=None,
        structural_failures=["largest_tenant_pct_max"],
    )
    assert session.threshold_crossed is False
    assert "Do not notify anyone" in result["next_step"]
    assert fake_repo.get_opportunity(session.opportunity_id).human_attention_required is False


# --------------------------------------------------------------------------- gating


def test_skeptic_review_is_refused_unless_the_status_is_review(session, claims, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)
    result = perform_skeptic_review(session)
    assert "skipped" in result and "WATCH" in result["skipped"]
    assert session.skeptic_report is None


def test_skeptic_review_runs_on_review_and_stores_the_report(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))

    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        skeptic_module,
        "run_skeptic",
        lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id),
    )
    result = perform_skeptic_review(session)

    assert result["verdict"] == "proceed_with_questions"
    assert result["suggested_questions"] == ["How old is the roof?"]
    assert fake_repo.list_skeptic_reports(session.opportunity_id)
    assert EventType.SKEPTIC_REVIEW_COMPLETED.value in fake_repo.event_types(session.opportunity_id)


def test_draft_is_refused_without_a_skeptic_report(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))

    result = perform_draft_broker_questions(session, ["How old is the roof?"])
    assert result == {"skipped": "no skeptic report; call request_skeptic_review first"}
    assert fake_repo.list_drafts() == []


def test_draft_is_refused_when_the_skeptic_report_is_for_an_older_run(session, claims, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    session.skeptic_report = make_skeptic_report(session.opportunity_id, "run_stale")

    result = perform_draft_broker_questions(session, ["How old is the roof?"])
    assert "not for the current underwriting run" in result["skipped"]


def test_draft_creates_a_pending_draft_and_sends_nothing(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    session.skeptic_report = make_skeptic_report(session.opportunity_id, session.run_after.run_id)

    result = perform_draft_broker_questions(session, ["How old is the roof?", "  ", "Phase I?"])

    assert result["sent"] is False and result["status"] == "pending"
    assert result["questions"] == ["How old is the roof?", "Phase I?"]
    assert result["subject"].startswith("Re: ")
    draft = fake_repo.list_drafts(opportunity_id=session.opportunity_id)[0]
    assert draft.status == "pending" and draft.to_email == "broker@brokerage.example"
    assert "How old is the roof?" in draft.body
    assert EventType.BROKER_DRAFT_CREATED.value in fake_repo.event_types(session.opportunity_id)
    assert session.notifier.sent == [], "drafting must not interrupt a human"


def test_notify_human_is_refused_without_a_threshold_crossing(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert perform_notify_human(session, "please look") == {"skipped": "no threshold crossing"}
    assert session.notifier.sent == []
    assert fake_repo.list_notifications() == []
    assert EventType.HUMAN_NOTIFIED.value not in fake_repo.event_types(session.opportunity_id)


def test_notify_human_fires_once_on_a_crossing(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))

    first = perform_notify_human(session, "cap and DSCR now pass")
    assert first["delivered"] is True and first["delivery_ref"] == "recorded-1"
    assert len(session.notifier.sent) == 1
    assert session.notifier.sent[0].kind == "threshold_crossed"
    assert fake_repo.list_notifications()[0].delivered is True
    assert EventType.HUMAN_NOTIFIED.value in fake_repo.event_types(session.opportunity_id)

    second = perform_notify_human(session, "again")
    assert second == {"skipped": "the human has already been notified for this message"}
    assert len(session.notifier.sent) == 1, "exactly one human interruption"


def test_events_can_be_attributed_to_the_system_actor(session, claims, fake_repo, monkeypatch):
    record(session, claims)
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id, OpportunityStatus.WATCH
        ),
    )
    perform_underwrite(session, actor=Actor.SYSTEM)
    completed = [
        e for e in fake_repo.list_events(session.opportunity_id) if e.type == EventType.UNDERWRITING_COMPLETED
    ]
    assert completed[0].actor == Actor.SYSTEM


# --------------------------------------------------------------------------- the tool surface


def test_make_tools_exposes_exactly_the_five_contract_tools(session):
    built = make_tools(session)
    assert [t.tool_name for t in built] == [
        "record_claims",
        "underwrite",
        "request_skeptic_review",
        "draft_broker_questions",
        "notify_human",
    ]


def test_record_claims_declares_a_pydantic_extracted_claims_parameter(session):
    """Strands schemas a Pydantic model parameter, so the model sees the full ExtractedClaims shape."""
    spec = make_tools(session)[0].tool_spec
    schema = spec["inputSchema"]["json"]
    assert schema["required"] == ["claims"]
    assert "$defs" in schema and "Evidence" in schema["$defs"]
    assert "$ref" in schema["properties"]["claims"]


def test_record_claims_re_validates_the_dict_strands_hands_it(session, claims, fake_repo):
    """`validate_input` returns `validated.model_dump()`, so the tool body gets a dict, not a model."""
    result = perform_record_claims(session, claims.model_dump(mode="python"))
    assert result["created"] is True
    assert isinstance(session.claims, type(claims))
    assert session.claims.asking_price == Decimal("1550000")
    assert fake_repo.list_evidence(session.opportunity_id)[0].field == "asking_price"


def test_a_tool_that_blows_up_is_recorded_on_the_session(session, monkeypatch):
    """Strands hides an exception in an error tool result; the summary must not stay optimistic."""

    def boom(message, c):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(tools_module, "extract_identity_keys", boom)
    record_claims = make_tools(session)[0]
    result = record_claims(claims={"asking_price": 1550000})

    assert "the database went away" in result["error"]
    assert session.errors and "record_claims failed" in session.errors[0]


def test_notify_human_tool_refuses_through_the_decorated_surface(session, claims, monkeypatch):
    record(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)
    notify = next(t for t in make_tools(session) if t.tool_name == "notify_human")
    assert notify(note="let me in") == {"skipped": "no threshold crossing"}
