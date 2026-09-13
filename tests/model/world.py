"""A small TLA+-style executable world for DealSieve.

The actions deliberately use the product entry points and a real SQLite repository.  The only
test doubles are recording delivery boundaries with a controllable, one-shot failure mode.
"""

from __future__ import annotations

import random
import re
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from dealsieve.agents.tools import aggregate_capex_items
from dealsieve.diligence import approve_and_send, classify_outbound_text, immediate_capex_from, run_follow_ups
from dealsieve.ingestion import parse_eml
from dealsieve.notifications import RecordingNotifier
from dealsieve.outbound import RecordingOutbox
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    EventType,
    Notification,
    OpportunityStatus,
    OutboundDraft,
    ProcessingOutcome,
)

FIXTURE_NAMES = (
    "01_initial_offer",
    "02_price_drop",
    "03_structural_single_tenant",
    "04_obvious_economic_failure",
    "05_inspection_report",
)

LEGAL_REQUEST_STATUSES = {"draft", "sent", "answered", "overdue", "stalled", "withdrawn"}
KNOWN_NOTIFICATION_KINDS = {
    "threshold_crossed",
    "fell_below_threshold",
    "diligence_stalled",
    "structural_dead",
    "status_update",
}
DECISION_NOTIFICATION_KINDS = {"threshold_crossed", "fell_below_threshold"}


@lru_cache(maxsize=4)
def _fixture_messages(fixtures_dir: str) -> tuple[tuple[str, Any], ...]:
    """Parse immutable fixture messages once; PDF/MIME decoding otherwise dominates 150 worlds."""
    root = Path(fixtures_dir)
    return tuple((name, parse_eml(root / "emails" / f"{name}.eml")) for name in FIXTURE_NAMES)


class FlakyNotifier(RecordingNotifier):
    """Recording notifier that can fail exactly one transport attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False
        self._lock = threading.Lock()

    def send(self, notification: Notification) -> str | None:
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("injected notifier failure")
            return super().send(notification)


class FlakyOutbox(RecordingOutbox):
    """Recording outbox that can fail exactly one transport attempt."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False
        self._lock = threading.Lock()

    def send(self, message: OutboundDraft) -> str:
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("injected outbox failure")
            return super().send(message)


@dataclass(frozen=True)
class Counts:
    events: int
    runs: int
    drafts: int
    notifications: int
    analyses: int

    def delta(self, before: Counts) -> Counts:
        return Counts(*(new - old for new, old in zip(self.as_tuple(), before.as_tuple(), strict=True)))

    def as_tuple(self) -> tuple[int, int, int, int, int]:
        return (self.events, self.runs, self.drafts, self.notifications, self.analyses)


@dataclass(frozen=True)
class StateView:
    opportunities: tuple[Any, ...]
    events: dict[str, tuple[Any, ...]]
    runs: dict[str, tuple[Any, ...]]
    drafts: tuple[Any, ...]
    notifications: tuple[Any, ...]
    requests: tuple[Any, ...]
    analyses: dict[str, tuple[Any, ...]]

    @property
    def counts(self) -> Counts:
        return Counts(
            events=sum(map(len, self.events.values())),
            runs=sum(map(len, self.runs.values())),
            drafts=len(self.drafts),
            notifications=len(self.notifications),
            analyses=sum(map(len, self.analyses.values())),
        )


class TraceInvariantError(AssertionError):
    """One or more state-machine properties failed after a transition."""


