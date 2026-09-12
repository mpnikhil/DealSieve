"""Unit tests for the diligence engine, outbox, screening, and follow-up cadence.

Covers:
- classify_outbound_text: screening for offers and money talk
- build_requests: normalization and batch deduplication
- compose_information_request / follow_up / credit_request: formatting, Re: handling, policy signatures
- dispatch: policy gating, screen blocking, auto-send behavior
- approve_and_send: human approval, outbox delivery, idempotence, error handling
- match_answers: keyword families and token-set overlap
- apply_answers: resolving vs partial answers
- immediate_capex_from: policy urgency filtering and midpoint/high summing
- run_follow_ups: cadence advancement, per-opportunity batching, stall handling, idempotence
- FileOutbox: writing RFC 822 compliant .eml files
"""

from __future__ import annotations

import email
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.parser import BytesParser

import pytest

from dealsieve.diligence import (
    apply_answers,
    approve_and_send,
    build_requests,
    classify_outbound_text,
    compose_credit_request,
    compose_follow_up,
    compose_information_request,
    dispatch,
    immediate_capex_from,
    match_answers,
    run_follow_ups,
)
from dealsieve.notifications import RecordingNotifier
from dealsieve.outbound import FileOutbox, OutboxError, RecordingOutbox
from dealsieve.persistence import Repo
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    CapexItem,
    DiligenceRequest,
    DocumentAnalysis,
    EventType,
    Opportunity,
    OpportunityStatus,
    OutboundDraft,
    Property,
    RequestAnswer,
)

# --------------------------------------------------------------------------- helpers


def _make_opp(repo: Repo, display_name: str = "Test Property") -> Opportunity:
    prop = repo.upsert_property(
        Property(
            canonical_address=f"1 {display_name} St, Sacramento, CA",
            normalized_address=f"1 {display_name.upper()} ST SACRAMENTO CA",
            city="Sacramento",
            state="CA",
            postal_code="95826",
        )
    )
    opp = Opportunity(
        property_id=prop.property_id,
        display_name=display_name,
        status=OpportunityStatus.REVIEW,
        current_asking_price=Decimal("1250000"),
        broker_name="Maya Chen",
        broker_email="maya@brokerage.example",
    )
    return repo.create_opportunity(opp)


# --------------------------------------------------------------------------- classify_outbound_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please send over the roof warranty and age.", "information_request"),
        ("Could we get a copy of the Phase I ESA?", "information_request"),
        ("Attached is our LOI for your review.", "offer"),
        ("We are submitting a letter of intent on this property.", "offer"),
        ("Please find our purchase agreement attached.", "offer"),
        ("This represents our offer of $1.2M.", "offer"),
        ("Would the seller consider a $42,000 credit for the roof?", "credit_request"),
        ("We request a price reduction of $50,000 given the condition report.", "credit_request"),
        ("Can we discuss a discount or seller concession?", "credit_request"),
        ("We would pay $1,200,000 based on the capex requirements.", "credit_request"),
        ("Could the seller offer terms on the financing?", "credit_request"),
    ],
)
def test_classify_outbound_text_distinguishes_kinds(text: str, expected: str):
    assert classify_outbound_text(text) == expected


def test_classify_outbound_text_money_and_offers_outrank_questions():
    mixed = "What is the roof age? Also, would the seller accept a $50,000 credit?"
    assert classify_outbound_text(mixed) == "credit_request"

    mixed_loi = "Could you confirm tenant count? Here is our LOI."
    assert classify_outbound_text(mixed_loi) == "offer"


# --------------------------------------------------------------------------- build_requests


def test_build_requests_deduplicates_by_normalized_topic():
    items = [
        {"topic": "Roof Age", "question": "How old is the roof?", "category": "document"},
        {"topic": "roof   age", "question": "Duplicate question about roof age?", "category": "document"},
        {"topic": "ROOF-AGE", "question": "Another roof age duplicate", "category": "document"},
        {"topic": "Phase I", "question": "Is there an environmental report?", "category": "disclosure"},
        {"topic": "", "question": "Blank topic question"},
        {"topic": "No question", "question": "   "},
    ]
    requests = build_requests("opp_1", items, source="skeptic")
    assert len(requests) == 2
    assert requests[0].topic == "Roof Age"
    assert requests[0].source_concern == "skeptic"
    assert requests[1].topic == "Phase I"


# --------------------------------------------------------------------------- compose helpers


