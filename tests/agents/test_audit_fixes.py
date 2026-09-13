"""Regression tests for the state-machine audit findings owned by W22.

Each test is named for the finding it pins (`docs/STATE_MACHINE.md`, section 6) and fails against
the code as the audit found it:

* **F1** `request_diligence` / `request_price_adjustment` were repeatable without limit.
* **F6** a notifier that returned but whose bookkeeping failed interrupted the human twice.
* **F11** a document marked a request `answered` that had never been sent.
* **F13** retrying a failed message duplicated its whole event and run history.
* **F15** two reports wording the same capital work differently double-counted it.
* **F17** the run and its `UNDERWRITING_COMPLETED` event were two transactions.
* **F18** a deal no price can fix landed in `WATCH` with a blank frontier.
* **F19** the archived policy body was re-read from disk at underwrite time.
* **F21** a skeptic report on a deal already in REVIEW chased nothing.
* **F23** `process_inbound` could raise before the session existed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from dealsieve.agents import tools as tools_module
from dealsieve.agents.tools import (
    ProcessingSession,
    aggregate_capex_items,
    perform_analyze_document,
    perform_notify_human,
    perform_record_claims,
    perform_request_diligence,
    perform_request_price_adjustment,
    perform_skeptic_review,
    perform_underwrite,
    resume_undelivered_notifications,
)
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import (
    Actor,
    Attachment,
    CapexItem,
    Channel,
    DiligenceRequest,
    DocumentAnalysis,
    DocumentFinding,
    EventType,
    ExpenseClaims,
    IdentityKeys,
    InboundMessage,
    Notification,
    OpportunityEvent,
    OpportunityStatus,
    RequestAnswer,
    ResolutionResult,
    UnderwritingResult,
    WorkingValues,
)

from .conftest import fake_threshold_alert, make_run, make_skeptic_report, make_working_values

ROOF_REPORT = "Roof_Report.pdf"
SECOND_ROOF_REPORT = "Roof_Report_B.pdf"
ROOF_TEXT = "Roof replacement is $85,000 to $95,000. Membrane work runs $90,000-$100,000."


# --------------------------------------------------------------------------- shared scaffolding


def document_message(message_id: str = "<audit-fixes@brokerage.example>") -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        channel=Channel.EMAIL,
        sender="broker@brokerage.example",
        sender_name="Dana Ruiz",
        subject="Off-market: 8-unit small-bay industrial, Sacramento",
        body_text="Asking $1,550,000, NOI $126,000. Report attached.",
        attachments=[
            Attachment(
                filename=ROOF_REPORT,
                content_type="application/pdf",
                sha256="b" * 64,
                size_bytes=4096,
                text=ROOF_TEXT,
            )
        ],
        thread_id="<audit-fixes@brokerage.example>",
    )


@pytest.fixture
def session(fake_repo, policy, recording_notifier) -> ProcessingSession:
    return ProcessingSession(
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        message=document_message(),
        model_backend="scripted",
    )


@pytest.fixture(autouse=True)
def stub_collaborators(monkeypatch):
    """W1/W2/W4 collaborators, replaced with predictable stand-ins (as the sibling suites do)."""
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    monkeypatch.setattr(
        tools_module, "extract_identity_keys", lambda message, claims: IdentityKeys(normalized_address="X")
    )
    monkeypatch.setattr(
        tools_module, "normalize_address", lambda line, city=None, state=None, postal=None: "NORM"
    )
    monkeypatch.setattr(
        tools_module,
        "resolve",
        lambda keys, repo: ResolutionResult(
            opportunity_id=next(iter(repo.opportunities), None),
            property_id=None,
            created=False,
            confidence=1.0 if repo.opportunities else 0.0,
        ),
    )
    monkeypatch.setattr(tools_module, "reconcile", lambda existing, claims: (make_working_values(), []))
    monkeypatch.setattr(tools_module, "format_threshold_alert", fake_threshold_alert)


def underwrite_as(session, monkeypatch, status, **kwargs):
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id, status, trigger_event_id=trigger_event_id, **kwargs
        ),
    )
    return perform_underwrite(session)


def in_review(session, claims, monkeypatch):
    perform_record_claims(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))


def capex(item: str, low: str, high: str, *, source: str) -> CapexItem:
    return CapexItem(
        item=item,
        low=Decimal(low),
        high=Decimal(high),
        urgency="immediate",
        source_document=source,
        location="page 3",
    )


def analysis_with(
    session: ProcessingSession,
    *,
    filename: str = ROOF_REPORT,
    items: list[CapexItem] | None = None,
    answers: list[RequestAnswer] | None = None,
) -> DocumentAnalysis:
    return DocumentAnalysis(
        opportunity_id=session.opportunity_id,
        message_id=session.message.message_id,
        filename=filename,
        document_type="roof_report",
        summary=f"Reading of {filename}.",
        findings=[DocumentFinding(topic="Roof age", value="Original 2001 membrane", confidence=0.9, page=3)],
        answers=list(answers or []),
        capex_items=list(items or []),
        images_reviewed=0,
    )


def install_inspector(monkeypatch, build) -> None:
    import dealsieve.agents.inspector as inspector_module

    monkeypatch.setattr(inspector_module, "run_inspector", build)


def install_agent(monkeypatch, behaviour) -> None:
    class FakeAgent:
        def __init__(self, session: ProcessingSession) -> None:
            self.session = session

        def __call__(self, prompt: str):
            behaviour(self.session)
            return type("R", (), {"message": {"content": [{"text": "one-line summary"}]}})()

    monkeypatch.setattr("dealsieve.pipeline.build_acquisition_agent", lambda session: FakeAgent(session))


class FlakyNotifier:
    """Raises on the first `failures` deliveries, succeeds afterwards."""

    name = "flaky"
    channel = Channel.MANUAL

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.attempts = 0
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> str | None:
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError("telegram is unreachable")
        self.sent.append(notification)
        return f"tg-{len(self.sent)}"


# ======================================================================= F1: one chase per message


def test_a_second_request_diligence_is_refused_and_writes_nothing(
    session, claims, fake_repo, monkeypatch
):
    """F1: the tool called `phase_check` but never `phase_advance`, so it repeated forever."""
    in_review(session, claims, monkeypatch)
    session.skeptic_report = make_skeptic_report(session.opportunity_id, session.run_after.run_id)
    items = [{"topic": "Roof age", "question": "How old is the roof?"}]

    first = perform_request_diligence(session, items)
    assert first["request_ids"], first

    before_requests = dict(fake_repo.diligence_requests)
    before_drafts = dict(fake_repo.drafts)
    before_events = len(fake_repo.list_events(session.opportunity_id))

    second = perform_request_diligence(session, items)

    assert "skipped" in second and "already ran" in second["skipped"]
    assert fake_repo.diligence_requests == before_requests, "no second copy of the same ask"
    assert fake_repo.drafts == before_drafts, "no second approval button"
    assert len(fake_repo.list_events(session.opportunity_id)) == before_events


def test_a_second_request_price_adjustment_is_refused_and_writes_nothing(
    session, claims, fake_repo, monkeypatch
):
    in_review(session, claims, monkeypatch)
    session.threshold_lost = True

    first = perform_request_price_adjustment(session, 250000, "capex")
    assert first["draft_id"], first

    before_drafts = dict(fake_repo.drafts)
    before_events = len(fake_repo.list_events(session.opportunity_id))

    second = perform_request_price_adjustment(session, 250000, "capex")

    assert "skipped" in second and "already ran" in second["skipped"]
    assert fake_repo.drafts == before_drafts, "money is never drafted twice for one message"
    assert len(fake_repo.list_events(session.opportunity_id)) == before_events


# ============================================== F6: delivered, but the bookkeeping fell over


class BookkeepingFailsOnce:
    """A repo whose `update_notification` raises exactly once, after the notifier returned."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.failures = 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def update_notification(self, notification: Notification) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("sqlite is locked")
        self._inner.update_notification(notification)


