"""The pipeline: dedupe, exception containment and the SYSTEM-actor safety net.

The demo must not depend on a model remembering a step, so these tests deliberately use agents that
forget things, and agents that explode.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from dealsieve.agents import tools as tools_module
from dealsieve.agents.tools import ProcessingSession, perform_record_claims, perform_underwrite
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import Actor, EventType, IdentityKeys, OpportunityStatus, ResolutionResult

from .conftest import fake_threshold_alert, make_run, make_skeptic_report, make_working_values


@pytest.fixture(autouse=True)
def scripted_env(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")


@pytest.fixture(autouse=True)
def stub_collaborators(monkeypatch, claims):
    monkeypatch.setattr(
        tools_module, "extract_identity_keys", lambda message, c: IdentityKeys(normalized_address="X")
    )
    monkeypatch.setattr(
        tools_module, "normalize_address", lambda line, city=None, state=None, postal=None: "NORM"
    )
    monkeypatch.setattr(
        tools_module,
        "resolve",
        lambda keys, repo: ResolutionResult(
            opportunity_id=None, property_id=None, created=False, confidence=0.0
        ),
    )
    monkeypatch.setattr(tools_module, "reconcile", lambda existing, c: (make_working_values(), []))
    monkeypatch.setattr(tools_module, "format_threshold_alert", fake_threshold_alert)


def use_status(monkeypatch, status: OpportunityStatus, **kwargs: Any) -> None:
    monkeypatch.setattr(
        tools_module,
        "run_underwriting",
        lambda values, policy, *, opportunity_id, trigger_event_id=None: make_run(
            opportunity_id, status, trigger_event_id=trigger_event_id, **kwargs
        ),
    )


def install_agent(monkeypatch, behaviour) -> list[ProcessingSession]:
    """Replace the real Strands agent with a callable that does exactly `behaviour(session)`."""
    seen: list[ProcessingSession] = []

    class FakeAgent:
        def __init__(self, session: ProcessingSession) -> None:
            self.session = session

        def __call__(self, prompt: str):
            behaviour(self.session)
            return type("R", (), {"message": {"content": [{"text": "one-line summary"}]}})()

    def build(session: ProcessingSession):
        seen.append(session)
        return FakeAgent(session)

    monkeypatch.setattr("dealsieve.pipeline.build_acquisition_agent", build)
    return seen


def run(message, fake_repo, policy, notifier):
    return process_inbound(message, repo=fake_repo, policy=policy, notifier=notifier, script=None)


# --------------------------------------------------------------------------- happy path


def test_a_watch_result_is_silent_and_fully_reported(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.WATCH)
    install_agent(
        monkeypatch,
        lambda s: (perform_record_claims(s, claims), perform_underwrite(s)),
    )

    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert outcome.created_opportunity is True
    assert outcome.status_before is None
    assert outcome.status_after == OpportunityStatus.WATCH
    assert outcome.notified_human is False and recording_notifier.sent == []
    assert outcome.run_id and outcome.model_backend == "scripted"
    assert outcome.summary == "one-line summary"
    assert outcome.events_created == [e.event_id for e in fake_repo.list_events(outcome.opportunity_id)]
    assert outcome.skeptic_report_id is None and outcome.draft_id is None


# --------------------------------------------------------------------------- dedupe


def test_a_duplicate_message_is_recognised_and_nothing_re_runs(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.WATCH)
    install_agent(monkeypatch, lambda s: (perform_record_claims(s, claims), perform_underwrite(s)))

    first = run(inbound_message, fake_repo, policy, recording_notifier)
    again = run(inbound_message, fake_repo, policy, recording_notifier)

    assert "duplicate" in again.summary.lower()
    assert again.opportunity_id == first.opportunity_id
    assert again.created_opportunity is False
    assert len(fake_repo.list_underwriting_runs(first.opportunity_id)) == 1


# --------------------------------------------------------------------------- safety net


def test_the_safety_net_underwrites_when_the_model_forgets(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.WATCH)
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))

    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert outcome.status_after == OpportunityStatus.WATCH
    assert outcome.run_id is not None
    assert "safety net" in outcome.summary
    completed = [
        e for e in fake_repo.list_events(outcome.opportunity_id) if e.type == EventType.UNDERWRITING_COMPLETED
    ]
    assert completed and completed[0].actor == Actor.SYSTEM


def test_the_safety_net_reviews_drafts_and_notifies_on_a_crossing(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))

    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )

    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert outcome.status_after == OpportunityStatus.REVIEW
    assert outcome.notified_human is True and len(recording_notifier.sent) == 1
    assert outcome.skeptic_report_id and outcome.draft_id
    types = fake_repo.event_types(outcome.opportunity_id)
    assert EventType.SKEPTIC_REVIEW_COMPLETED.value in types
    assert EventType.BROKER_DRAFT_CREATED.value in types
    assert EventType.HUMAN_NOTIFIED.value in types
    notified = [e for e in fake_repo.list_events(outcome.opportunity_id) if e.type == EventType.HUMAN_NOTIFIED]
    assert notified[0].actor == Actor.SYSTEM
    assert fake_repo.list_drafts(status="pending", opportunity_id=outcome.opportunity_id)


def test_the_safety_net_never_notifies_without_a_crossing(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.DEAD, max_viable=None, structural_failures=["tenant_count_min"])
    install_agent(monkeypatch, lambda s: perform_record_claims(s, claims))

    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert outcome.status_after == OpportunityStatus.DEAD
    assert outcome.notified_human is False and recording_notifier.sent == []


def test_the_safety_net_does_not_double_notify(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))

    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )

    def full_procedure(session: ProcessingSession) -> None:
        perform_record_claims(session, claims)
        perform_underwrite(session)
        tools_module.perform_skeptic_review(session)
        tools_module.perform_request_diligence(
            session, [{"topic": "Roof age", "question": "How old is the roof?"}]
        )
        tools_module.perform_notify_human(session, "crossed")

    install_agent(monkeypatch, full_procedure)
    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert len(recording_notifier.sent) == 1
    assert "safety net" not in outcome.summary
    assert len(fake_repo.list_drafts(opportunity_id=outcome.opportunity_id)) == 1


# --------------------------------------------------------------------------- failure containment


def test_a_model_failure_is_contained_and_recorded(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message, claims
):
    use_status(monkeypatch, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43"))

    import dealsieve.agents.skeptic as skeptic_module

    monkeypatch.setattr(
        skeptic_module, "run_skeptic", lambda s: make_skeptic_report(s.opportunity_id, s.run_after.run_id)
    )

    def record_then_explode(session: ProcessingSession) -> None:
        perform_record_claims(session, claims)
        raise RuntimeError("the CLI fell over")

    install_agent(monkeypatch, record_then_explode)
    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert "the CLI fell over" in outcome.summary
    notes = [e for e in fake_repo.list_events(outcome.opportunity_id) if e.type == EventType.NOTE]
    assert notes and notes[0].actor == Actor.SYSTEM
    assert outcome.status_after == OpportunityStatus.REVIEW, "the safety net still ran"
    assert outcome.notified_human is True


def test_a_failure_before_anything_is_recorded_still_returns_an_outcome(
    monkeypatch, fake_repo, policy, recording_notifier, inbound_message
):
    def explode(session: ProcessingSession) -> None:
        raise RuntimeError("model unavailable")

    install_agent(monkeypatch, explode)
    outcome = run(inbound_message, fake_repo, policy, recording_notifier)

    assert outcome.opportunity_id is None
    assert outcome.status_after is None
    assert "model unavailable" in outcome.summary
    assert outcome.notified_human is False
