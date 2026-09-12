"""Act 3 of the story: the autonomous diligence loop.

After the price drop makes the deal investable, DealSieve drafts an information request to the broker (roof age,
Phase I, CAM reconciliation). Humans stay in the loop: the request waits for a one-tap approval, then goes out.
Follow-ups on that approved thread are autonomous. The broker replies with a property condition report (PDF with photos).
DealSieve recognises the thread, reads the document text AND the photos, marks the roof question answered,
records $85k-$95k of day-one roof work as immediate capex, re-underwrites on the all-in basis, and the deal falls
back out of REVIEW to NEAR with a new frontier. That is a decision change, so the human is interrupted (second time
in the whole story) and a price-credit request is drafted, which must wait for approval because it is money talk.
Follow-ups for the still-open requests go out on the policy cadence; when the policy's follow-up budget is
exhausted, the loop stops and a human is told (third and last interruption).

Offline: scripted model, recording outbox, recording notifier. No network.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from dealsieve.diligence import approve_and_send, run_follow_ups
from dealsieve.ingestion import parse_eml
from dealsieve.notifications import RecordingNotifier
from dealsieve.outbound import RecordingOutbox
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import EventType, OpportunityStatus

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


def test_inspection_report_is_read_answers_requests_and_moves_the_frontier(fixtures_dir, repo, policy):
    notifier, outbox = RecordingNotifier(), RecordingOutbox()
    a = _run(fixtures_dir, repo, policy, notifier, outbox, "01_initial_offer")
    b = _run(fixtures_dir, repo, policy, notifier, outbox, "02_price_drop")
    assert b.status_after == OpportunityStatus.REVIEW and len(notifier.sent) == 1
    assert outbox.sent == [], "the information request waits for a human"
    pending = [d for d in repo.list_drafts(opportunity_id=a.opportunity_id) if d.kind == "information_request"]
    assert len(pending) == 1 and pending[0].status == "pending" and pending[0].requires_approval

    # --- the human taps Approve (Telegram button or dashboard) ---------------------------------------
    approved = approve_and_send(pending[0].draft_id, repo=repo, policy=policy, outbox=outbox)
    assert approved.status == "sent" and approved.delivery_ref
    assert len(outbox.sent) == 1 and outbox.sent[0].kind == "information_request"
    sent_before = {r.request_id: r for r in repo.list_diligence_requests(a.opportunity_id)}
    assert sent_before and all(r.status == "sent" and r.sent_at and r.due_at for r in sent_before.values())
    types_after_approval = [e.type for e in repo.list_events(a.opportunity_id)]
    assert EventType.HUMAN_APPROVED_DRAFT in types_after_approval
    assert EventType.DILIGENCE_REQUEST_SENT in types_after_approval
    assert any(e.actor.value == "human" for e in repo.list_events(a.opportunity_id) if e.type == EventType.HUMAN_APPROVED_DRAFT)

    # --- Event C: broker replies with the property condition report --------------------------------
    c = _run(fixtures_dir, repo, policy, notifier, outbox, "05_inspection_report")
    assert c.opportunity_id == a.opportunity_id, "the reply resolves to the same opportunity via the thread"
    assert c.status_before == OpportunityStatus.REVIEW
    assert c.status_after == OpportunityStatus.NEAR, c.summary
    assert c.notified_human is True and len(notifier.sent) == 2
    assert notifier.sent[1].kind == "fell_below_threshold"
    assert "roof" in notifier.sent[1].body.lower()

    analyses = repo.list_document_analyses(a.opportunity_id)
    assert len(analyses) == 1
    an = analyses[0]
    assert an.document_type == "inspection_report"
    assert an.images_reviewed >= 2, "the photos were actually looked at"
    assert an.capex_items and any("roof" in i.item.lower() for i in an.capex_items)
    assert any(f.image_ref for f in an.findings), "at least one finding rests on a photo"

    opp = repo.get_opportunity(a.opportunity_id)
    assert Decimal("80000") <= opp.working_values.immediate_capex <= Decimal("100000")
    run_c = repo.get_underwriting_run(c.run_id)
    assert run_c.financing.immediate_capex == opp.working_values.immediate_capex
    assert run_c.financing.all_in_basis == run_c.financing.purchase_price + run_c.financing.immediate_capex
    assert run_c.status == OpportunityStatus.NEAR
    assert Decimal("1190000") <= run_c.viability.max_viable_price <= Decimal("1225000")
    assert run_c.viability.distance_pct is not None and Decimal("0") < run_c.viability.distance_pct <= policy.classification.near_threshold_pct

    requests = {r.topic.lower(): r for r in repo.list_diligence_requests(a.opportunity_id)}
    roof = next(r for t, r in requests.items() if "roof" in t)
    assert roof.status == "answered" and roof.answered_by_document and roof.answer_evidence_ids
    phase_i = next(r for t, r in requests.items() if "phase i" in t)
    cam = next(r for t, r in requests.items() if "cam" in t)
    assert phase_i.status == "sent" and cam.status == "sent", "unanswered questions stay open"

    types = [e.type for e in repo.list_events(a.opportunity_id)]
    for t in (EventType.DOCUMENT_ANALYZED, EventType.DILIGENCE_ANSWERED, EventType.CAPEX_ADJUSTED, EventType.STATUS_CHANGED):
        assert t in types
    assert types.count(EventType.UNDERWRITING_COMPLETED) == 3

    credit = [d for d in repo.list_drafts(opportunity_id=a.opportunity_id) if d.kind == "credit_request"]
    assert len(credit) == 1 and credit[0].requires_approval and credit[0].status == "pending"
    assert len(outbox.sent) == 1, "the credit request was NOT sent: money talk waits for a human"

    # --- Follow-ups on the policy cadence; stop and escalate when the budget is exhausted -----------
    sent_at = phase_i.sent_at
    days = policy.outreach.follow_up_after_days
    r1 = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=sent_at + timedelta(days=days + 1))
    assert r1.follow_ups_sent == 1 and len(outbox.sent) == 2 and outbox.sent[1].kind == "follow_up"
    assert {r.topic.lower() for r in r1.requests_followed_up} >= {phase_i.topic.lower(), cam.topic.lower()}
    assert "roof" not in outbox.sent[1].body.lower(), "answered questions are not re-asked"

    r2 = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=sent_at + timedelta(days=2 * days + 2))
    assert r2.follow_ups_sent == 1 and len(outbox.sent) == 3
    assert all(r.follow_up_count == policy.outreach.max_follow_ups for r in repo.list_diligence_requests(a.opportunity_id) if r.status == "sent")

    r3 = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=sent_at + timedelta(days=3 * days + 3))
    assert r3.follow_ups_sent == 0 and len(outbox.sent) == 3, "follow-up budget exhausted: stop"
    assert r3.stalled == 2
    stalled = [r for r in repo.list_diligence_requests(a.opportunity_id) if r.status == "stalled"]
    assert {r.topic for r in stalled} == {phase_i.topic, cam.topic}
    assert len(notifier.sent) == 3 and notifier.sent[2].kind == "diligence_stalled"
    assert EventType.DILIGENCE_STALLED in [e.type for e in repo.list_events(a.opportunity_id)]

    # a fourth tick changes nothing: idempotent
    r4 = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=sent_at + timedelta(days=4 * days + 4))
    assert r4.follow_ups_sent == 0 and r4.stalled == 0 and len(notifier.sent) == 3 and len(outbox.sent) == 3


def test_information_requests_cannot_carry_money_talk(fixtures_dir, repo, policy):
    """The deterministic screen: a 'question' that negotiates is blocked before any send."""
    from dealsieve.diligence import classify_outbound_text

    assert classify_outbound_text("Can you confirm the age and condition of the roof?") == "information_request"
    assert classify_outbound_text("Would the seller accept $1,200,000?") == "credit_request"
    assert classify_outbound_text("We would need a $45,000 credit for the roof.") == "credit_request"
    assert classify_outbound_text("Please send the LOI template.") == "offer"


def test_autosend_policy_sends_information_requests_without_approval(fixtures_dir, repo, policy):
    """Flip one policy flag and the same loop sends the first message itself. Money talk still waits."""
    from dealsieve.policy import load_policy

    autosend = load_policy(fixtures_dir / "policies" / "autosend_policy.yaml")
    assert autosend.outreach.auto_send_information_requests is True
    notifier, outbox = RecordingNotifier(), RecordingOutbox()
    _run(fixtures_dir, repo, autosend, notifier, outbox, "01_initial_offer")
    b = _run(fixtures_dir, repo, autosend, notifier, outbox, "02_price_drop")
    assert b.status_after == OpportunityStatus.REVIEW
    assert len(outbox.sent) == 1 and outbox.sent[0].kind == "information_request"
    drafts = repo.list_drafts(opportunity_id=b.opportunity_id)
    assert len(drafts) == 1 and drafts[0].status == "sent" and drafts[0].requires_approval is False
    assert all(r.status == "sent" for r in repo.list_diligence_requests(b.opportunity_id))
