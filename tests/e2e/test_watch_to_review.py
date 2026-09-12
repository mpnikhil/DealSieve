"""THE highest-priority test in the repository.

Broker email -> WATCH with a stored viability frontier -> price-drop email recognised as the SAME
opportunity -> ASKING_PRICE_CHANGED event -> new immutable underwriting run -> REVIEW -> exactly one
human interruption. A third, cheap-but-structurally-broken property must stay DEAD and stay silent.

Runs fully offline with the scripted model backend. No network, no model calls.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.ingestion import parse_eml
from dealsieve.notifications import RecordingNotifier
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import EventType, OpportunityStatus

pytestmark = pytest.mark.usefixtures("scripted_backend")


def _run(fixtures_dir, repo, policy, notifier, name: str):
    message = parse_eml(fixtures_dir / "emails" / f"{name}.eml")
    return process_inbound(
        message,
        repo=repo,
        policy=policy,
        notifier=notifier,
        script=str(fixtures_dir / "scripted" / f"{name}.json"),
    )


def test_watch_then_price_drop_flips_to_review_and_interrupts_once(fixtures_dir, repo, policy):
    notifier = RecordingNotifier()

    # --- Event A: initial broker email --------------------------------------------------------
    a = _run(fixtures_dir, repo, policy, notifier, "01_initial_offer")
    assert a.created_opportunity is True
    assert a.status_after == OpportunityStatus.WATCH, a.summary
    assert a.notified_human is False
    assert notifier.sent == [], "a WATCH result must not interrupt a human"

    opp = repo.get_opportunity(a.opportunity_id)
    assert opp.status == OpportunityStatus.WATCH
    assert opp.viability is not None and opp.viability.max_viable_price is not None
    assert Decimal("1250000") <= opp.viability.max_viable_price < Decimal("1550000") * Decimal("0.95"), (
        "frontier must sit between the future drop price and the NEAR band"
    )
    assert set(opp.viability.binding_constraints) & {"min_normalized_cap_rate", "min_base_dscr"}

    run_a = repo.get_underwriting_run(a.run_id)
    assert run_a.policy_version == policy.policy_version
    assert run_a.normalized.normalized_cap_rate < policy.underwriting.min_normalized_cap_rate
    assert run_a.normalized.broker_cap_rate is not None and run_a.normalized.broker_cap_rate > run_a.normalized.normalized_cap_rate
    assert run_a.financing.dscr < policy.underwriting.min_base_dscr
    assert all(g.passed for g in run_a.gates if g.kind.value == "structural"), "initial deal fails on valuation only"

    events_a = repo.list_events(a.opportunity_id)
    types_a = [e.type for e in events_a]
    assert EventType.DEAL_DISCOVERED in types_a
    assert EventType.UNDERWRITING_COMPLETED in types_a
    assert [e.seq for e in events_a] == list(range(1, len(events_a) + 1)), "events are append-only and sequential"

    # --- Event B: price-drop email in the same thread -------------------------------------------
    b = _run(fixtures_dir, repo, policy, notifier, "02_price_drop")
    assert b.created_opportunity is False
    assert b.opportunity_id == a.opportunity_id, "price drop must resolve to the SAME opportunity"
    assert b.status_before == OpportunityStatus.WATCH
    assert b.status_after == OpportunityStatus.REVIEW, b.summary
    assert b.notified_human is True
    assert len(notifier.sent) == 1, "exactly one human interruption"
    alert = notifier.sent[0]
    assert alert.kind == "threshold_crossed"
    assert "1,550,000" in alert.body or "1.55M" in alert.body
    assert "1,250,000" in alert.body or "1.25M" in alert.body

    opp = repo.get_opportunity(a.opportunity_id)
    assert opp.status == OpportunityStatus.REVIEW
    assert opp.previous_status == OpportunityStatus.WATCH
    assert opp.current_asking_price == Decimal("1250000")

    events_b = repo.list_events(a.opportunity_id)
    types_b = [e.type for e in events_b]
    assert EventType.ASKING_PRICE_CHANGED in types_b
    assert types_b.count(EventType.UNDERWRITING_COMPLETED) == 2
    assert EventType.STATUS_CHANGED in types_b
    assert EventType.HUMAN_NOTIFIED in types_b
    assert EventType.SKEPTIC_REVIEW_COMPLETED in types_b
    assert events_b[: len(events_a)] == events_a, "history is never rewritten"

    runs = repo.list_underwriting_runs(a.opportunity_id)
    assert len(runs) == 2
    run_b = repo.get_underwriting_run(b.run_id)
    assert run_b.normalized.normalized_cap_rate >= policy.underwriting.min_normalized_cap_rate
    assert run_b.financing.dscr >= policy.underwriting.min_base_dscr
    assert run_b.status == OpportunityStatus.REVIEW
    assert repo.get_underwriting_run(a.run_id) == run_a, "runs are immutable"

    reports = repo.list_skeptic_reports(a.opportunity_id)
    assert len(reports) == 1 and reports[0].concerns, "skeptic ran once, on REVIEW entry"
    drafts = repo.list_drafts(opportunity_id=a.opportunity_id)
    info = [d for d in drafts if d.kind == "information_request"]
    assert len(info) == 1, "one information request to the broker, sent autonomously under the outreach policy"
    assert info[0].status == "sent" and info[0].requires_approval is False
    assert all(d.requires_approval for d in drafts if d.kind in ("credit_request", "offer")), "money talk waits for a human"
    requests = repo.list_diligence_requests(a.opportunity_id)
    assert len(requests) >= 3 and all(r.status == "sent" for r in requests)
    topics = " ".join(r.topic.lower() for r in requests)
    assert "roof" in topics and "phase i" in topics and "cam" in topics


def test_structural_failure_stays_dead_and_silent(fixtures_dir, repo, policy):
    notifier = RecordingNotifier()
    c = _run(fixtures_dir, repo, policy, notifier, "03_structural_single_tenant")
    assert c.status_after == OpportunityStatus.DEAD, c.summary
    assert c.notified_human is False and notifier.sent == []
    opp = repo.get_opportunity(c.opportunity_id)
    assert opp.viability is not None
    assert opp.viability.max_viable_price is None, "no price fixes a structural failure"
    assert "largest_tenant_pct_max" in opp.viability.structural_failures


def test_duplicate_message_is_ignored(fixtures_dir, repo, policy):
    notifier = RecordingNotifier()
    first = _run(fixtures_dir, repo, policy, notifier, "01_initial_offer")
    again = _run(fixtures_dir, repo, policy, notifier, "01_initial_offer")
    assert again.opportunity_id == first.opportunity_id
    assert "duplicate" in again.summary.lower()
    assert len(repo.list_underwriting_runs(first.opportunity_id)) == 1
