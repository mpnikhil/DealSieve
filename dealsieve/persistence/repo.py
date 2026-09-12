"""Repository over SQLite (stdlib sqlite3, JSON columns for nested contracts).

W2 implements. Tables (see docs/CONTRACTS.md): properties, opportunities, opportunity_events,
inbound_messages, source_documents, evidence_observations, underwriting_runs, policy_versions,
skeptic_reports, outbound_drafts, notifications.

Nested contracts are stored as JSON TEXT produced by ``json.dumps(model.model_dump(mode="python"),
default=str)`` so ``Decimal`` and timezone-aware ``datetime`` round-trip exactly through
``Model.model_validate(json.loads(text))``.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from dealsieve.persistence.db import connect
from dealsieve.persistence.db import init_schema as _init_schema
from dealsieve.schemas import (
    DashboardStats,
    DSModel,
    EventType,
    Evidence,
    InboundMessage,
    Notification,
    Opportunity,
    OpportunityDetail,
    OpportunityEvent,
    OpportunityStatus,
    OutboundDraft,
    Property,
    SkepticReport,
    UnderwritingResult,
    WatchlistItem,
    new_id,
    now_utc,
)

DEFAULT_DB_PATH = Path(os.environ.get("DEALSIEVE_DB_PATH", "data/dealsieve.db"))

_STATUS_RANK = {
    OpportunityStatus.REVIEW: 0,
    OpportunityStatus.NEAR: 1,
    OpportunityStatus.WATCH: 2,
}

_CONDITION_CHANGE_TYPES = {
    EventType.ASKING_PRICE_CHANGED,
    EventType.NOI_CHANGED,
    EventType.RENT_ROLL_UPDATED,
    EventType.FINANCING_CHANGED,
}


def _dump(model: DSModel) -> str:
    return json.dumps(model.model_dump(mode="python"), default=str)


def _load[M: DSModel](cls: type[M], text: str) -> M:
    return cls.model_validate(json.loads(text))


def _locked[F: Callable[..., Any]](method: F) -> F:
    """Serialize access to the shared sqlite3 connection.

    Repo is usually built on one thread, but callers such as the Strands agent runtime dispatch
    synchronous ``@tool`` calls onto worker threads (``asyncio.to_thread``). sqlite3 connections are
    not safe for concurrent use from multiple threads even with ``check_same_thread=False``, and
    read-modify-write sequences (``append_event``'s ``max(seq)+1``, ``create_opportunity``'s
    ``max(deal_number)+1``) would otherwise race. An RLock lets locked methods call other locked
    methods on the same thread (e.g. ``opportunity_detail``) without deadlocking.
    """

    @functools.wraps(method)
    def wrapper(self: Repo, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class Repo:
    """All methods are synchronous. One Repo per process is fine; sqlite3 with WAL."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._conn: sqlite3.Connection = connect(self.db_path)
        self._lock = threading.RLock()

    @_locked
    def init_schema(self) -> None:
        _init_schema(self._conn)

    # ------------------------------------------------------------------ messages

    @_locked
    def store_inbound_message(self, message: InboundMessage) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO inbound_messages
                (message_id, channel, received_at, sender, subject, thread_id, opportunity_id, json)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                message.message_id,
                str(message.channel),
                message.received_at.isoformat(),
                message.sender,
                message.subject,
                message.thread_id,
                _dump(message),
            ),
        )
        self._conn.commit()

    @_locked
    def message_exists(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM inbound_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    @_locked
    def get_message(self, message_id: str) -> InboundMessage | None:
        row = self._conn.execute(
            "SELECT json FROM inbound_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return _load(InboundMessage, row["json"]) if row else None

    # ------------------------------------------------------------------ properties & opportunities

    @_locked
    def upsert_property(self, prop: Property) -> Property:
        conn = self._conn
        row = None
        if prop.normalized_address:
            row = conn.execute(
                "SELECT property_id FROM properties WHERE normalized_address = ?",
                (prop.normalized_address,),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT property_id FROM properties WHERE property_id = ?",
                (prop.property_id,),
            ).fetchone()
        if row is not None:
            canonical_id = row["property_id"]
            merged = prop.model_copy(update={"property_id": canonical_id})
            conn.execute(
                "UPDATE properties SET normalized_address = ?, apn = ?, json = ? WHERE property_id = ?",
                (merged.normalized_address, merged.apn, _dump(merged), canonical_id),
            )
            conn.commit()
            return merged
        conn.execute(
            "INSERT INTO properties (property_id, normalized_address, apn, json) VALUES (?, ?, ?, ?)",
            (prop.property_id, prop.normalized_address, prop.apn, _dump(prop)),
        )
        conn.commit()
        return prop

    @_locked
    def get_property(self, property_id: str) -> Property | None:
        row = self._conn.execute(
            "SELECT json FROM properties WHERE property_id = ?", (property_id,)
        ).fetchone()
        return _load(Property, row["json"]) if row else None

    @_locked
    def list_properties(self) -> list[Property]:
        rows = self._conn.execute("SELECT json FROM properties ORDER BY rowid").fetchall()
        return [_load(Property, r["json"]) for r in rows]

    @_locked
    def create_opportunity(self, opp: Opportunity) -> Opportunity:
        """Assigns deal_number (sequential, starting at 101) and persists."""
        conn = self._conn
        row = conn.execute("SELECT MAX(deal_number) AS m FROM opportunities").fetchone()
        next_deal_number = (row["m"] + 1) if row and row["m"] is not None else 101
        final = opp.model_copy(update={"deal_number": next_deal_number})
        conn.execute(
            """
            INSERT INTO opportunities
                (opportunity_id, deal_number, property_id, status, broker_property_ref, listing_url,
                 updated_at, json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                final.opportunity_id,
                final.deal_number,
                final.property_id,
                str(final.status),
                final.broker_property_ref,
                final.listing_url,
                final.updated_at.isoformat(),
                _dump(final),
            ),
        )
        conn.commit()
        return final

    @_locked
    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self._conn.execute(
            "SELECT json FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
        ).fetchone()
        return _load(Opportunity, row["json"]) if row else None

    @_locked
    def get_opportunity_by_deal_number(self, deal_number: int) -> Opportunity | None:
        row = self._conn.execute(
            "SELECT json FROM opportunities WHERE deal_number = ?", (deal_number,)
        ).fetchone()
        return _load(Opportunity, row["json"]) if row else None

    @_locked
    def save_opportunity(self, opp: Opportunity) -> Opportunity:
        """Update derived state only (status, working_values, viability, latest_run_id, ...)."""
        updated = opp.model_copy(update={"updated_at": now_utc()})
        self._conn.execute(
            """
            UPDATE opportunities
               SET deal_number = ?, property_id = ?, status = ?, broker_property_ref = ?,
                   listing_url = ?, updated_at = ?, json = ?
             WHERE opportunity_id = ?
            """,
            (
                updated.deal_number,
                updated.property_id,
                str(updated.status),
                updated.broker_property_ref,
                updated.listing_url,
                updated.updated_at.isoformat(),
                _dump(updated),
                updated.opportunity_id,
            ),
        )
        self._conn.commit()
        return updated

    @_locked
    def list_opportunities(self, status: OpportunityStatus | None = None) -> list[Opportunity]:
        if status is None:
            rows = self._conn.execute(
                "SELECT json FROM opportunities ORDER BY deal_number"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT json FROM opportunities WHERE status = ? ORDER BY deal_number",
                (str(status),),
            ).fetchall()
        return [_load(Opportunity, r["json"]) for r in rows]

    # ------------------------------------------------------------------ events (append-only)

    @_locked
    def append_event(self, event: OpportunityEvent) -> OpportunityEvent:
        """Assigns seq (per opportunity, monotonically increasing) and persists. Returns the stored event."""
        conn = self._conn
        row = conn.execute(
            "SELECT MAX(seq) AS m FROM opportunity_events WHERE opportunity_id = ?",
            (event.opportunity_id,),
        ).fetchone()
        next_seq = (row["m"] + 1) if row and row["m"] is not None else 1
        final = event.model_copy(update={"seq": next_seq})
        conn.execute(
            """
            INSERT INTO opportunity_events
                (event_id, opportunity_id, seq, type, occurred_at, source_message_id, json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                final.event_id,
                final.opportunity_id,
                final.seq,
                str(final.type),
                final.occurred_at.isoformat(),
                final.source_message_id,
                _dump(final),
            ),
        )
        conn.commit()
        return final

    @_locked
    def list_events(self, opportunity_id: str) -> list[OpportunityEvent]:
        rows = self._conn.execute(
            "SELECT json FROM opportunity_events WHERE opportunity_id = ? ORDER BY seq",
            (opportunity_id,),
        ).fetchall()
        return [_load(OpportunityEvent, r["json"]) for r in rows]

    # ------------------------------------------------------------------ evidence & documents

    @_locked
    def store_evidence(self, opportunity_id: str, evidence: list[Evidence]) -> None:
        conn = self._conn
        for e in evidence:
            conn.execute(
                """
                INSERT OR REPLACE INTO evidence_observations (evidence_id, opportunity_id, field, json)
                VALUES (?, ?, ?, ?)
                """,
                (e.evidence_id, opportunity_id, e.field, _dump(e)),
            )
        conn.commit()

    @_locked
    def list_evidence(self, opportunity_id: str) -> list[Evidence]:
        rows = self._conn.execute(
            "SELECT json FROM evidence_observations WHERE opportunity_id = ? ORDER BY rowid",
            (opportunity_id,),
        ).fetchall()
        return [_load(Evidence, r["json"]) for r in rows]

    @_locked
    def store_document(
        self, opportunity_id: str, message_id: str, filename: str, sha256: str, text: str | None
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO source_documents (id, opportunity_id, message_id, filename, sha256, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("doc"), opportunity_id, message_id, filename, sha256, text),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ underwriting runs (append-only)

    @_locked
    def store_underwriting_run(self, run: UnderwritingResult) -> None:
        self._conn.execute(
            """
            INSERT INTO underwriting_runs (run_id, opportunity_id, policy_version, created_at, status, json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.opportunity_id,
                run.policy_version,
                run.created_at.isoformat(),
                str(run.status),
                _dump(run),
            ),
        )
        self._conn.commit()

    @_locked
    def get_underwriting_run(self, run_id: str) -> UnderwritingResult | None:
        row = self._conn.execute(
            "SELECT json FROM underwriting_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return _load(UnderwritingResult, row["json"]) if row else None

    @_locked
    def list_underwriting_runs(self, opportunity_id: str) -> list[UnderwritingResult]:
        rows = self._conn.execute(
            "SELECT json FROM underwriting_runs WHERE opportunity_id = ? ORDER BY rowid",
            (opportunity_id,),
        ).fetchall()
        return [_load(UnderwritingResult, r["json"]) for r in rows]

    @_locked
    def record_policy_version(self, policy_version: str, name: str, raw_yaml: str) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO policy_versions (policy_version, name, raw_yaml, first_seen_at)
            VALUES (?, ?, ?, ?)
            """,
            (policy_version, name, raw_yaml, now_utc().isoformat()),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ skeptic, drafts, notifications

    @_locked
    def store_skeptic_report(self, report: SkepticReport) -> None:
        self._conn.execute(
            """
            INSERT INTO skeptic_reports (report_id, opportunity_id, run_id, created_at, json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                report.report_id,
                report.opportunity_id,
                report.run_id,
                report.created_at.isoformat(),
                _dump(report),
            ),
        )
        self._conn.commit()

    @_locked
    def list_skeptic_reports(self, opportunity_id: str) -> list[SkepticReport]:
        rows = self._conn.execute(
            "SELECT json FROM skeptic_reports WHERE opportunity_id = ? ORDER BY rowid",
            (opportunity_id,),
        ).fetchall()
        return [_load(SkepticReport, r["json"]) for r in rows]

    @_locked
    def store_draft(self, draft: OutboundDraft) -> None:
        self._conn.execute(
            """
            INSERT INTO outbound_drafts (draft_id, opportunity_id, status, created_at, json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (draft.draft_id, draft.opportunity_id, draft.status, draft.created_at.isoformat(), _dump(draft)),
        )
        self._conn.commit()

    @_locked
    def get_draft(self, draft_id: str) -> OutboundDraft | None:
        row = self._conn.execute(
            "SELECT json FROM outbound_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return _load(OutboundDraft, row["json"]) if row else None

    @_locked
    def update_draft(self, draft: OutboundDraft) -> None:
        self._conn.execute(
            "UPDATE outbound_drafts SET status = ?, json = ? WHERE draft_id = ?",
            (draft.status, _dump(draft), draft.draft_id),
        )
        self._conn.commit()

    @_locked
    def list_drafts(
        self, status: str | None = None, opportunity_id: str | None = None
    ) -> list[OutboundDraft]:
        query = "SELECT json FROM outbound_drafts WHERE 1=1"
        params: list[Any] = []
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        if opportunity_id is not None:
            query += " AND opportunity_id = ?"
            params.append(opportunity_id)
        query += " ORDER BY rowid"
        rows = self._conn.execute(query, params).fetchall()
        return [_load(OutboundDraft, r["json"]) for r in rows]

    @_locked
    def store_notification(self, notification: Notification) -> None:
        self._conn.execute(
            """
            INSERT INTO notifications (notification_id, opportunity_id, kind, created_at, json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                notification.notification_id,
                notification.opportunity_id,
                notification.kind,
                notification.created_at.isoformat(),
                _dump(notification),
            ),
        )
        self._conn.commit()

    @_locked
    def list_notifications(self, opportunity_id: str | None = None) -> list[Notification]:
        if opportunity_id is None:
            rows = self._conn.execute("SELECT json FROM notifications ORDER BY rowid").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT json FROM notifications WHERE opportunity_id = ? ORDER BY rowid",
                (opportunity_id,),
            ).fetchall()
        return [_load(Notification, r["json"]) for r in rows]

    # ------------------------------------------------------------------ read models for API/dashboard

    @_locked
    def dashboard_stats(self, policy_version: str) -> DashboardStats:
        conn = self._conn
        encountered = conn.execute("SELECT COUNT(*) AS c FROM opportunities").fetchone()["c"]

        def _count_status(status: OpportunityStatus) -> int:
            return conn.execute(
                "SELECT COUNT(*) AS c FROM opportunities WHERE status = ?", (str(status),)
            ).fetchone()["c"]

        dead = _count_status(OpportunityStatus.DEAD)
        watch = _count_status(OpportunityStatus.WATCH)
        near = _count_status(OpportunityStatus.NEAR)
        review = _count_status(OpportunityStatus.REVIEW)

        cutoff = (now_utc() - timedelta(days=7)).isoformat()

        condition_rows = conn.execute(
            "SELECT type FROM opportunity_events WHERE occurred_at >= ?", (cutoff,)
        ).fetchall()
        conditions_changed_7d = sum(
            1 for r in condition_rows if r["type"] in {str(t) for t in _CONDITION_CHANGE_TYPES}
        )

        status_changed_rows = conn.execute(
            "SELECT json FROM opportunity_events WHERE occurred_at >= ? AND type = ?",
            (cutoff, str(EventType.STATUS_CHANGED)),
        ).fetchall()
        threshold_crossings_7d = 0
        for r in status_changed_rows:
            payload = json.loads(r["json"]).get("payload", {})
            if payload.get("to") == str(OpportunityStatus.REVIEW):
                threshold_crossings_7d += 1

        human_interruptions_7d = conn.execute(
            "SELECT COUNT(*) AS c FROM opportunity_events WHERE occurred_at >= ? AND type = ?",
            (cutoff, str(EventType.HUMAN_NOTIFIED)),
        ).fetchone()["c"]

        return DashboardStats(
            encountered=encountered,
            dead=dead,
            watch=watch,
            near=near,
            review=review,
            conditions_changed_7d=conditions_changed_7d,
            threshold_crossings_7d=threshold_crossings_7d,
            human_interruptions_7d=human_interruptions_7d,
            policy_version=policy_version,
        )

    @_locked
    def watchlist(self) -> list[WatchlistItem]:
        """All non-DEAD opportunities sorted by distance_pct ascending (REVIEW first, then NEAR, WATCH)."""
        rows = self._conn.execute(
            "SELECT json FROM opportunities WHERE status != ? ORDER BY deal_number",
            (str(OpportunityStatus.DEAD),),
        ).fetchall()
        opps = [_load(Opportunity, r["json"]) for r in rows]

        items = [
            WatchlistItem(
                opportunity_id=o.opportunity_id,
                deal_number=o.deal_number,
                display_name=o.display_name,
                status=o.status,
                current_asking_price=o.current_asking_price,
                max_viable_price=o.viability.max_viable_price if o.viability else None,
                distance_pct=o.viability.distance_pct if o.viability else None,
                binding_constraints=o.viability.binding_constraints if o.viability else [],
                reason_summary=o.reason_summary,
                updated_at=o.updated_at,
            )
            for o in opps
        ]

        def sort_key(item: WatchlistItem) -> tuple[int, int, Any]:
            rank = _STATUS_RANK.get(item.status, 3)
            if item.distance_pct is None:
                return (rank, 1, 0)
            return (rank, 0, item.distance_pct)

        items.sort(key=sort_key)
        return items

    @_locked
    def opportunity_detail(self, opportunity_id: str) -> OpportunityDetail | None:
        """Accepts an opportunity_id or a deal-number string."""
        opp = self.get_opportunity(opportunity_id)
        if opp is None and opportunity_id.isdigit():
            opp = self.get_opportunity_by_deal_number(int(opportunity_id))
        if opp is None:
            return None

        prop = self.get_property(opp.property_id)
        if prop is None:
            return None

        runs = self.list_underwriting_runs(opp.opportunity_id)
        latest_run = None
        if opp.latest_run_id is not None:
            latest_run = self.get_underwriting_run(opp.latest_run_id)
        if latest_run is None and runs:
            latest_run = runs[-1]

        return OpportunityDetail(
            opportunity=opp,
            property=prop,
            latest_run=latest_run,
            runs=runs,
            events=self.list_events(opp.opportunity_id),
            evidence=self.list_evidence(opp.opportunity_id),
            skeptic_reports=self.list_skeptic_reports(opp.opportunity_id),
            drafts=self.list_drafts(opportunity_id=opp.opportunity_id),
            notifications=self.list_notifications(opportunity_id=opp.opportunity_id),
        )

    # ------------------------------------------------------------------ identity lookups

    @_locked
    def link_message_to_opportunity(self, message_id: str, opportunity_id: str) -> None:
        self._conn.execute(
            "UPDATE inbound_messages SET opportunity_id = ? WHERE message_id = ?",
            (opportunity_id, message_id),
        )
        self._conn.commit()

    @_locked
    def find_opportunity_id_by_message_id(self, message_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT opportunity_id FROM inbound_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return None
        return row["opportunity_id"]

    @_locked
    def find_opportunity_id_by_thread_id(self, thread_id: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT opportunity_id FROM inbound_messages
             WHERE thread_id = ? AND opportunity_id IS NOT NULL
             ORDER BY rowid LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        return row["opportunity_id"]

    @_locked
    def find_opportunity_ids_by_keys(
        self,
        *,
        normalized_address: str | None = None,
        apn: str | None = None,
        listing_url: str | None = None,
        broker_property_ref: str | None = None,
    ) -> dict[str, str]:
        conn = self._conn
        result: dict[str, str] = {}

        if normalized_address:
            row = conn.execute(
                "SELECT property_id FROM properties WHERE normalized_address = ?",
                (normalized_address,),
            ).fetchone()
            if row is not None:
                opp_row = conn.execute(
                    "SELECT opportunity_id FROM opportunities WHERE property_id = ? ORDER BY rowid LIMIT 1",
                    (row["property_id"],),
                ).fetchone()
                if opp_row is not None:
                    result["normalized_address"] = opp_row["opportunity_id"]

        if apn:
            row = conn.execute(
                "SELECT property_id FROM properties WHERE apn = ? ORDER BY rowid LIMIT 1", (apn,)
            ).fetchone()
            if row is not None:
                opp_row = conn.execute(
                    "SELECT opportunity_id FROM opportunities WHERE property_id = ? ORDER BY rowid LIMIT 1",
                    (row["property_id"],),
                ).fetchone()
                if opp_row is not None:
                    result["apn"] = opp_row["opportunity_id"]

        if listing_url:
            row = conn.execute(
                "SELECT opportunity_id FROM opportunities WHERE listing_url = ? ORDER BY rowid LIMIT 1",
                (listing_url,),
            ).fetchone()
            if row is not None:
                result["listing_url"] = row["opportunity_id"]

        if broker_property_ref:
            row = conn.execute(
                """
                SELECT opportunity_id FROM opportunities WHERE broker_property_ref = ?
                 ORDER BY rowid LIMIT 1
                """,
                (broker_property_ref,),
            ).fetchone()
            if row is not None:
                result["broker_property_ref"] = row["opportunity_id"]

        return result

    @_locked
    def find_opportunity_id_by_attachment_sha(self, sha256: str) -> str | None:
        row = self._conn.execute(
            "SELECT opportunity_id FROM source_documents WHERE sha256 = ? ORDER BY rowid LIMIT 1",
            (sha256,),
        ).fetchone()
        if row is None:
            return None
        return row["opportunity_id"]