def test_a_delivered_alert_whose_record_fails_is_not_sent_again(
    fake_repo, policy, claims, monkeypatch, recording_notifier
):
    """F6: send succeeded, persist failed -- the row said "we tried" and the resume step re-sent."""
    repo = BookkeepingFailsOnce(fake_repo)
    session = ProcessingSession(
        repo=repo,
        policy=policy,
        notifier=recording_notifier,
        message=document_message(),
        model_backend="scripted",
    )
    in_review(session, claims, monkeypatch)

    result = perform_notify_human(session, "crossed")

    assert result["delivered"] is True and result["recorded"] is False
    assert len(recording_notifier.sent) == 1
    assert session.notification is not None
    assert session.notification.delivered is True, "the in-memory alert knows it went out"
    assert session.notification.delivery_ref == "recorded-1"
    assert session.undelivered_notification is None
    assert "delivered but not recorded" in session.notification_error

    notes = [e for e in fake_repo.list_events(session.opportunity_id) if e.type == EventType.NOTE]
    assert any("delivered but not recorded" in e.summary for e in notes)

    # The stored row now carries the delivery_ref but not the flag: the resume step must skip it.
    stored = fake_repo.list_notifications(session.opportunity_id)[0]
    stored.delivery_ref = "recorded-1"
    assert resume_undelivered_notifications(session) == {"resumed": 0, "notification_ids": []}
    assert len(recording_notifier.sent) == 1, "one crossing, one interruption"


