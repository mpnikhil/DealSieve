"""The real Strands agent loop, driven by ScriptedModel, over the real tools.

Unlike `test_tools.py` these go through `strands.Agent`: tool-spec generation, input validation,
tool execution, the skeptic's `structured_output_model=SkepticOutput` round trip. Only W1's engine,
W2's identity/reconcile and W4's alert formatter are stubbed, because those are still being built.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.agents import tools as tools_module
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import (
    Channel,
    EventType,
    IdentityKeys,
    InboundMessage,
    OpportunityStatus,
    ResolutionResult,
)

from .conftest import fake_threshold_alert, make_run, make_working_values

SCRIPT_01 = "fixtures/scripted/01_initial_offer.json"
SCRIPT_02 = "fixtures/scripted/02_price_drop.json"


@pytest.fixture(autouse=True)
def scripted_env(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    monkeypatch.delenv("DEALSIEVE_SCRIPT", raising=False)


@pytest.fixture
def stub_world(monkeypatch):
    """Stub W1/W2/W4 and let the caller choose the underwriting outcome."""

    monkeypatch.setattr(
        tools_module, "extract_identity_keys", lambda message, claims: IdentityKeys(normalized_address="X")
    )
    monkeypatch.setattr(
        tools_module, "normalize_address", lambda line, city=None, state=None, postal=None: "NORM"
    )
    monkeypatch.setattr(tools_module, "format_threshold_alert", fake_threshold_alert)

    def configure(
        *, status: OpportunityStatus, price: Decimal, existing_id: str | None = None, **run_kwargs
    ) -> None:
        monkeypatch.setattr(
            tools_module,
            "resolve",
            lambda keys, repo: ResolutionResult(
                opportunity_id=existing_id,
                property_id=None,
                created=False,
                confidence=1.0 if existing_id else 0.0,
            ),
        )
        monkeypatch.setattr(
            tools_module, "reconcile", lambda existing, claims: (make_working_values(price), [])
        )
        monkeypatch.setattr(
            tools_module,
            "run_underwriting",
            lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
                opportunity_id, status, price=price, trigger_event_id=trigger_event_id, **run_kwargs
            ),
        )

    return configure


def email(message_id: str, subject: str, body: str) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        channel=Channel.EMAIL,
        sender="broker@brokerage.example",
        sender_name="Dana Ruiz",
        subject=subject,
        body_text=body,
        thread_id="<om-2026-0912-power-inn@brokerage.example>",
    )


def test_script_01_runs_the_quiet_path_through_the_real_agent(
    stub_world, fake_repo, policy, recording_notifier
):
    stub_world(status=OpportunityStatus.WATCH, price=Decimal("1550000"))
    message = email("<om-2026-0912-power-inn@brokerage.example>", "Off-market: 8-unit", "Asking $1.55M.")

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=SCRIPT_01
    )

    assert outcome.created_opportunity is True
    assert outcome.status_after == OpportunityStatus.WATCH, outcome.summary
    assert outcome.notified_human is False and recording_notifier.sent == []
    assert "safety net" not in outcome.summary, "the scripted agent did every step itself"
    assert outcome.summary.startswith("Deal recorded and underwritten: WATCH")

    assert fake_repo.event_types(outcome.opportunity_id) == [
        EventType.MESSAGE_RECEIVED.value,
        EventType.DEAL_DISCOVERED.value,
        EventType.CLAIMS_EXTRACTED.value,
        EventType.UNDERWRITING_COMPLETED.value,
        EventType.STATUS_CHANGED.value,
    ]
    assert len(fake_repo.list_evidence(outcome.opportunity_id)) >= 1, (
        "the golden claims fixture carries provenance and it must survive the tool boundary"
    )


def test_script_02_crosses_into_review_and_interrupts_exactly_once(
    stub_world, fake_repo, policy, recording_notifier
):
    stub_world(
        status=OpportunityStatus.REVIEW,
        price=Decimal("1250000"),
        cap=Decimal("0.0827"),
        dscr=Decimal("1.43"),
    )
    message = email("<price-drop@brokerage.example>", "Re: Off-market", "Seller reduced this to $1.25M.")

    outcome = process_inbound(
        message, repo=fake_repo, policy=policy, notifier=recording_notifier, script=SCRIPT_02
    )

    assert outcome.status_after == OpportunityStatus.REVIEW, outcome.summary
    assert outcome.notified_human is True and len(recording_notifier.sent) == 1
    assert outcome.skeptic_report_id and outcome.draft_id
    assert "safety net" not in outcome.summary

    types = fake_repo.event_types(outcome.opportunity_id)
    assert types[-3:] == [
        EventType.SKEPTIC_REVIEW_COMPLETED.value,
        EventType.BROKER_DRAFT_CREATED.value,
        EventType.HUMAN_NOTIFIED.value,
    ]

    # the skeptic agent's structured output really round-tripped through Strands
    report = fake_repo.list_skeptic_reports(outcome.opportunity_id)[0]
    assert report.verdict == "proceed_with_questions"
    missing = {c.topic.lower() for c in report.concerns if c.evidence_status == "missing"}
    assert {"roof age", "phase i environmental", "cam reconciliation"} <= missing
    assert report.run_id == outcome.run_id

    draft = fake_repo.list_drafts(opportunity_id=outcome.opportunity_id)[0]
    assert draft.status == "pending" and len(draft.questions) == 3
    assert draft.subject == "Re: Re: Off-market"


def test_a_second_message_on_the_same_opportunity_does_not_recreate_it(
    stub_world, fake_repo, policy, recording_notifier
):
    stub_world(status=OpportunityStatus.WATCH, price=Decimal("1550000"))
    first = process_inbound(
        email("<first@x>", "Off-market", "Asking $1.55M."),
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        script=SCRIPT_01,
    )

    stub_world(
        status=OpportunityStatus.REVIEW,
        price=Decimal("1250000"),
        existing_id=first.opportunity_id,
        cap=Decimal("0.0827"),
        dscr=Decimal("1.43"),
    )
    second = process_inbound(
        email("<second@x>", "Re: Off-market", "Seller reduced this to $1.25M."),
        repo=fake_repo,
        policy=policy,
        notifier=recording_notifier,
        script=SCRIPT_02,
    )

    assert second.opportunity_id == first.opportunity_id
    assert second.created_opportunity is False
    assert second.status_before == OpportunityStatus.WATCH
    assert second.status_after == OpportunityStatus.REVIEW
    assert len(recording_notifier.sent) == 1, "only the crossing interrupts a human"

    opp = fake_repo.get_opportunity(first.opportunity_id)
    assert opp.previous_status == OpportunityStatus.WATCH
    assert opp.current_asking_price == Decimal("1250000")
    assert len(fake_repo.list_underwriting_runs(first.opportunity_id)) == 2
    assert fake_repo.get_underwriting_run(first.run_id).status == OpportunityStatus.WATCH, (
        "stored runs are immutable"
    )