def test_compose_information_request_no_double_re_and_numbers_questions(policy: InvestmentPolicy):
    opp = Opportunity(
        property_id="p1",
        display_name="Power Inn",
        broker_name="Maya Chen",
        broker_email="maya@brokerage.example",
    )
    reqs = [
        DiligenceRequest(opportunity_id="opp_1", topic="Roof age", question="What is the roof age?"),
        DiligenceRequest(opportunity_id="opp_1", topic="Phase I", question="Is there a Phase I report?"),
    ]

    draft1 = compose_information_request(
        opp, reqs, policy, in_reply_to="<msg-1@example>", original_subject="Re: Off-market Power Inn"
    )
    assert draft1.subject == "Re: Off-market Power Inn"
    assert not draft1.subject.startswith("Re: Re:")
    assert "Maya" in draft1.body
    assert "1. What is the roof age?" in draft1.body
    assert "2. Is there a Phase I report?" in draft1.body
    assert draft1.to_email == "maya@brokerage.example"
    assert draft1.in_reply_to_message_id == "<msg-1@example>"
    assert draft1.requires_approval is True

    draft2 = compose_information_request(
        opp, reqs, policy, in_reply_to="<msg-1@example>", original_subject="Off-market Power Inn"
    )
    assert draft2.subject == "Re: Off-market Power Inn"


def test_compose_follow_up_references_number_and_topics(policy: InvestmentPolicy):
    opp = Opportunity(
        property_id="p1",
        display_name="Power Inn",
        broker_name="Maya Chen",
        broker_email="maya@brokerage.example",
    )
    reqs = [
        DiligenceRequest(opportunity_id="opp_1", topic="Phase I", question="Is there a Phase I report?"),
    ]
    draft = compose_follow_up(opp, reqs, policy, follow_up_number=2, in_reply_to="<msg-1@example>")
    assert draft.kind == "follow_up"
    assert "Following up #2" in draft.body
    assert "Phase I report?" in draft.body


def test_compose_credit_request_always_requires_approval(policy: InvestmentPolicy):
    opp = Opportunity(
        property_id="p1",
        display_name="Power Inn",
        broker_name="Maya Chen",
        broker_email="maya@brokerage.example",
    )
    draft = compose_credit_request(
        opp,
        amount=Decimal("42000"),
        rationale="Roof replacement budgeted at $90,000.",
        policy=policy,
        in_reply_to="<msg-2@example>",
    )
    assert draft.kind == "credit_request"
    assert draft.requires_approval is True
    assert "$42,000" in draft.body
    assert "Roof replacement" in draft.body


# --------------------------------------------------------------------------- dispatch & screening


def test_dispatch_blocks_money_in_information_request(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()

    sneaky_draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="broker@example.com",
        subject="Re: Inquiry",
        body="What is the roof age? Also we want a $40,000 credit.",
        questions=["What is the roof age?"],
        requires_approval=False,
    )

    result = dispatch(sneaky_draft, repo=repo, policy=policy, outbox=outbox)
    assert result.status == "pending"
    assert result.requires_approval is True
    assert result.kind == "credit_request"
    assert outbox.sent == [], "Must not send blocked message"

    events = repo.list_events(opp.opportunity_id)
    assert any(e.type == EventType.OUTBOUND_BLOCKED for e in events)


def test_dispatch_holds_pending_when_approval_required(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()

    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="broker@example.com",
        subject="Re: Inquiry",
        body="What is the roof age?",
        questions=["What is the roof age?"],
        requires_approval=True,
    )

    result = dispatch(draft, repo=repo, policy=policy, outbox=outbox)
    assert result.status == "pending"
    assert outbox.sent == []
    events = repo.list_events(opp.opportunity_id)
    assert any(e.type == EventType.BROKER_DRAFT_CREATED for e in events)


def test_dispatch_auto_sends_when_policy_permits(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()
    req = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof age", question="How old is the roof?")
    repo.store_diligence_request(req)

    auto_policy = policy.model_copy(
        update={"outreach": policy.outreach.model_copy(update={"auto_send_information_requests": True})}
    )

    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="broker@example.com",
        subject="Re: Inquiry",
        body="How old is the roof?",
        questions=["How old is the roof?"],
        request_ids=[req.request_id],
        requires_approval=False,
    )

    result = dispatch(draft, repo=repo, policy=auto_policy, outbox=outbox)
    assert result.status == "sent"
    assert result.sent_at is not None
    assert len(outbox.sent) == 1

    stored_req = repo.get_diligence_request(req.request_id)
    assert stored_req.status == "sent"
    assert stored_req.due_at is not None


# --------------------------------------------------------------------------- approve_and_send