def test_the_message_is_marked_failed_when_the_alert_could_not_be_recorded(
    fake_repo, policy, claims, monkeypatch
):
    """The row disagrees with reality, so the message stays failed -- with that as the reason."""
    import dealsieve.agents.skeptic as skeptic_module

    repo = BookkeepingFailsOnce(fake_repo)
    notifier = FlakyNotifier(failures=0)
    failures: list[str] = []
    monkeypatch.setattr(
        fake_repo, "mark_message_failed", lambda message_id, error: failures.append(error)
    )
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id,
            OpportunityStatus.REVIEW,
            cap=Decimal("0.083"),
            dscr=Decimal("1.43"),
            trigger_event_id=trigger_event_id,
        ),
    )
    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))
    message = document_message("<bookkeeping@brokerage.example>")

    outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier, script=None)

    assert outcome.notified_human is True, "the human really was interrupted"
    assert len(notifier.sent) == 1, "and only once"
    assert failures and "delivered but not recorded" in failures[-1]
    notified = [
        e for e in fake_repo.list_events(outcome.opportunity_id) if e.type == EventType.HUMAN_NOTIFIED
    ]
    assert len(notified) == 1 and notified[0].payload["recorded"] is False


# ======================================== F11: a document cannot answer a question nobody asked


def test_a_document_does_not_answer_a_request_that_was_never_sent(
    session, claims, fake_repo, monkeypatch
):
    """F11: `draft -> answered` claimed the broker replied to a question still awaiting approval."""
    perform_record_claims(session, claims)
    unsent = DiligenceRequest(
        opportunity_id=session.opportunity_id,
        topic="Roof age",
        question="How old is the roof?",
        status="draft",
    )
    fake_repo.store_diligence_request(unsent)

    install_inspector(
        monkeypatch,
        lambda s, attached, open_requests: analysis_with(
            s,
            answers=[
                RequestAnswer(
                    request_topic="Roof age", answer="Original 2001 membrane.", resolves=True
                )
            ],
        ),
    )

    result = perform_analyze_document(session, ROOF_REPORT)

    stored = fake_repo.get_diligence_request(unsent.request_id)
    assert stored.status == "draft", "an unsent request is not answered by anybody"
    assert stored.answered_at is None and stored.answered_by_document is None
    assert result["answered_topics"] == []
    assert result["answered_before_asked"] == ["Roof age"]
    assert session.answered_before_asked == ["Roof age"]
    assert "Roof age" not in result["still_open_topics"], "the approve step drops it, not chases it"

    notes = [e for e in fake_repo.list_events(session.opportunity_id) if e.type == EventType.NOTE]
    assert [e.summary for e in notes] == ["answered before the request was sent: Roof age"]
    assert notes[0].payload["request_id"] == unsent.request_id
    assert notes[0].payload["answer"] == "Original 2001 membrane."
    analysis = next(iter(fake_repo.document_analyses.values()))
    assert analysis.answers[0].answer == "Original 2001 membrane.", "the answer is on the record"


