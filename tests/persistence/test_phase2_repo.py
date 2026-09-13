"""W10a additions to Repo: claim_message (R4), notification dedupe_key (R3 constraint),
diligence_requests / document_analyses persistence, and the new dashboard/detail fields."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from dealsieve.persistence import DuplicateNotification, Repo
from dealsieve.schemas import (
    Channel,
    DiligenceRequest,
    DocumentAnalysis,
    DocumentFinding,
    EventType,
    InboundMessage,
    Notification,
    Opportunity,
    OpportunityEvent,
    OutboundDraft,
    Property,
    RequestAnswer,
)

# --------------------------------------------------------------------------- helpers


def _property(repo, address="1 Test St") -> Property:
    return repo.upsert_property(
        Property(canonical_address=f"{address}, Sacramento, CA", normalized_address=address.upper())
    )


def _opportunity(repo, display_name="Test deal") -> Opportunity:
    prop = _property(repo, address=f"{display_name} Ave")
    return repo.create_opportunity(Opportunity(property_id=prop.property_id, display_name=display_name))


def _set_updated_at(repo, message_id: str, when: datetime) -> None:
    repo._conn.execute(
        "UPDATE inbound_messages SET updated_at = ? WHERE message_id = ?", (when.isoformat(), message_id)
    )
    repo._conn.commit()


# --------------------------------------------------------------------------- claim_message (R4)


def test_claim_message_stores_message_and_returns_true_on_first_sight(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi", subject="s", thread_id="m1")
    assert repo.claim_message(msg) is True
    stored = repo.get_message("m1")
    assert stored == msg


def test_claim_message_returns_false_while_actively_processing(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    assert repo.claim_message(msg) is True
    # Not stale -- someone else (or this same in-flight call) owns it right now.
    assert repo.claim_message(msg) is False


def test_claim_message_returns_false_once_completed(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    assert repo.claim_message(msg) is True
    repo.mark_message_completed("m1")
    assert repo.claim_message(msg) is False


def test_claim_message_claims_a_received_row_stored_by_legacy_path(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    repo.store_inbound_message(msg)

    assert repo.claim_message(msg) is True
    row = repo._conn.execute(
        "SELECT status FROM inbound_messages WHERE message_id = ?", (msg.message_id,)
    ).fetchone()
    assert row["status"] == "processing"


def test_claim_message_reclaims_failed_message(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    repo.claim_message(msg)
    repo.mark_message_failed("m1", "boom")
    assert repo.claim_message(msg) is True


def test_claim_message_reclaims_stale_processing_but_not_fresh(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    repo.claim_message(msg)  # status=processing, updated_at=now

    # Fresh processing row: not reclaimed.
    assert repo.claim_message(msg) is False

    # Backdate as if the worker died 40 minutes ago (> 30 minute staleness window).
    _set_updated_at(repo, "m1", datetime.now(UTC) - timedelta(minutes=40))
    assert repo.claim_message(msg) is True


def test_mark_message_completed_and_failed_set_status_and_error(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    repo.claim_message(msg)
    repo.mark_message_failed("m1", "kaboom")
    row = repo._conn.execute("SELECT status, error FROM inbound_messages WHERE message_id='m1'").fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "kaboom"

    repo.claim_message(msg)
    repo.mark_message_completed("m1")
    row = repo._conn.execute("SELECT status, error FROM inbound_messages WHERE message_id='m1'").fetchone()
    assert row["status"] == "completed"
    assert row["error"] is None


def test_list_inbound_messages_by_opportunity(repo):
    opp = _opportunity(repo)
    other_opp = _opportunity(repo, display_name="Other deal")

    m1 = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="one")
    m2 = InboundMessage(message_id="m2", channel=Channel.EMAIL, body_text="two")
    m3 = InboundMessage(message_id="m3", channel=Channel.EMAIL, body_text="three")
    for m in (m1, m2, m3):
        repo.store_inbound_message(m)
    repo.link_message_to_opportunity("m1", opp.opportunity_id)
    repo.link_message_to_opportunity("m2", opp.opportunity_id)
    repo.link_message_to_opportunity("m3", other_opp.opportunity_id)

    listed = repo.list_inbound_messages(opp.opportunity_id)
    assert [m.message_id for m in listed] == ["m1", "m2"]
    assert repo.list_inbound_messages(other_opp.opportunity_id) == [m3]
    assert repo.list_inbound_messages("does-not-exist") == []


# --------------------------------------------------------------------------- notifications dedupe_key (R3)


def test_store_notification_raises_duplicate_notification_on_dedupe_key_clash(repo):
    opp = _opportunity(repo)
    n1 = Notification(
        opportunity_id=opp.opportunity_id,
        kind="threshold_crossed",
        channel=Channel.TELEGRAM,
        title="t",
        body="b",
        dedupe_key=f"{opp.opportunity_id}:run_1:threshold_crossed",
    )
    repo.store_notification(n1)

    n2 = n1.model_copy(update={"notification_id": "ntf_other"})
    with pytest.raises(DuplicateNotification) as excinfo:
        repo.store_notification(n2)
    assert excinfo.value.dedupe_key == n1.dedupe_key

    # Only one row was actually written.
    assert len(repo.list_notifications(opp.opportunity_id)) == 1

    # The handled constraint error must not leave an implicit transaction open and poison the
    # next method that uses BEGIN IMMEDIATE.
    event = repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=EventType.NOTE,
            summary="repo still works after dedupe",
        )
    )
    assert event.seq == 1


def test_duplicate_notification_id_is_not_misreported_as_dedupe_clash(repo):
    opp = _opportunity(repo)
    notification = Notification(
        opportunity_id=opp.opportunity_id,
        kind="threshold_crossed",
        channel=Channel.TELEGRAM,
        title="t",
        body="b",
        dedupe_key="first-key",
    )
    repo.store_notification(notification)
    collision = notification.model_copy(update={"dedupe_key": "different-key"})

    with pytest.raises(sqlite3.IntegrityError):
        repo.store_notification(collision)


def test_store_notification_allows_multiple_null_dedupe_keys(repo):
    opp = _opportunity(repo)
    n1 = Notification(opportunity_id=opp.opportunity_id, kind="status_update", channel=Channel.TELEGRAM, title="a", body="a")
    n2 = Notification(opportunity_id=opp.opportunity_id, kind="status_update", channel=Channel.TELEGRAM, title="b", body="b")
    repo.store_notification(n1)
    repo.store_notification(n2)  # both dedupe_key=None; must not collide
    assert len(repo.list_notifications(opp.opportunity_id)) == 2


def test_update_notification_persists_delivered_flag(repo):
    opp = _opportunity(repo)
    n = Notification(
        opportunity_id=opp.opportunity_id,
        kind="threshold_crossed",
        channel=Channel.TELEGRAM,
        title="t",
        body="b",
        dedupe_key="dk1",
        delivered=False,
    )
    repo.store_notification(n)
    delivered = n.model_copy(update={"delivered": True, "delivery_ref": "tg_1"})
    repo.update_notification(delivered)

    reloaded = repo.list_notifications(opp.opportunity_id)[0]
    assert reloaded.delivered is True
    assert reloaded.delivery_ref == "tg_1"
    assert reloaded.dedupe_key == "dk1"


# --------------------------------------------------------------------------- diligence_requests


def test_transition_draft_is_atomic_compare_and_swap(repo):
    opp = _opportunity(repo)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        to_email="broker@example.com",
        subject="Questions",
        body="What is the roof age?",
    )
    repo.store_draft(draft)

    assert repo.transition_draft(draft.draft_id, "pending", "approved") is True
    assert repo.transition_draft(draft.draft_id, "pending", "approved") is False
    assert repo.get_draft(draft.draft_id).status == "approved"
    assert repo.transition_draft(draft.draft_id, "approved", "sending") is True
    # The internal sending lease never leaks an invalid status through the shared schema.
    assert repo.get_draft(draft.draft_id).status == "approved"
    assert repo.transition_draft(draft.draft_id, "sending", "approved") is True


def test_reserve_follow_up_checks_status_count_and_due_date(repo):
    opp = _opportunity(repo)
    due = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    request = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Roof age",
        question="How old is the roof?",
        status="sent",
        due_at=due,
    )
    repo.store_diligence_request(request)

    assert repo.reserve_follow_up(request.request_id, 0, due - timedelta(seconds=1)) is False
    assert repo.reserve_follow_up(request.request_id, 0, due) is True
    assert repo.reserve_follow_up(request.request_id, 0, due) is False
    stored = repo.get_diligence_request(request.request_id)
    assert stored.follow_up_count == 1
    assert stored.last_follow_up_at == due


def test_diligence_request_round_trip_update_and_get(repo):
    opp = _opportunity(repo)
    req = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof age", question="How old is the roof?")
    repo.store_diligence_request(req)

    assert repo.get_diligence_request(req.request_id) == req
    assert repo.get_diligence_request("does-not-exist") is None

    sent = req.model_copy(update={"status": "sent", "sent_at": datetime.now(UTC)})
    repo.update_diligence_request(sent)
    reloaded = repo.get_diligence_request(req.request_id)
    assert reloaded.status == "sent"
    assert reloaded.sent_at is not None


def test_list_diligence_requests_filters_by_opportunity_and_status_ordered_by_created_at(repo):
    opp = _opportunity(repo)
    other = _opportunity(repo, display_name="Other")

    t0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)
    r1 = DiligenceRequest(
        opportunity_id=opp.opportunity_id, topic="Roof age", question="q1", status="sent", created_at=t0
    )
    r2 = DiligenceRequest(
        opportunity_id=opp.opportunity_id,
        topic="Phase I",
        question="q2",
        status="draft",
        created_at=t0 + timedelta(minutes=5),
    )
    r3 = DiligenceRequest(opportunity_id=other.opportunity_id, topic="CAM", question="q3", status="sent")
    for r in (r2, r1, r3):  # insert out of chronological order
        repo.store_diligence_request(r)

    all_for_opp = repo.list_diligence_requests(opportunity_id=opp.opportunity_id)
    assert [r.request_id for r in all_for_opp] == [r1.request_id, r2.request_id]

    sent_only = repo.list_diligence_requests(status="sent")
    assert {r.request_id for r in sent_only} == {r1.request_id, r3.request_id}

    everything = repo.list_diligence_requests()
    assert {r.request_id for r in everything} == {r1.request_id, r2.request_id, r3.request_id}


def test_open_diligence_count_counts_sent_and_overdue_only(repo):
    opp = _opportunity(repo)
    statuses = ["draft", "sent", "overdue", "answered", "stalled", "withdrawn"]
    for i, status in enumerate(statuses):
        repo.store_diligence_request(
            DiligenceRequest(opportunity_id=opp.opportunity_id, topic=f"topic{i}", question="q", status=status)
        )
    assert repo.open_diligence_count() == 2  # sent + overdue


def test_dashboard_stats_fills_open_diligence_requests(repo):
    opp = _opportunity(repo)
    repo.store_diligence_request(DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof", question="q", status="sent"))
    repo.store_diligence_request(DiligenceRequest(opportunity_id=opp.opportunity_id, topic="HVAC", question="q", status="answered"))

    stats = repo.dashboard_stats("policy_v1")
    assert stats.open_diligence_requests == 1


# --------------------------------------------------------------------------- document_analyses


def test_document_analysis_round_trip(repo):
    opp = _opportunity(repo)
    analysis = DocumentAnalysis(
        opportunity_id=opp.opportunity_id,
        message_id="m1",
        filename="report.pdf",
        document_type="inspection_report",
        summary="Roof needs replacement.",
        findings=[DocumentFinding(topic="Roof age", value="2001", severity="high", confidence=0.9, page=3, image_ref="image 1")],
        answers=[RequestAnswer(request_topic="Roof age", answer="Built 2001", resolves=True)],
        images_reviewed=2,
        image_paths=["data/documents/abc/img_1.png", "data/documents/abc/img_2.png"],
        text_chars=1200,
        model_backend="scripted",
    )
    repo.store_document_analysis(analysis)

    listed = repo.list_document_analyses(opp.opportunity_id)
    assert listed == [analysis]
    assert repo.list_document_analyses("does-not-exist") == []


def test_list_document_analyses_ordered_by_created_at(repo):
    opp = _opportunity(repo)
    t0 = datetime(2026, 9, 15, 9, 0, 0, tzinfo=UTC)
    a1 = DocumentAnalysis(
        opportunity_id=opp.opportunity_id, message_id="m1", filename="a.pdf",
        document_type="other", summary="first", created_at=t0,
    )
    a2 = DocumentAnalysis(
        opportunity_id=opp.opportunity_id, message_id="m2", filename="b.pdf",
        document_type="other", summary="second", created_at=t0 + timedelta(minutes=1),
    )
    repo.store_document_analysis(a2)
    repo.store_document_analysis(a1)
    assert [a.analysis_id for a in repo.list_document_analyses(opp.opportunity_id)] == [a1.analysis_id, a2.analysis_id]


# --------------------------------------------------------------------------- opportunity_detail


def test_opportunity_detail_includes_diligence_documents_and_messages(repo):
    opp = _opportunity(repo)
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi")
    repo.store_inbound_message(msg)
    repo.link_message_to_opportunity("m1", opp.opportunity_id)

    req = DiligenceRequest(opportunity_id=opp.opportunity_id, topic="Roof", question="q")
    repo.store_diligence_request(req)

    analysis = DocumentAnalysis(
        opportunity_id=opp.opportunity_id, message_id="m1", filename="r.pdf",
        document_type="inspection_report", summary="s",
    )
    repo.store_document_analysis(analysis)

    detail = repo.opportunity_detail(opp.opportunity_id)
    assert detail.diligence_requests == [req]
    assert detail.document_analyses == [analysis]
    assert detail.inbound_messages == [msg]


def test_init_schema_migrates_pre_phase2_database(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE inbound_messages (
            message_id TEXT PRIMARY KEY, channel TEXT NOT NULL, received_at TEXT NOT NULL,
            sender TEXT, subject TEXT, thread_id TEXT, opportunity_id TEXT, json TEXT NOT NULL
        );
        CREATE TABLE notifications (
            notification_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, kind TEXT NOT NULL,
            created_at TEXT NOT NULL, json TEXT NOT NULL
        );
        """
    )
    conn.close()
    repo = Repo(path)
    repo.init_schema()

    inbound_columns = {
        row["name"] for row in repo._conn.execute("PRAGMA table_info(inbound_messages)").fetchall()
    }
    notification_columns = {
        row["name"] for row in repo._conn.execute("PRAGMA table_info(notifications)").fetchall()
    }
    assert {"status", "error", "updated_at"} <= inbound_columns
    assert "dedupe_key" in notification_columns