def test_approve_and_send_dispatches_records_events_and_is_idempotent(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()
    req = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof", question="Roof condition?")
    repo.store_diligence_request(req)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email="broker@example.com",
        subject="Re: Roof",
        body="Roof condition?",
        questions=["Roof condition?"],
        request_ids=[req.request_id],
        requires_approval=True,
        status="pending",
    )
    repo.store_draft(draft)

    sent = approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=outbox)
    assert sent.status == "sent"
    assert len(outbox.sent) == 1

    events = repo.list_events(opp.opportunity_id)
    assert any(e.type == EventType.HUMAN_APPROVED_DRAFT and e.actor == Actor.HUMAN for e in events)
    assert any(e.type == EventType.BROKER_MESSAGE_SENT for e in events)

    again = approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=outbox)
    assert again.status == "sent"
    assert len(outbox.sent) == 1


def test_approve_and_send_rejects_rejected_draft(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="credit_request",
        to_email="broker@example.com",
        subject="Re: Credit",
        body="Credit request",
        status="rejected",
    )
    repo.store_draft(draft)
    with pytest.raises(ValueError, match="rejected draft cannot be approved"):
        approve_and_send(draft.draft_id, repo=repo, policy=policy, outbox=outbox)


def test_approve_and_send_raises_on_unknown_draft(repo: Repo, policy: InvestmentPolicy):
    outbox = RecordingOutbox()
    with pytest.raises(LookupError, match="No draft"):
        approve_and_send("nonexistent-draft", repo=repo, policy=policy, outbox=outbox)


# --------------------------------------------------------------------------- match_answers & apply_answers


def test_match_answers_matches_families():
    reqs = [
        DiligenceRequest(opportunity_id="opp_1", topic="Roof Age", question="When was roof installed?"),
        DiligenceRequest(opportunity_id="opp_1", topic="Mechanical HVAC", question="Unit vintages?"),
        DiligenceRequest(opportunity_id="opp_1", topic="Parking ratio", question="Stall count?"),
    ]
    analysis = DocumentAnalysis(
        analysis_id="a1",
        opportunity_id="opp_1",
        message_id="m1",
        filename="report.pdf",
        document_type="inspection_report",
        summary="Condition report",
        answers=[
            RequestAnswer(request_topic="Roofing membrane", answer="2001 built-up", resolves=True),
            RequestAnswer(request_topic="HVAC cooling", answer="6 units 2018, 2 units 1998", resolves=True),
            RequestAnswer(request_topic="Phase I ESA", answer="Not included", resolves=False),
        ],
    )
    matches = match_answers(reqs, analysis)
    assert len(matches) == 2
    matched_req_topics = {req.topic for req, _ in matches}
    assert matched_req_topics == {"Roof Age", "Mechanical HVAC"}


def test_apply_answers_resolving_and_partial(repo: Repo):
    opp = _make_opp(repo)
    r_roof = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof age", question="Age?", status="sent")
    repo.store_diligence_request(r_roof)
    r_cam = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="CAM audit", question="Audit?", status="sent")
    repo.store_diligence_request(r_cam)

    analysis = DocumentAnalysis(
        analysis_id="a1",
        opportunity_id=opp.opportunity_id,
        message_id="m1",
        filename="report.pdf",
        document_type="inspection_report",
        summary="Condition report",
        answers=[],
    )

    matches = [
        (r_roof, RequestAnswer(request_topic="Roof age", answer="Installed 2001", resolves=True)),
        (r_cam, RequestAnswer(request_topic="CAM audit", answer="Still reviewing 2024 records", resolves=False)),
    ]

    updated = apply_answers(matches, analysis, repo=repo, evidence_ids_by_topic={"Roof age": ["ev-1"]})
    assert len(updated) == 2

    stored_roof = repo.get_diligence_request(r_roof.request_id)
    assert stored_roof.status == "answered"
    assert stored_roof.answered_by_document == "report.pdf"
    assert stored_roof.answer_evidence_ids == ["ev-1"]
    assert stored_roof.due_at is None

    stored_cam = repo.get_diligence_request(r_cam.request_id)
    assert stored_cam.status == "sent"
    assert stored_cam.answer_summary.startswith("partial:")


# --------------------------------------------------------------------------- immediate_capex_from


def test_immediate_capex_from_filters_by_urgency(policy: InvestmentPolicy):
    items = [
        CapexItem(item="Roof replacement", low=Decimal("85000"), high=Decimal("95000"), urgency="immediate", source_document="report.pdf"),
        CapexItem(item="HVAC replacement", low=Decimal("28000"), high=Decimal("36000"), urgency="deferred", source_document="report.pdf"),
        CapexItem(item="Seal coat", low=Decimal("6000"), high=Decimal("8000"), urgency="deferred", source_document="report.pdf"),
    ]
    total = immediate_capex_from(items, policy)
    assert total == Decimal("90000")