def test_a_sent_request_is_still_answered_by_a_document(session, claims, fake_repo, monkeypatch):
    perform_record_claims(session, claims)
    sent = DiligenceRequest(
        opportunity_id=session.opportunity_id,
        topic="Roof age",
        question="How old is the roof?",
        status="sent",
    )
    fake_repo.store_diligence_request(sent)
    install_inspector(
        monkeypatch,
        lambda s, attached, open_requests: analysis_with(
            s,
            answers=[
                RequestAnswer(
                    request_topic="Roof age", answer="Original 2001 membrane.", resolves=True
                )
            ],
        ),
    )

    result = perform_analyze_document(session, ROOF_REPORT)

    assert fake_repo.get_diligence_request(sent.request_id).status == "answered"
    assert result["answered_topics"] == ["Roof age"]
    assert result["answered_before_asked"] == []
    assert session.answered_before_asked == []


# ================================================= F13: a retry resumes, it does not re-record


def test_retrying_a_failed_message_adds_no_second_run_claims_or_alert(
    fake_repo, policy, claims, monkeypatch
):
    """F13: the retry used to append a second MESSAGE_RECEIVED/CLAIMS_EXTRACTED and a second run."""
    notifier = FlakyNotifier(failures=1)
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id,
            OpportunityStatus.REVIEW,
            cap=Decimal("0.083"),
            dscr=Decimal("1.43"),
            trigger_event_id=trigger_event_id,
        ),
    )
    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))
    message = document_message("<retry-me@brokerage.example>")

    first = process_inbound(message, repo=fake_repo, policy=policy, notifier=notifier, script=None)
    assert first.notified_human is False
    assert fake_repo.message_statuses[message.message_id] == "failed"

    second = process_inbound(message, repo=fake_repo, policy=policy, notifier=notifier, script=None)

    opportunity_id = first.opportunity_id
    assert second.opportunity_id == opportunity_id
    assert "duplicate" not in second.summary.lower(), "a failed message is retried, not skipped"

    types = fake_repo.event_types(opportunity_id)
    assert types.count(EventType.MESSAGE_RECEIVED.value) == 1
    assert types.count(EventType.CLAIMS_EXTRACTED.value) == 1
    assert types.count(EventType.DOCUMENT_ADDED.value) == 1
    assert types.count(EventType.UNDERWRITING_COMPLETED.value) == 1
    assert len(fake_repo.list_underwriting_runs(opportunity_id)) == 1, "one message, one run"
    assert second.run_id == first.run_id
    assert len(fake_repo.documents) == 1, "the attachment is stored once"
    assert len(fake_repo.list_evidence(opportunity_id)) == 1

    delivered = fake_repo.list_notifications(opportunity_id)
    assert len(delivered) == 1 and delivered[0].delivered is True
    assert len(notifier.sent) == 1, "the human is interrupted exactly once across both runs"
    assert fake_repo.message_statuses[message.message_id] == "completed"
    assert len(fake_repo.list_skeptic_reports(opportunity_id)) == 1
    assert len(fake_repo.list_drafts(opportunity_id=opportunity_id)) == 1, "one ask, not two"