class World:
    """One shared repository/policy/outbox/notifier state driven by trace actions."""

    def __init__(
        self,
        *,
        db_path: Path,
        fixtures_dir: Path,
        policy: InvestmentPolicy,
        seed: int | str,
    ) -> None:
        self.db_path = db_path
        self.fixtures_dir = fixtures_dir
        self.policy = policy
        self.seed = seed
        self.rng = random.Random(str(seed))
        self.repo = Repo(db_path)
        self.repo.init_schema()
        self.notifier = FlakyNotifier()
        self.outbox = FlakyOutbox()
        self.clock = datetime.now(UTC)
        self.tick_times: list[datetime] = []
        self.history: list[str] = []
        self.messages = dict(_fixture_messages(str(fixtures_dir.resolve())))
        self.last_injected_name: str | None = None
        self.last_draft_id: str | None = None
        self.last_outcome: ProcessingOutcome | None = None
        self._last_process_raised: BaseException | None = None
        self._last_process_delivery_delta = 0
        self._last_action_error: str | None = None
        self._notifier_armed_before_action = False
        self._outbox_armed_before_action = False
        self._outbox_deliveries_before_action = 0
        self._state = self._load_state()
        self._before_counts = self._state.counts
        self._after_counts = self._before_counts
        self._precompleted = False
        self._pre_message_state: str | None = None
        self._post_message_state: str | None = None
        self._processed_message_id: str | None = None
        self._immutable_events: dict[str, tuple[Any, ...]] = {}
        self._immutable_runs: dict[str, tuple[Any, ...]] = {}
        self._answered_followups: dict[str, int] = {}
        self._max_followup_due: dict[str, datetime] = {}
        self._api_reject: Callable[..., Any] | None = None
        self.confirmed_defects: list[str] = []
        self._confirmed_defect_keys: set[str] = set()

    # ------------------------------------------------------------------ transition driver

    def execute(self, action: str, argument: str | int | None = None) -> None:
        """Apply one alphabet action and check every safety invariant immediately."""
        label = self._label(action, argument)
        self.history.append(label)
        self._before_counts = self._state.counts
        self._after_counts = self._before_counts
        self._last_process_raised = None
        self._last_process_delivery_delta = 0
        self._last_action_error = None
        self._notifier_armed_before_action = self.notifier.fail_next
        self._outbox_armed_before_action = self.outbox.fail_next
        self._outbox_deliveries_before_action = len(self.outbox.sent)
        self._precompleted = False
        self._pre_message_state = None
        self._post_message_state = None
        self._processed_message_id = None

        try:
            if action == "inject":
                assert isinstance(argument, str)
                self._process(argument)
            elif action == "duplicate":
                if self.last_injected_name is not None:
                    self._process(self.last_injected_name)
            elif action == "retry_last":
                if self.last_injected_name is not None:
                    self._process(self.last_injected_name)
            elif action == "approve":
                self.approve()
            elif action == "reject":
                self.reject()
            elif action == "tick":
                assert isinstance(argument, int)
                self.tick(argument)
            elif action == "notifier_fail_next":
                self.notifier.fail_next = True
            elif action == "outbox_fail_next":
                self.outbox.fail_next = True
            elif action == "concurrent_ticks":
                self.concurrent_ticks()
            elif action == "concurrent_approve":
                self.concurrent_approve()
            else:  # pragma: no cover - a test author error
                raise ValueError(f"unknown action {action!r}")
        except Exception as exc:  # actions must refuse safely; expected delivery failures are classified below
            if not self._expected_action_exception(action, exc):
                self._last_action_error = f"{action} raised {type(exc).__name__}: {exc}"

        self._state = self._load_state()
        self._after_counts = self._state.counts
        self._remember_request_due_dates()
        violations = self._record_confirmed_defects(self.check_invariants())
        if violations:
            raise TraceInvariantError(self.failure_message(violations))

    @staticmethod
    def _label(action: str, argument: str | int | None) -> str:
        return f"{action}({argument})" if argument is not None else f"{action}()"

    @staticmethod
    def _expected_action_exception(action: str, exc: Exception) -> bool:
        if action in ("approve", "concurrent_approve") and isinstance(exc, RuntimeError) and "injected outbox" in str(exc):
            return True
        if action == "reject" and type(exc).__name__ == "HTTPException":
            return True
        return False

    def _process(self, name: str) -> None:
        message = self.messages[name]
        self.last_injected_name = name
        self._processed_message_id = message.message_id
        self._pre_message_state = self._message_state(message.message_id)
        self._precompleted = self._pre_message_state == "completed"
        delivered_before = len(self.notifier.sent)
        try:
            self.last_outcome = process_inbound(
                message,
                repo=self.repo,
                policy=self.policy,
                notifier=self.notifier,
                outbox=self.outbox,
                script=str(self.fixtures_dir / "scripted" / f"{name}.json"),
            )
        except BaseException as exc:  # S11 records the violation instead of losing the trace
            self._last_process_raised = exc
            self.last_outcome = None
        self._last_process_delivery_delta = len(self.notifier.sent) - delivered_before
        self._post_message_state = self._message_state(message.message_id)

    def _message_state(self, message_id: str) -> str | None:
        """Read pipeline bookkeeping not exposed by Repo without claiming/mutating the message."""
        status = getattr(self.repo, "message_status", None)
        if status is not None:
            return status(message_id)
        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT status FROM inbound_messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            return str(row[0]) if row is not None else None
        finally:
            connection.close()

    def approve(self, draft_id: str | None = None) -> None:
        candidates = [draft for draft in self.repo.list_drafts() if draft.status == "pending"]
        if not candidates:
            candidates = [draft for draft in self.repo.list_drafts() if draft.status == "approved"]
        target = self.repo.get_draft(draft_id) if draft_id else (self.rng.choice(candidates) if candidates else None)
        if target is None:
            return
        self.last_draft_id = target.draft_id
        approve_and_send(
            target.draft_id,
            repo=self.repo,
            policy=self.policy,
            outbox=self.outbox,
            principal="human:model-trace",
        )

    def _reject_endpoint(self) -> Callable[..., Any]:
        """Return the API route callable, invoking its closure directly (never over HTTP)."""
        if self._api_reject is None:
            from dealsieve.api.app import create_app

            app = create_app(repo=self.repo, policy=self.policy, notifier=self.notifier)
            route = next(item for item in app.routes if getattr(item, "path", None) == "/api/drafts/{draft_id}/reject")
            self._api_reject = route.endpoint
        return self._api_reject

    def reject(self, draft_id: str | None = None) -> None:
        pending = [draft for draft in self.repo.list_drafts() if draft.status == "pending"]
        target_id = draft_id or (self.rng.choice(pending).draft_id if pending else self.last_draft_id)
        if target_id is None:
            return
        self.last_draft_id = target_id
        self._reject_endpoint()(
            target_id,
            principal="human:model-trace",
            repo=self.repo,
        )

    def tick(self, days: int) -> None:
        if not 0 <= days <= 10:
            raise ValueError("tick days must be 0 (same time) or 1..10")
        self.clock += timedelta(days=days)
        self.tick_times.append(self.clock)
        run_follow_ups(
            repo=self.repo,
            policy=self.policy,
            outbox=self.outbox,
            notifier=self.notifier,
            as_of=self.clock,
        )

    def concurrent_ticks(self) -> None:
        self.clock += timedelta(days=self.policy.outreach.follow_up_after_days + 1)
        self.tick_times.extend([self.clock, self.clock])
        barrier = threading.Barrier(2)

        def worker() -> None:
            repo = Repo(self.db_path)
            barrier.wait()
            run_follow_ups(
                repo=repo,
                policy=self.policy,
                outbox=self.outbox,
                notifier=self.notifier,
                as_of=self.clock,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for future in futures:
                future.result()

    def concurrent_approve(self) -> None:
        pending = [draft for draft in self.repo.list_drafts() if draft.status == "pending"]
        if not pending:
            return
        target = self.rng.choice(pending)
        self.last_draft_id = target.draft_id
        barrier = threading.Barrier(2)

        def worker() -> None:
            repo = Repo(self.db_path)
            barrier.wait()
            try:
                approve_and_send(
                    target.draft_id,
                    repo=repo,
                    policy=self.policy,
                    outbox=self.outbox,
                    principal="human:model-concurrent",
                )
            except ValueError:
                pass  # exactly one compare-and-swap winner is expected

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for future in futures:
                future.result()

    # ------------------------------------------------------------------ snapshots and diagnostics

    def _load_state(self) -> StateView:
        opportunities = tuple(self.repo.list_opportunities())
        return StateView(
            opportunities=opportunities,
            events={opp.opportunity_id: tuple(self.repo.list_events(opp.opportunity_id)) for opp in opportunities},
            runs={
                opp.opportunity_id: tuple(self.repo.list_underwriting_runs(opp.opportunity_id))
                for opp in opportunities
            },
            drafts=tuple(self.repo.list_drafts()),
            notifications=tuple(self.repo.list_notifications()),
            requests=tuple(self.repo.list_diligence_requests()),
            analyses={
                opp.opportunity_id: tuple(self.repo.list_document_analyses(opp.opportunity_id))
                for opp in opportunities
            },
        )

    def _remember_request_due_dates(self) -> None:
        for request in self._state.requests:
            if (
                request.follow_up_count == self.policy.outreach.max_follow_ups
                and request.due_at is not None
            ):
                self._max_followup_due[request.request_id] = request.due_at

    def failure_message(self, violations: list[str]) -> str:
        rendered = "\n".join(f"  - {item}" for item in violations)
        return f"seed={self.seed!r}\nactions={self.history!r}\nviolations:\n{rendered}"

    def check_invariants(self, *, final: bool = False) -> list[str]:
        checks = [
            self.invariant_s1_append_only,
            self.invariant_s2_status_history,
            self.invariant_s3_notifications,
            self.invariant_s4_outbound_approval,
            self.invariant_s5_diligence,
            self.invariant_s6_capex_basis,
            self.invariant_s7_classification,
            self.invariant_s8_retries,
            self.invariant_s9_policy_version,
            self.invariant_s10_price_change_event,
            self.invariant_s11_processing_outcome,
        ]
        violations = [item for check in checks for item in check()]
        if final:
            violations.extend(self.invariant_s12_liveness())
        return violations

    def assert_liveness(self) -> None:
        violations = self._record_confirmed_defects(self.check_invariants(final=True))
        if violations:
            raise TraceInvariantError(self.failure_message(violations))

    def _record_confirmed_defects(self, violations: list[str]) -> list[str]:
        """Keep confirmed checks executing while allowing pytest to report their traces as xfail."""
        unexpected: list[str] = []
        inspection = "inject(05_inspection_report)"
        initial = "inject(01_initial_offer)"
        confirmed_order = (
            inspection in self.history
            and initial in self.history
            and self.history.index(inspection) < self.history.index(initial)
        )
        confirmed_capex_shape = any(
            opp.working_values is not None
            and opp.working_values.immediate_capex == 0
            and immediate_capex_from(
                aggregate_capex_items(self._state.analyses[opp.opportunity_id])[0], self.policy
            )
            == 90000
            for opp in self._state.opportunities
        )
        for violation in violations:
            # Confirmed minimal trace: inspection_report before initial_offer.  The analysis and
            # its verified $90k immediate capex are durable, but later initialization of working
            # values does not fold those existing analyses into the basis.
            if confirmed_order and confirmed_capex_shape and violation.startswith("S6 "):
                finding = self.failure_message([violation])
                key = "S6:historic-analysis-not-applied-on-working-values-initialization"
                if key not in self._confirmed_defect_keys:
                    self._confirmed_defect_keys.add(key)
                    self.confirmed_defects.append(finding)
            elif self._pre_message_state == "completed" and violation.startswith("S8 completed message"):
                finding = self.failure_message([violation])
                key = "S8:tri-state-message-claim-treated-as-boolean"
                if key not in self._confirmed_defect_keys:
                    self._confirmed_defect_keys.add(key)
                    self.confirmed_defects.append(finding)
            elif self._pre_message_state == "completed" and violation.startswith(
                "S8 completed-message replay changed counts"
            ):
                finding = self.failure_message([violation])
                key = "S8:tri-state-message-claim-treated-as-boolean"
                if key not in self._confirmed_defect_keys:
                    self._confirmed_defect_keys.add(key)
                    self.confirmed_defects.append(finding)
            else:
                unexpected.append(violation)
        return unexpected

    # ------------------------------------------------------------------ S1 .. S12

    def invariant_s1_append_only(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            oid = opp.opportunity_id
            events = self._state.events[oid]
            runs = self._state.runs[oid]
            seqs = [event.seq for event in events]
            if seqs != list(range(1, len(events) + 1)):
                violations.append(f"S1 {oid}: event seq observed {seqs}, expected 1..{len(events)}")
            previous_events = self._immutable_events.get(oid, ())
            previous_runs = self._immutable_runs.get(oid, ())
            if events[: len(previous_events)] != previous_events:
                violations.append(f"S1 {oid}: earlier event rows were rewritten")
            if runs[: len(previous_runs)] != previous_runs:
                violations.append(f"S1 {oid}: earlier underwriting runs were rewritten")
            completed = sum(event.type == EventType.UNDERWRITING_COMPLETED for event in events)
            if len(runs) != completed:
                violations.append(f"S1 {oid}: {len(runs)} runs vs {completed} UNDERWRITING_COMPLETED events")
            self._immutable_events[oid] = events
            self._immutable_runs[oid] = runs
        return violations

    def invariant_s2_status_history(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            events = self._state.events[opp.opportunity_id]
            runs = self._state.runs[opp.opportunity_id]
            if runs and opp.status != runs[-1].status:
                violations.append(f"S2 {opp.opportunity_id}: opportunity={opp.status}, latest run={runs[-1].status}")
            expected: list[tuple[str, str]] = []
            previous = OpportunityStatus.NEW
            for run in runs:
                if run.status != previous:
                    expected.append((previous.value, run.status.value))
                previous = run.status
            actual = [
                (str(event.payload.get("from")), str(event.payload.get("to")))
                for event in events
                if event.type == EventType.STATUS_CHANGED
            ]
            if actual != expected:
                violations.append(f"S2 {opp.opportunity_id}: STATUS_CHANGED={actual}, expected {expected}")
        return violations

    def invariant_s3_notifications(self) -> list[str]:
        violations: list[str] = []
        delivered = [item for item in self._state.notifications if item.delivered]
        keys = [item.dedupe_key for item in delivered]
        duplicates = [key for key, count in Counter(keys).items() if count > 1]
        if duplicates:
            violations.append(f"S3 delivered duplicate dedupe_key(s): {duplicates}")
        sent_counts = Counter(item.notification_id for item in self.notifier.sent)
        if any(count > 1 for count in sent_counts.values()):
            violations.append(f"S3 notifier delivered a notification more than once: {dict(sent_counts)}")

        for notification in self._state.notifications:
            if notification.kind not in KNOWN_NOTIFICATION_KINDS:
                violations.append(f"S3 {notification.notification_id}: unknown kind {notification.kind}")
            if notification.kind not in DECISION_NOTIFICATION_KINDS:
                continue
            parts = (notification.dedupe_key or "").rsplit(":", 2)
            run_id = parts[-2] if len(parts) == 3 else None
            runs = self._state.runs[notification.opportunity_id]
            index = next((i for i, run in enumerate(runs) if run.run_id == run_id), None)
            if index is None:
                violations.append(f"S3 {notification.notification_id}: dedupe key has no underwriting run")
                continue
            run = runs[index]
            predecessor = runs[index - 1].status if index else OpportunityStatus.NEW
            if notification.kind == "threshold_crossed" and not (
                run.status == OpportunityStatus.REVIEW and predecessor != OpportunityStatus.REVIEW
            ):
                violations.append(
                    f"S3 {notification.notification_id}: threshold_crossed for {predecessor}->{run.status}"
                )
            if notification.kind == "fell_below_threshold" and not (
                predecessor == OpportunityStatus.REVIEW and run.status != OpportunityStatus.REVIEW
            ):
                violations.append(
                    f"S3 {notification.notification_id}: fell_below_threshold for {predecessor}->{run.status}"
                )
            if {predecessor, run.status} <= {OpportunityStatus.WATCH, OpportunityStatus.NEAR}:
                violations.append(f"S3 {notification.notification_id}: notified on WATCH<->NEAR transition")
            if run.status == OpportunityStatus.DEAD or predecessor == OpportunityStatus.DEAD:
                violations.append(f"S3 {notification.notification_id}: notified on DEAD transition")
        return violations

    def invariant_s4_outbound_approval(self) -> list[str]:
        violations: list[str] = []
        drafts = self._state.drafts
        events_by_opp = {
            opp.opportunity_id: self._state.events[opp.opportunity_id] for opp in self._state.opportunities
        }
        sent = [draft for draft in drafts if draft.status == "sent"]
        if len(self.outbox.sent) != len(sent):
            violations.append(f"S4 outbox.sent={len(self.outbox.sent)}, sent drafts={len(sent)}")
        outbox_failure_consumed = self._outbox_armed_before_action and not self.outbox.fail_next
        if self.history[-1].startswith("approve") and outbox_failure_consumed:
            target = self.repo.get_draft(self.last_draft_id) if self.last_draft_id else None
            if target is None or target.status not in {"pending", "approved"}:
                status = target.status if target is not None else "missing"
                violations.append(f"S4 failed approval left draft {status}, expected approved/pending")
            if len(self.outbox.sent) != self._outbox_deliveries_before_action:
                violations.append("S4 failed outbox attempt was recorded as delivered")
        delivery_refs = [draft.delivery_ref for draft in sent]
        if any(ref is None for ref in delivery_refs) or len(set(delivery_refs)) != len(delivery_refs):
            violations.append(f"S4 sent delivery_refs are missing or non-unique: {delivery_refs}")
        message_ids = [f"<dealsieve.{sha256(draft.draft_id.encode()).hexdigest()}@local>" for draft in sent]
        if len(set(message_ids)) != len(message_ids):
            violations.append(f"S4 sent Message-IDs are non-unique: {message_ids}")

        sent_info = [draft for draft in sent if draft.kind == "information_request"]
        for draft in sent:
            combined = "\n".join([draft.body, *draft.questions])
            classified = classify_outbound_text(combined)
            expected = "information_request" if draft.kind == "follow_up" else draft.kind
            if classified != expected:
                violations.append(f"S4 {draft.draft_id}: kind={draft.kind}, classifier={classified}")
            approval_required = draft.kind in {"credit_request", "offer"} or (
                draft.kind == "information_request" and not self.policy.outreach.auto_send_information_requests
            )
            if approval_required:
                approvals = [
                    event
                    for event in events_by_opp[draft.opportunity_id]
                    if event.type == EventType.HUMAN_APPROVED_DRAFT
                    and event.actor == Actor.HUMAN
                    and event.payload.get("draft_id") == draft.draft_id
                    and draft.sent_at is not None
                    and event.occurred_at <= draft.sent_at
                ]
                if not approvals:
                    violations.append(f"S4 {draft.draft_id}: sent without prior human approval event")
            if draft.kind == "follow_up":
                missing = [
                    request_id
                    for request_id in draft.request_ids
                    if not any(request_id in info.request_ids for info in sent_info)
                ]
                if missing:
                    violations.append(f"S4 {draft.draft_id}: follow-up carries never-sent requests {missing}")
        return violations

    def invariant_s5_diligence(self) -> list[str]:
        violations: list[str] = []
        requests = self._state.requests
        for request in requests:
            if request.status not in LEGAL_REQUEST_STATUSES:
                violations.append(f"S5 {request.request_id}: illegal status {request.status}")
            if request.follow_up_count > self.policy.outreach.max_follow_ups:
                violations.append(
                    f"S5 {request.request_id}: follow_up_count={request.follow_up_count} exceeds max"
                )
            if request.status == "stalled" and request.follow_up_count != self.policy.outreach.max_follow_ups:
                violations.append(f"S5 {request.request_id}: stalled at count {request.follow_up_count}")
            if (request.due_at is not None) != (request.status == "sent"):
                violations.append(f"S5 {request.request_id}: status={request.status}, due_at={request.due_at}")
            if request.answered_at is not None:
                frozen = self._answered_followups.setdefault(request.request_id, request.follow_up_count)
                if request.follow_up_count != frozen:
                    violations.append(
                        f"S5 {request.request_id}: answered follow-up count changed {frozen}->{request.follow_up_count}"
                    )

        seen: set[tuple[str, int]] = set()
        requests_by_id = {request.request_id: request for request in requests}
        for draft in [item for item in self._state.drafts if item.kind == "follow_up"]:
            shared_match = re.search(r"Following up #(\d+)", draft.body)
            for request_id in draft.request_ids:
                request = requests_by_id.get(request_id)
                item_match = (
                    re.search(rf"Follow-up #(\d+) on {re.escape(request.topic)}:", draft.body)
                    if request is not None
                    else None
                )
                match = item_match or shared_match
                if match is None:
                    violations.append(
                        f"S5 {draft.draft_id}: follow-up number missing for request {request_id}"
                    )
                    continue
                number = int(match.group(1))
                key = (request_id, number)
                if key in seen:
                    violations.append(f"S5 duplicate follow-up pair {key}")
                seen.add(key)
        return violations

    def invariant_s6_capex_basis(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            if opp.working_values is None:
                continue
            analyses = self._state.analyses[opp.opportunity_id]
            items, _ = aggregate_capex_items(analyses)
            expected = immediate_capex_from(items, self.policy)
            observed = opp.working_values.immediate_capex
            if observed != expected:
                violations.append(f"S6 {opp.opportunity_id}: immediate_capex={observed}, expected {expected}")
            runs = self._state.runs[opp.opportunity_id]
            if runs:
                financing = runs[-1].financing
                if financing.immediate_capex != expected:
                    violations.append(
                        f"S6 {runs[-1].run_id}: financing immediate_capex="
                        f"{financing.immediate_capex}, expected {expected}"
                    )
                expected_basis = financing.purchase_price + expected
                if financing.all_in_basis != expected_basis:
                    violations.append(
                        f"S6 {runs[-1].run_id}: all_in_basis={financing.all_in_basis}, expected {expected_basis}"
                    )
        return violations

    def invariant_s7_classification(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            runs = self._state.runs[opp.opportunity_id]
            if not runs:
                continue
            run = runs[-1]
            frontier = run.viability
            if run.status in {OpportunityStatus.WATCH, OpportunityStatus.NEAR}:
                if frontier.max_viable_price is None or frontier.max_viable_price >= run.inputs.asking_price:
                    violations.append(
                        f"S7 {run.run_id}: {run.status} frontier={frontier.max_viable_price}, ask={run.inputs.asking_price}"
                    )
                near = frontier.distance_pct is not None and (
                    frontier.distance_pct <= self.policy.classification.near_threshold_pct
                )
                if (run.status == OpportunityStatus.NEAR) != near:
                    violations.append(
                        f"S7 {run.run_id}: status={run.status}, distance={frontier.distance_pct}, near={near}"
                    )
            elif run.status == OpportunityStatus.REVIEW:
                failed = [gate.gate for gate in run.gates if not gate.passed]
                if failed:
                    violations.append(f"S7 {run.run_id}: REVIEW with failed gates {failed}")
            elif run.status == OpportunityStatus.DEAD:
                if not frontier.structural_failures or frontier.max_viable_price is not None:
                    violations.append(
                        f"S7 {run.run_id}: DEAD structural={frontier.structural_failures}, frontier={frontier.max_viable_price}"
                    )
        return violations

    def invariant_s8_retries(self) -> list[str]:
        violations: list[str] = []
        if self._processed_message_id is None:
            return violations
        delta = self._after_counts.delta(self._before_counts)
        if self._precompleted and delta.as_tuple() != (0, 0, 0, 0, 0):
            violations.append(f"S8 completed-message replay changed counts by {delta}")
        if self._precompleted and self.last_outcome and "duplicate" not in self.last_outcome.summary.casefold():
            violations.append("S8 completed message was not reported as a duplicate")
        if self.history[-1].startswith("retry_last") and not self._precompleted and self.last_outcome:
            if "duplicate" in self.last_outcome.summary.casefold():
                violations.append("S8 failed message retry was incorrectly treated as a completed duplicate")
            if self.last_outcome.run_id is not None:
                if self._post_message_state != "completed" and not any(
                    not item.delivered for item in self._state.notifications
                ):
                    violations.append(
                        f"S8 successful retry left message {self._post_message_state}, expected completed"
                    )
                limits = Counts(events=20, runs=1, drafts=2, notifications=1, analyses=1)
                if any(value < 0 for value in delta.as_tuple()) or any(
                    value > limit for value, limit in zip(delta.as_tuple(), limits.as_tuple(), strict=True)
                ):
                    violations.append(f"S8 successful retry added more than one processing can add: {delta}")
        return violations

    def invariant_s9_policy_version(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            for run in self._state.runs[opp.opportunity_id]:
                if run.policy_version != self.policy.policy_version:
                    violations.append(
                        f"S9 {run.run_id}: policy={run.policy_version}, expected {self.policy.policy_version}"
                    )
        return violations

    def invariant_s10_price_change_event(self) -> list[str]:
        violations: list[str] = []
        for opp in self._state.opportunities:
            events = self._state.events[opp.opportunity_id]
            underwriting_events = [
                (index, event)
                for index, event in enumerate(events)
                if event.type == EventType.UNDERWRITING_COMPLETED
            ]
            run_by_id = {run.run_id: run for run in self._state.runs[opp.opportunity_id]}
            for (left_index, left), (right_index, right) in zip(
                underwriting_events, underwriting_events[1:], strict=False
            ):
                left_run = run_by_id.get(str(left.payload.get("run_id")))
                right_run = run_by_id.get(str(right.payload.get("run_id")))
                if left_run is None or right_run is None:
                    continue
                if left_run.inputs.asking_price == right_run.inputs.asking_price:
                    continue
                between = events[left_index + 1 : right_index]
                if not any(event.type == EventType.ASKING_PRICE_CHANGED for event in between):
                    violations.append(
                        f"S10 {left_run.run_id}->{right_run.run_id}: asking price "
                        f"{left_run.inputs.asking_price}->{right_run.inputs.asking_price} without event"
                    )
        return violations

    def invariant_s11_processing_outcome(self) -> list[str]:
        violations: list[str] = []
        if self._last_action_error:
            violations.append(f"S11 {self._last_action_error}")
        if self._last_process_raised is not None:
            exc = self._last_process_raised
            violations.append(f"S11 process_inbound raised {type(exc).__name__}: {exc}")
        if self._processed_message_id is not None and self.last_outcome is not None:
            expected = self._last_process_delivery_delta > 0
            if self.last_outcome.notified_human != expected:
                violations.append(
                    f"S11 notified_human={self.last_outcome.notified_human}, delivered during call={expected}"
                )
            notifier_failure_consumed = self._notifier_armed_before_action and not self.notifier.fail_next
            if notifier_failure_consumed:
                undelivered = [item for item in self._state.notifications if not item.delivered]
                if self._post_message_state != "failed" or not undelivered:
                    violations.append(
                        "S11 notifier failure did not leave a failed message and stored undelivered alert: "
                        f"message={self._post_message_state}, undelivered={len(undelivered)}"
                    )
        return violations

    def invariant_s12_liveness(self) -> list[str]:
        violations: list[str] = []
        notifications = self._state.notifications
        for opp in self._state.opportunities:
            runs = self._state.runs[opp.opportunity_id]
            previous = OpportunityStatus.NEW
            for run in runs:
                if run.status == OpportunityStatus.REVIEW and previous != OpportunityStatus.REVIEW:
                    matching = [
                        item
                        for item in notifications
                        if item.opportunity_id == opp.opportunity_id
                        and item.kind == "threshold_crossed"
                        and item.dedupe_key == f"{opp.opportunity_id}:{run.run_id}:threshold_crossed"
                        and item.delivered
                    ]
                    pending = any(
                        item.dedupe_key == f"{opp.opportunity_id}:{run.run_id}:threshold_crossed"
                        and not item.delivered
                        for item in notifications
                    )
                    if not pending and len(matching) != 1:
                        violations.append(
                            f"S12 {run.run_id}: REVIEW entry has {len(matching)} delivered threshold notifications"
                        )
                previous = run.status

        approvals = {
            event.payload.get("draft_id")
            for opp in self._state.opportunities
            for event in self._state.events[opp.opportunity_id]
            if event.type == EventType.HUMAN_APPROVED_DRAFT
        }
        outbox_counts = Counter(draft.draft_id for draft in self.outbox.sent)
        for draft in self._state.drafts:
            if draft.kind != "information_request" or draft.draft_id not in approvals:
                continue
            if draft.status == "approved":
                continue  # a failed transport is intentionally retryable by a later approve action
            if draft.status != "sent" or outbox_counts[draft.draft_id] != 1:
                violations.append(
                    f"S12 {draft.draft_id}: approved information request status={draft.status}, "
                    f"deliveries={outbox_counts[draft.draft_id]}"
                )

        for request in self._state.requests:
            if request.follow_up_count != self.policy.outreach.max_follow_ups:
                continue
            maxed_due = self._max_followup_due.get(request.request_id)
            ticked_after_due = maxed_due is not None and any(tick >= maxed_due for tick in self.tick_times)
            if not ticked_after_due:
                continue
            if request.status != "stalled":
                violations.append(f"S12 {request.request_id}: maxed request is {request.status}, expected stalled")
            stalled = [
                item
                for item in notifications
                if item.opportunity_id == request.opportunity_id
                and item.kind == "diligence_stalled"
                and item.delivered
            ]
            if len(stalled) != 1:
                violations.append(
                    f"S12 {request.opportunity_id}: {len(stalled)} delivered diligence_stalled notifications"
                )
        return violations


def random_action(rng: random.Random) -> tuple[str, str | int | None]:
    """Choose one deterministic transition from the complete model alphabet."""
    actions = (
        ("inject", "duplicate", "retry_last", "concurrent_ticks", "concurrent_approve")
        + ("approve",) * 4
        + ("reject",) * 4
        + ("tick",) * 8
        + ("notifier_fail_next",) * 4
        + ("outbox_fail_next",) * 4
    )
    action = rng.choice(actions)
    if action == "inject":
        return action, rng.choice(FIXTURE_NAMES)
    if action == "tick":
        return action, 0 if rng.random() < 0.2 else rng.randint(1, 10)
    return action, None
