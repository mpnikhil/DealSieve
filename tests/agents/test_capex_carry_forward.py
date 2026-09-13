"""S6: the basis is derived from the stored analyses, whatever order the messages arrive in.

The trace explorer found this one: inject the property condition report *first*, then the offering.
The report has no price, so `reconcile` raises `MissingInputs` and the opportunity has no working
values for `analyze_document` to hang $90,000 of roof, HVAC and paving work on. The offering then
built fresh working values from its own claims -- and the capital work, already verified and stored
as a `DocumentAnalysis`, silently left the basis: all-in $1,550,000 instead of $1,640,000.

Real ordering, not an exotic one: a broker who sends the condition report ahead of the numbers, or
any retry that reorders two messages, reproduces it.

Runs fully offline with the scripted model backend.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.agents.tools import aggregate_capex_items
from dealsieve.diligence import immediate_capex_from
from dealsieve.ingestion import parse_eml
from dealsieve.notifications import RecordingNotifier
from dealsieve.outbound import RecordingOutbox
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import EventType

pytestmark = pytest.mark.usefixtures("scripted_backend")


def _run(fixtures_dir, repo, policy, notifier, outbox, name: str):
    message = parse_eml(fixtures_dir / "emails" / f"{name}.eml")
    return process_inbound(
        message,
        repo=repo,
        policy=policy,
        notifier=notifier,
        outbox=outbox,
        script=str(fixtures_dir / "scripted" / f"{name}.json"),
    )


def test_a_condition_report_before_the_offering_still_moves_the_basis(fixtures_dir, repo, policy):
    notifier, outbox = RecordingNotifier(), RecordingOutbox()

    first = _run(fixtures_dir, repo, policy, notifier, outbox, "05_inspection_report")
    assert first.opportunity_id is not None
    analyses = repo.list_document_analyses(first.opportunity_id)
    assert analyses, "the report was read and stored even without a price"

    second = _run(fixtures_dir, repo, policy, notifier, outbox, "01_initial_offer")
    assert second.opportunity_id == first.opportunity_id, "same property, one deal"

    items, _ = aggregate_capex_items(repo.list_document_analyses(second.opportunity_id))
    expected = immediate_capex_from(items, policy)
    assert expected == Decimal("90000"), "the fixture's verified capital work"

    opp = repo.get_opportunity(second.opportunity_id)
    assert opp.working_values is not None
    assert opp.working_values.immediate_capex == expected, (
        "S6: immediate_capex is the union of the verified items over every analysis"
    )
    assert {i.item for i in opp.working_values.capex_items} == {i.item for i in items}

    run = repo.get_underwriting_run(opp.latest_run_id)
    assert run.financing.immediate_capex == expected
    assert run.financing.all_in_basis == run.inputs.asking_price + expected

    adjusted = [
        e
        for e in repo.list_events(second.opportunity_id)
        if e.type == EventType.CAPEX_ADJUSTED and e.source_message_id == second.message_id
    ]
    assert adjusted, "the timeline says when the stored work entered the basis"
    assert adjusted[-1].payload["to"] == str(expected)


def test_the_usual_order_is_unchanged(fixtures_dir, repo, policy):
    """The offering first, the report second: the basis moves on the report, exactly as before."""
    notifier, outbox = RecordingNotifier(), RecordingOutbox()

    first = _run(fixtures_dir, repo, policy, notifier, outbox, "01_initial_offer")
    opp = repo.get_opportunity(first.opportunity_id)
    assert opp.working_values.immediate_capex == Decimal("0"), "no documents, no capex"

    second = _run(fixtures_dir, repo, policy, notifier, outbox, "05_inspection_report")
    assert second.opportunity_id == first.opportunity_id

    opp = repo.get_opportunity(second.opportunity_id)
    items, _ = aggregate_capex_items(repo.list_document_analyses(second.opportunity_id))
    assert opp.working_values.immediate_capex == immediate_capex_from(items, policy) == Decimal("90000")
    run = repo.get_underwriting_run(opp.latest_run_id)
    assert run.financing.all_in_basis == run.inputs.asking_price + Decimal("90000")