def test_a_retry_reuses_the_skeptic_report_and_asks_the_broker_nothing_new(
    session, claims, fake_repo, monkeypatch
):
    """F13: the retry must not pay for a second skeptic call, a second ask or a second credit draft."""
    import dealsieve.agents.skeptic as skeptic_module

    calls: list[str] = []

    def run_skeptic(s: ProcessingSession):
        calls.append(s.message.message_id)
        return make_skeptic_report(s.opportunity_id, s.run_after.run_id)

    monkeypatch.setattr(skeptic_module, "run_skeptic", run_skeptic)

    in_review(session, claims, monkeypatch)
    perform_skeptic_review(session)
    perform_request_diligence(session, [{"topic": "Roof age", "question": "How old is the roof?"}])
    session.threshold_lost = True
    perform_request_price_adjustment(session, 250000, "capex")
    assert len(calls) == 1

    requests_before = dict(fake_repo.diligence_requests)
    drafts_before = dict(fake_repo.drafts)

    retry = ProcessingSession(
        repo=fake_repo,
        policy=session.policy,
        notifier=session.notifier,
        message=session.message,
        model_backend="scripted",
    )
    perform_record_claims(retry, claims)
    assert retry.replayed_message is True
    underwrite_as(retry, monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    assert retry.underwrite_result["reused_run"] is True

    reused = perform_skeptic_review(retry)
    assert reused["reused"] is True and calls == [session.message.message_id], "no second model call"
    assert retry.skeptic_report is not None

    chased = perform_request_diligence(retry, [{"topic": "Roof age", "question": "How old is the roof?"}])
    assert "already raised its diligence requests" in chased["skipped"]

    retry.threshold_lost = True
    credit = perform_request_price_adjustment(retry, 250000, "capex")
    assert "already produced a credit request" in credit["skipped"]

    assert fake_repo.diligence_requests == requests_before
    assert fake_repo.drafts == drafts_before
    assert len(fake_repo.list_skeptic_reports(retry.opportunity_id)) == 1


def test_a_retry_that_changes_the_working_values_does_underwrite_again(
    fake_repo, policy, claims, monkeypatch
):
    """The reuse is keyed on the inputs, not on the retry: real movement still produces a run."""
    session = ProcessingSession(
        repo=fake_repo,
        policy=policy,
        notifier=FlakyNotifier(failures=0),
        message=document_message("<moves@brokerage.example>"),
        model_backend="scripted",
    )
    perform_record_claims(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    retry = ProcessingSession(
        repo=fake_repo,
        policy=policy,
        notifier=FlakyNotifier(failures=0),
        message=document_message("<moves@brokerage.example>"),
        model_backend="scripted",
    )
    perform_record_claims(retry, claims)
    assert retry.replayed_message is True

    opp = fake_repo.get_opportunity(retry.opportunity_id)
    opp.working_values.immediate_capex = Decimal("90000")
    fake_repo.save_opportunity(opp)

    underwrite_as(retry, monkeypatch, OpportunityStatus.NEAR)

    assert len(fake_repo.list_underwriting_runs(retry.opportunity_id)) == 2
    assert retry.underwrite_result.get("reused_run") is None


# ============================================ F15: one roof, priced twice, is still one roof


def test_two_reports_wording_the_same_roof_differently_do_not_double_count():
    """F15: "Roof replacement" and "Roof membrane replacement" are one roof, not $180k of work."""
    first = DocumentAnalysis(
        opportunity_id="opp_1",
        message_id="<m1>",
        filename=ROOF_REPORT,
        document_type="roof_report",
        summary="roof",
        capex_items=[capex("Roof replacement", "85000", "95000", source=ROOF_REPORT)],
    )
    second = DocumentAnalysis(
        opportunity_id="opp_1",
        message_id="<m2>",
        filename=SECOND_ROOF_REPORT,
        document_type="roof_report",
        summary="roof again",
        capex_items=[capex("Roof membrane replacement", "90000", "100000", source=SECOND_ROOF_REPORT)],
    )

    items, conflicts = aggregate_capex_items([first, second])

    assert len(items) == 1, "the same work is not paid for twice under two names"
    assert items[0].item == "Roof membrane replacement", "the newer estimate wins"
    assert items[0].source_document == SECOND_ROOF_REPORT
    assert len(conflicts) == 1
    assert "price the same work" in conflicts[0]
    assert "$85,000" in conflicts[0] and "$90,000" in conflicts[0]
    assert "not added" in conflicts[0]


def test_different_families_still_add_up():
    roof = DocumentAnalysis(
        opportunity_id="opp_1",
        message_id="<m1>",
        filename=ROOF_REPORT,
        document_type="roof_report",
        summary="roof",
        capex_items=[capex("Roof replacement", "85000", "95000", source=ROOF_REPORT)],
    )
    other = DocumentAnalysis(
        opportunity_id="opp_1",
        message_id="<m2>",
        filename="HVAC.pdf",
        document_type="inspection_report",
        summary="hvac and paving",
        capex_items=[
            capex("HVAC replacement, suites 103 & 106", "28000", "32000", source="HVAC.pdf"),
            capex("Parking lot repaving", "40000", "44000", source="HVAC.pdf"),
        ],
    )

    items, conflicts = aggregate_capex_items([roof, other])

    assert len(items) == 3 and conflicts == []


def test_one_report_itemising_two_lines_of_one_family_is_not_a_conflict():
    """Two roof lines in a single inspector's report is itemisation, not disagreement."""
    single = DocumentAnalysis(
        opportunity_id="opp_1",
        message_id="<m1>",
        filename=ROOF_REPORT,
        document_type="roof_report",
        summary="roof",
        capex_items=[
            capex("Roof membrane replacement", "85000", "95000", source=ROOF_REPORT),
            capex("Roof drains and flashing", "6000", "8000", source=ROOF_REPORT),
        ],
    )

    items, conflicts = aggregate_capex_items([single])

    assert len(items) == 2 and conflicts == []


# ================================================== F17: the run and its event, one transaction


def test_the_run_and_its_event_go_through_record_underwriting_when_the_repo_has_it(
    session, claims, fake_repo, monkeypatch
):
    """F17: a crash between two commits left a run with no event; one call closes the window."""
    calls: list[tuple[UnderwritingResult, OpportunityEvent]] = []

    def record_underwriting(run: UnderwritingResult, event: OpportunityEvent) -> OpportunityEvent:
        calls.append((run, event))
        fake_repo.store_underwriting_run(run)
        return fake_repo.append_event(event)

    monkeypatch.setattr(fake_repo, "record_underwriting", record_underwriting, raising=False)

    perform_record_claims(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert len(calls) == 1, "the two-step path was not used"
    run, event = calls[0]
    assert event.type == EventType.UNDERWRITING_COMPLETED
    assert event.payload["run_id"] == run.run_id
    completed = [
        e for e in fake_repo.list_events(session.opportunity_id) if e.type == EventType.UNDERWRITING_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0].event_id in session.events_created, "the session booked the stored event"
    assert EventType.UNDERWRITING_COMPLETED in session.event_types_created


def test_a_repo_without_record_underwriting_keeps_the_two_step_path(
    session, claims, fake_repo, monkeypatch
):
    assert not hasattr(fake_repo, "record_underwriting")

    perform_record_claims(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert len(fake_repo.list_underwriting_runs(session.opportunity_id)) == 1
    assert EventType.UNDERWRITING_COMPLETED.value in fake_repo.event_types(session.opportunity_id)


# ======================================================== F18: no price fixes this deal


def no_price_values() -> WorkingValues:
    """Fixed costs alone exceed the income: NOI is negative at every purchase price."""
    return WorkingValues(
        asking_price=Decimal("1550000"),
        gross_scheduled_income=Decimal("12000"),
        other_income=Decimal("0"),
        stated_vacancy_pct=Decimal("0"),
        stated_expenses=ExpenseClaims(total=Decimal("40000")),
        stated_noi=Decimal("-28000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
    )


def test_no_viable_price_is_watch_with_an_explicit_reason_and_no_credit_request(
    fake_repo, policy, claims, recording_notifier, monkeypatch
):
    """F18: `WATCH` with a blank frontier silently disabled `request_price_adjustment`."""
    from dealsieve.underwriting import run_underwriting

    monkeypatch.setattr(tools_module, "reconcile", lambda existing, c: (no_price_values(), []))
    session = ProcessingSession(
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        message=document_message("<no-price@brokerage.example>"),
        model_backend="scripted",
    )
    monkeypatch.setattr(tools_module, "run_underwriting", run_underwriting)

    perform_record_claims(session, claims)
    result = perform_underwrite(session)

    assert result["status"] == OpportunityStatus.WATCH.value
    assert result["failure_summary"].startswith("No purchase price passes the economic gates (NOI ")
    run = session.run_after
    assert run.viability.no_viable_price is True
    assert run.viability.max_viable_price is None and run.viability.distance_pct is None
    assert run.viability.paths == [] and run.viability.structural_failures == []

    session.threshold_lost = True  # the most permissive eligibility there is
    skipped = perform_request_price_adjustment(session, None, "capex")

    assert "no purchase price fixes this deal" in skipped["skipped"]
    assert "No purchase price passes the economic gates" in skipped["skipped"]
    assert fake_repo.drafts == {}, "nothing is drafted for a deal no credit can save"


# ================================================== F19: the archived policy is the loaded one


def test_the_archived_policy_body_is_the_text_the_version_hashes(
    session, claims, fake_repo, monkeypatch, policy
):
    """F19: re-reading `source_path` archived whatever the file said at underwrite time."""
    from dealsieve.policy.loader import policy_version as policy_version_of

    def explode(*args: Any, **kwargs: Any) -> str:  # pragma: no cover - must never be reached
        raise AssertionError("the policy file must not be read at underwrite time")

    monkeypatch.setattr("pathlib.Path.read_text", explode)

    perform_record_claims(session, claims)
    underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert len(fake_repo.policy_versions) == 1
    version, name, raw_yaml = fake_repo.policy_versions[0]
    assert version == policy.policy_version and name == policy.name
    assert raw_yaml == policy.raw_yaml and raw_yaml
    assert policy_version_of(raw_yaml, policy.version) == version


def test_a_policy_body_that_does_not_hash_to_its_version_is_never_archived(
    session, claims, fake_repo, monkeypatch, policy
):
    tampered = policy.model_copy(update={"raw_yaml": policy.raw_yaml + "\n# edited after load\n"})
    session.policy = tampered

    perform_record_claims(session, claims)
    result = underwrite_as(session, monkeypatch, OpportunityStatus.WATCH)

    assert result["run_id"], "the run itself still lands"
    assert fake_repo.policy_versions == [], "an unverifiable policy body is not archived"
    assert any("was not archived" in error for error in session.errors)


# =========================================== F21: a skeptic report always owes the broker a chase


def test_a_skeptic_report_without_a_crossing_still_raises_the_diligence(
    fake_repo, policy, claims, recording_notifier, monkeypatch
):
    """F21: step 3 was guarded on `threshold_crossed`, so a report on a REVIEW deal chased nothing."""
    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id,
            OpportunityStatus.REVIEW,
            cap=Decimal("0.083"),
            dscr=Decimal("1.43"),
            trigger_event_id=trigger_event_id,
        ),
    )
    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )

    # First message: the deal crosses into REVIEW and is chased as usual.
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))
    first = process_inbound(
        document_message("<crossing@brokerage.example>"),
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        script=None,
    )
    assert first.status_after == OpportunityStatus.REVIEW
    chased_first = len(fake_repo.list_diligence_requests(first.opportunity_id))
    assert chased_first

    # Second message on the same deal: still REVIEW, so no crossing -- but the model asks for a
    # skeptic review, and its concerns are just as chaseable.
    def record_and_review(s: ProcessingSession) -> None:
        perform_record_claims(s, claims)
        perform_underwrite(s)
        perform_skeptic_review(s)

    install_agent(monkeypatch, record_and_review)
    second = process_inbound(
        document_message("<already-review@brokerage.example>"),
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        script=None,
    )

    assert second.opportunity_id == first.opportunity_id
    assert second.status_after == OpportunityStatus.REVIEW
    assert second.skeptic_report_id and second.draft_id, "the safety net raised the chase"
    assert "diligence request(s) raised by safety net" in second.summary
    raised = [
        e
        for e in fake_repo.list_events(second.opportunity_id)
        if e.type == EventType.DILIGENCE_REQUESTED
        and e.source_message_id == "<already-review@brokerage.example>"
    ]
    assert raised and raised[0].actor == Actor.SYSTEM


