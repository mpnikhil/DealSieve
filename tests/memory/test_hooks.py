"""The deterministic memory hooks wired into dealsieve.diligence: approve, reject-with-reason,
answered, stalled, and the notification ignore/review acknowledgement path.

Uses the same RecordingOutbox/RecordingNotifier pattern as tests/diligence and tests/e2e: a real
Repo (tmp_path-backed), a real policy, and in-memory outbox/notifier doubles. No mocking of
dealsieve.memory itself -- these exercise the real LocalMemoryStore end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dealsieve.diligence import (
    acknowledge_opportunity,
    apply_answers,
    approve_and_send,
    reject_draft,
    run_follow_ups,
)
from dealsieve.memory import LocalMemoryStore, broker_namespace, investor_namespace
from dealsieve.notifications import RecordingNotifier
from dealsieve.outbound import RecordingOutbox
from dealsieve.persistence import Repo
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    CapexItem,
    Channel,
    DiligenceRequest,
    DocumentAnalysis,
    Notification,
    Opportunity,
    OpportunityStatus,
    OutboundDraft,
    Property,
    RequestAnswer,
    ViabilityFrontier,
    WorkingValues,
)

# --------------------------------------------------------------------------- helpers


def _make_opp(repo: Repo, **overrides) -> Opportunity:
    prop = repo.upsert_property(
        Property(
            canonical_address="123 Power Inn Rd, Sacramento, CA",
            normalized_address="123 POWER INN RD SACRAMENTO CA",
            city="Sacramento",
            state="CA",
        )
    )
    opp = repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name="Power Inn",
            broker_name="Maya Chen",
            broker_email="maya.chen@brokerage.example",
        )
    )
    if overrides:
        opp = repo.save_opportunity(opp.model_copy(update=overrides))
    return opp


def _memory_texts(repo: Repo, namespace: str) -> list[str]:
    return [event.text for event in LocalMemoryStore(repo).list(namespace, limit=50)]


# --------------------------------------------------------------------------- approve


def test_approve_and_send_remembers_the_approved_credit_decision(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(
        repo,
        deal_number=113,
        status=OpportunityStatus.NEAR,
        working_values=WorkingValues(
            asking_price=Decimal("1250000"),
            gross_scheduled_income=Decimal("150000"),
            immediate_capex=Decimal("90000"),
            capex_items=[
                CapexItem(
                    item="Roof replacement",
                    low=Decimal("85000"),
                    high=Decimal("95000"),
                    urgency="immediate",
                    source_document="inspection.pdf",
                )
            ],
        ),
        viability=ViabilityFrontier(
            current_price=Decimal("1250000"),
            max_viable_price=Decimal("1208108"),
            distance_pct=Decimal("0.034"),
        ),
    )
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="credit_request",
        to_email="maya.chen@brokerage.example",
        subject="Diligence credit request: Power Inn",
        body="Based on our diligence, we would need a $42,000 credit. Roof replacement is past its useful life.",
        requires_approval=True,
        status="pending",
    )
    repo.store_draft(draft)

    sent = approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=RecordingOutbox(), principal="human:local")
    assert sent.status == "sent"

    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert (
        "Approved a $42,000 credit request to maya.chen@brokerage.example on deal #113 "
        "(Power Inn; capex: Roof replacement; roof capex $90,000; NEAR, 3.4% under ask)."
    ) in texts


def test_approve_and_send_remembers_an_information_request_approval(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(repo, deal_number=101)
    request = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof age", question="How old is the roof?")
    repo.store_diligence_request(request)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="maya.chen@brokerage.example",
        subject="Diligence questions: Power Inn",
        body="How old is the roof?",
        questions=["How old is the roof?"],
        request_ids=[request.request_id],
        requires_approval=True,
        status="pending",
    )
    repo.store_draft(draft)

    approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=RecordingOutbox(), principal="human:local")

    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert "Approved the information request on deal #101 (topics: Roof age)." in texts


def test_approving_an_already_sent_draft_does_not_double_record(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(repo, deal_number=102)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="maya.chen@brokerage.example",
        subject="Diligence questions: Power Inn",
        body="How old is the roof?",
        requires_approval=True,
        status="pending",
    )
    repo.store_draft(draft)
    outbox = RecordingOutbox()

    approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=outbox, principal="human:local")
    approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=outbox, principal="human:local")

    decisions = [e for e in LocalMemoryStore(repo).list(investor_namespace("human:local"), limit=50)]
    assert len(decisions) == 1


# --------------------------------------------------------------------------- reject with reason


def test_reject_draft_with_reason_is_remembered(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(repo, deal_number=107)
    phase_i = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Phase I", question="Any Phase I report?")
    cam = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="CAM", question="Any CAM reconciliation?")
    repo.store_diligence_request(phase_i)
    repo.store_diligence_request(cam)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="maya.chen@brokerage.example",
        subject="Diligence questions: Power Inn",
        body="Any Phase I report? Any CAM reconciliation?",
        request_ids=[phase_i.request_id, cam.request_id],
        status="pending",
    )
    repo.store_draft(draft)

    reject_draft(draft.draft_id, repo=repo, principal="human:local", reason="Broker already flagged both as N/A.")

    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert (
        "Rejected the information request on deal #107 (topics: Phase I, CAM). "
        "Reason: Broker already flagged both as N/A."
    ) in texts


def test_reject_draft_without_reason_omits_reason_sentence(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(repo, deal_number=108)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="maya.chen@brokerage.example",
        subject="Diligence questions: Power Inn",
        body="Some questions.",
        status="pending",
    )
    repo.store_draft(draft)

    # The Telegram reject path calls reject_draft without a reason.
    reject_draft(draft.draft_id, repo=repo, principal="human:telegram:123")

    texts = _memory_texts(repo, investor_namespace("human:telegram:123"))
    assert "Rejected the information request on deal #108." in texts
    assert not any("Reason:" in text for text in texts)


# --------------------------------------------------------------------------- apply_answers -> answered


def test_apply_answers_remembers_broker_answered_with_days_since_sent(repo: Repo) -> None:
    opp = _make_opp(repo, deal_number=105)
    sent_at = datetime(2026, 6, 1, tzinfo=UTC)
    request = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Roof age",
        question="How old is the roof?",
        status="sent",
        sent_at=sent_at,
    )
    repo.store_diligence_request(request)
    analysis = DocumentAnalysis(
        analysis_id="a1",
        opportunity_id=opp.opportunity_id,
        message_id="m1",
        filename="report.pdf",
        document_type="inspection_report",
        summary="Condition report",
    )
    answer = RequestAnswer(request_topic="Roof age", answer="Installed 2001", resolves=True)

    apply_answers([(request, answer)], analysis, repo=repo, evidence_ids_by_topic={"Roof age": ["ev-1"]})

    texts = _memory_texts(repo, broker_namespace("maya.chen@brokerage.example"))
    # Deterministic modulo the wall-clock "answered_at": assert the fixed parts and the day count
    # relative to `sent_at`, which is what the hook actually varies.
    matching = [t for t in texts if t.startswith("maya.chen@brokerage.example answered 'Roof age'")]
    assert len(matching) == 1
    assert "a condition report" in matching[0]
    assert "days after the request" in matching[0] or "day after the request" in matching[0]


def test_apply_answers_does_not_remember_partial_answers(repo: Repo) -> None:
    opp = _make_opp(repo, deal_number=106)
    request = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="CAM audit",
        question="Audit status?",
        status="sent",
        sent_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    repo.store_diligence_request(request)
    analysis = DocumentAnalysis(
        analysis_id="a2",
        opportunity_id=opp.opportunity_id,
        message_id="m2",
        filename="cam.pdf",
        document_type="cam_statement",
        summary="Partial CAM detail",
    )
    answer = RequestAnswer(request_topic="CAM audit", answer="Still reviewing", resolves=False)

    apply_answers([(request, answer)], analysis, repo=repo, evidence_ids_by_topic={})

    texts = _memory_texts(repo, broker_namespace("maya.chen@brokerage.example"))
    assert texts == []


# --------------------------------------------------------------------------- run_follow_ups -> stalled


def test_run_follow_ups_stall_is_remembered(repo: Repo, policy: InvestmentPolicy) -> None:
    opp = _make_opp(repo, deal_number=110)
    base_time = datetime(2026, 6, 1, tzinfo=UTC)
    r1 = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Topic 1",
        question="Q1?",
        status="sent",
        sent_at=base_time,
        due_at=base_time,
        follow_up_count=2,
    )
    r2 = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Topic 2",
        question="Q2?",
        status="sent",
        sent_at=base_time,
        due_at=base_time,
        follow_up_count=2,
    )
    repo.store_diligence_request(r1)
    repo.store_diligence_request(r2)

    report = run_follow_ups(
        repo=repo,
        policy=policy,
        outbox=RecordingOutbox(),
        notifier=RecordingNotifier(),
        as_of=base_time + timedelta(days=1),
    )
    assert report.stalled == 2

    texts = _memory_texts(repo, broker_namespace("maya.chen@brokerage.example"))
    assert (
        "maya.chen@brokerage.example went silent on 2 requests after 2 follow-ups (Topic 1, Topic 2)."
        in texts
    )


# --------------------------------------------------------------------------- ignore/review acknowledgement


def test_ignoring_a_fell_below_alert_is_remembered(repo: Repo) -> None:
    opp = _make_opp(repo, deal_number=113)
    notification = Notification(
        opportunity_id=opp.opportunity_id,
        kind="fell_below_threshold",
        channel=Channel.MANUAL,
        title="DEAL #113 FELL BACK BELOW THRESHOLD",
        body="...",
    )
    repo.store_notification(notification)

    acknowledge_opportunity(
        opp.opportunity_id,
        repo=repo,
        principal="human:local",
        notification=notification,
        action="ignore",
    )

    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert "Ignored the fell-below alert on deal #113." in texts


def test_reviewing_a_stalled_alert_is_remembered_with_review_wording(repo: Repo) -> None:
    opp = _make_opp(repo, deal_number=114)
    notification = Notification(
        opportunity_id=opp.opportunity_id,
        kind="diligence_stalled",
        channel=Channel.MANUAL,
        title="DEAL #114 DILIGENCE STALLED",
        body="...",
    )
    repo.store_notification(notification)

    acknowledge_opportunity(
        opp.opportunity_id,
        repo=repo,
        principal="human:local",
        notification=notification,
        action="review",
    )

    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert "Opened the diligence-stalled alert for review on deal #114." in texts


def test_acknowledge_without_notification_does_not_record_an_alert_memory(repo: Repo) -> None:
    opp = _make_opp(repo, deal_number=115)
    acknowledge_opportunity(opp.opportunity_id, repo=repo, principal="human:local")
    texts = _memory_texts(repo, investor_namespace("human:local"))
    assert texts == []