# --------------------------------------------------------------------------- run_follow_ups


def test_run_follow_ups_cadence_batching_stall_and_idempotence(repo: Repo, policy: InvestmentPolicy):
    opp = _make_opp(repo)
    outbox = RecordingOutbox()
    notifier = RecordingNotifier()

    base_time = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    r1 = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Topic 1",
        question="Q1?",
        status="sent",
        sent_at=base_time,
        due_at=base_time + timedelta(days=3),
        follow_up_count=0,
    )
    repo.store_diligence_request(r1)
    r2 = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Topic 2",
        question="Q2?",
        status="sent",
        sent_at=base_time,
        due_at=base_time + timedelta(days=3),
        follow_up_count=0,
    )
    repo.store_diligence_request(r2)

    # 1. Before due date: nothing sent
    report1 = run_follow_ups(
        repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=base_time + timedelta(days=2)
    )
    assert report1.follow_ups_sent == 0
    assert len(outbox.sent) == 0

    # 2. At due date: single follow-up batched for both requests
    as_of_due = base_time + timedelta(days=3, hours=1)
    report2 = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=as_of_due)
    assert report2.follow_ups_sent == 1
    assert len(outbox.sent) == 1
    assert "Following up #1" in outbox.sent[0].body
    assert "Q1?" in outbox.sent[0].body and "Q2?" in outbox.sent[0].body

    updated_r1 = repo.get_diligence_request(r1.request_id)
    assert updated_r1.follow_up_count == 1
    assert updated_r1.due_at > as_of_due

    # 3. Running again with same as_of is idempotent
    report2_dup = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=as_of_due)
    assert report2_dup.follow_ups_sent == 0
    assert len(outbox.sent) == 1

    # 4. Advance past max_follow_ups -> marks stalled and alerts human
    stalled_due = as_of_due + timedelta(days=10)
    repo.update_diligence_request(updated_r1.model_copy(update={"follow_up_count": 2, "due_at": stalled_due - timedelta(days=1)}))
    repo.update_diligence_request(r2.model_copy(update={"follow_up_count": 2, "due_at": stalled_due - timedelta(days=1)}))

    report_stall = run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=stalled_due)
    assert report_stall.stalled == 2
    assert repo.get_diligence_request(r1.request_id).status == "stalled"
    assert repo.get_diligence_request(r2.request_id).status == "stalled"
    assert len(notifier.sent) == 1
    assert notifier.sent[0].kind == "diligence_stalled"

    # Running stall again is idempotent (dedupe_key prevents duplicate notification)
    run_follow_ups(repo=repo, policy=policy, outbox=outbox, notifier=notifier, as_of=stalled_due)
    assert len(notifier.sent) == 1


# --------------------------------------------------------------------------- FileOutbox


def test_file_outbox_writes_valid_rfc822_eml(tmp_path, policy: InvestmentPolicy):
    outbox = FileOutbox(policy=policy, directory=tmp_path)
    draft = OutboundDraft(
        opportunity_id="opp_1",
        kind="information_request",
        to_email="maya@brokerage.example",
        subject="Re: Off-market listing",
        body="Hello Maya,\n\nCould you clarify the roof age?\n\nThank you,\nDealSieve Team",
        in_reply_to_message_id="<orig-123@brokerage.example>",
    )

    path_str = outbox.send(draft)
    path = tmp_path / path_str
    assert path.is_file()

    with path.open("rb") as f:
        msg = BytesParser(policy=email.policy.default).parse(f)

    assert msg["To"] == "maya@brokerage.example"
    assert msg["From"] == f"{policy.outreach.from_name} <{policy.outreach.from_email}>"
    assert msg["Subject"] == "Re: Off-market listing"
    assert msg["In-Reply-To"] == "<orig-123@brokerage.example>"
    assert msg["References"] == "<orig-123@brokerage.example>"
    assert msg["Date"] is not None
    assert "Could you clarify the roof age?" in msg.get_content()


def test_file_outbox_raises_when_no_recipient(tmp_path, policy: InvestmentPolicy):
    outbox = FileOutbox(policy=policy, directory=tmp_path)
    draft = OutboundDraft(
        opportunity_id="opp_1",
        kind="information_request",
        to_email="",
        subject="Re: Missing recipient",
        body="No recipient",
    )
    with pytest.raises(OutboxError, match="no recipient email"):
        outbox.send(draft)