# ============================================================ F23: process_inbound never raises


def test_a_claim_that_raises_returns_an_outcome_and_marks_the_message_failed(
    fake_repo, policy, recording_notifier, monkeypatch
):
    """F23: the claim sat outside the try, so a locked database raised out of `process_inbound`."""
    message = document_message("<claim-explodes@brokerage.example>")
    fake_repo.store_inbound_message(message)

    def explode(_message: InboundMessage) -> str:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(fake_repo, "claim_message", explode)

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )

    assert outcome.message_id == message.message_id
    assert outcome.opportunity_id is None and outcome.notified_human is False
    assert "database is locked" in outcome.summary
    assert fake_repo.message_statuses[message.message_id] == "failed", "retryable, not stranded"


def test_an_unusable_outbox_returns_an_outcome_instead_of_raising(
    fake_repo, policy, recording_notifier, monkeypatch
):
    from dealsieve import pipeline as pipeline_module

    def explode(outbox):
        raise RuntimeError("DEALSIEVE_OUTBOX=nonsense")

    monkeypatch.setattr(pipeline_module, "_resolve_outbox", explode)
    message = document_message("<bad-outbox@brokerage.example>")

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )

    assert "DEALSIEVE_OUTBOX=nonsense" in outcome.summary
    assert fake_repo.message_statuses[message.message_id] == "failed"


def test_a_backend_that_raises_is_reported_not_propagated(
    fake_repo, policy, recording_notifier, monkeypatch
):
    from dealsieve import pipeline as pipeline_module

    def explode() -> str:
        raise RuntimeError("no model backend configured")

    monkeypatch.setattr(pipeline_module, "backend_name", explode)
    message = document_message("<no-backend@brokerage.example>")

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )

    assert outcome.model_backend == "unknown"
    assert "no model backend configured" in outcome.summary
    assert message.message_id not in fake_repo.messages, "nothing was claimed, nothing to mark"


# ---------------------------------------------------------------- F14 call site (tri-state claim)


def test_an_in_flight_message_is_not_reported_as_a_duplicate(
    fake_repo, policy, recording_notifier, monkeypatch
):
    """W21's `claim_message` returns a tri-state; "another worker has it" is not "already done"."""
    message = document_message("<in-flight@brokerage.example>")
    monkeypatch.setattr(fake_repo, "claim_message", lambda _m: "in_flight", raising=False)

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )

    assert "being processed by another worker" in outcome.summary
    assert "duplicate" not in outcome.summary.lower()
    assert outcome.run_id is None


def test_a_completed_message_is_still_reported_as_a_duplicate(
    fake_repo, policy, recording_notifier, monkeypatch
):
    message = document_message("<done@brokerage.example>")
    monkeypatch.setattr(fake_repo, "claim_message", lambda _m: "completed", raising=False)

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )

    assert "duplicate" in outcome.summary.lower()


def test_a_repo_that_still_returns_a_bool_keeps_working(
    fake_repo, policy, recording_notifier, monkeypatch
):
    """The bool path must survive: `False` can only mean "completed"."""
    message = document_message("<bool-repo@brokerage.example>")
    monkeypatch.setattr(fake_repo, "claim_message", lambda _m: False, raising=False)

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )
    assert "duplicate" in outcome.summary.lower()

    monkeypatch.setattr(fake_repo, "claim_message", lambda _m: True, raising=False)
    install_agent(monkeypatch, lambda s: None)
    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=None
    )
    assert "duplicate" not in outcome.summary.lower()
