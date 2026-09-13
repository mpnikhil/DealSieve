"""Deterministic broker-diligence loop and approval boundary.

Models may propose questions, but every outbound message is classified and gated
here.  Information requests wait by default, follow-ups may use the approval
already granted to their thread, and any money or offer language always waits
for a fresh human approval.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any

from dealsieve.notifications import format_stalled_alert
from dealsieve.persistence import DuplicateNotification, Repo
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    CapexItem,
    Channel,
    DiligenceRequest,
    DocumentAnalysis,
    EventType,
    Notification,
    NotificationAction,
    Opportunity,
    OpportunityEvent,
    OutboundDraft,
    OutboundKind,
    RequestAnswer,
    now_utc,
)

_STRONG_OFFER_RE = re.compile(
    r"\b(?:loi|letter\s+of\s+intent|psa|purchase\s+agreement|bid|proposal)\b",
    re.I,
)
_CONTEXTUAL_OFFER_RE = re.compile(
    r"\b(?:our\s+offer|make\s+(?:an?\s+)?offer|submit(?:ting)?\s+(?:an?\s+)?offer|offer\s+of)\b",
    re.I,
)
_OFFER_WORD_RE = re.compile(r"\boffer\b", re.I)
_MONEY_AMOUNT_RE = re.compile(
    r"(?:"
    r"\$\s*\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|bn|thousand|million|billion)?(?![a-z0-9_])"
    r"|\b\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|bn|thousand|million|billion|dollars?|usd)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|billion)"
    r"(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|billion))*"
    r"\s+dollars?\b"
    r")",
    re.I,
)
_TERMS_RE = re.compile(
    r"\b(?:purchase\s+price|price|consideration|earnest\s+money|deposit|escrow|contingency|"
    r"inspection\s+period|closing\s+date|close\s+of\s+escrow|financing|seller\s+carry|terms?|"
    r"concessions?|credits?|discounts?|reduce|reduction)\b",
    re.I,
)
_TOPIC_TOKEN_RE = re.compile(r"[a-z0-9]+")

_FAMILIES: tuple[frozenset[str], ...] = (
    frozenset({"roof", "roofing", "membrane"}),
    frozenset({"hvac", "mechanical", "heating", "cooling"}),
    frozenset({"phase", "environmental", "esa"}),
    frozenset({"cam", "reconciliation", "opex", "operating"}),
    frozenset({"lease", "rollover", "estoppel"}),
    frozenset({"rent", "roll"}),
    frozenset({"survey", "title", "easement"}),
    frozenset({"zoning", "zone"}),
    frozenset({"parking", "paving", "pavement"}),
)


def classify_outbound_text(text: str) -> OutboundKind:
    """Classify text conservatively; offers and money outrank ordinary questions."""
    if _STRONG_OFFER_RE.search(text) or _CONTEXTUAL_OFFER_RE.search(text):
        return "offer"
    if _MONEY_AMOUNT_RE.search(text) or _TERMS_RE.search(text):
        return "credit_request"
    if _OFFER_WORD_RE.search(text):
        return "offer"
    return "information_request"


def _normalized_topic(topic: str) -> str:
    return " ".join(_TOPIC_TOKEN_RE.findall(topic.casefold()))


def build_requests(
    opportunity_id: str,
    items: list[dict[str, Any]],
    *,
    source: str | None,
) -> list[DiligenceRequest]:
    """Build draft requests, deduplicating topics within this batch."""
    requests: list[DiligenceRequest] = []
    seen: set[str] = set()
    for item in items:
        topic = str(item.get("topic", "")).strip()
        question = str(item.get("question", "")).strip()
        normalized = _normalized_topic(topic)
        if not normalized or not question or normalized in seen:
            continue
        seen.add(normalized)
        requests.append(
            DiligenceRequest(
                opportunity_id=opportunity_id,
                topic=topic,
                question=question,
                category=item.get("category") or "document",
                source_concern=str(item.get("source_concern") or source)
                if (item.get("source_concern") or source)
                else None,
            )
        )
    return requests


def _greeting(opportunity: Opportunity) -> str:
    if opportunity.broker_name:
        return f"Hi {opportunity.broker_name.strip().split()[0]},"
    return "Hi there,"


def compose_information_request(
    opp: Opportunity,
    requests: list[DiligenceRequest],
    policy: InvestmentPolicy,
    *,
    in_reply_to: str | None,
    original_subject: str,
) -> OutboundDraft:
    del original_subject  # Threading is carried by In-Reply-To/References, never by broker subject text.
    questions = [request.question for request in requests]
    numbered = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    body = (
        f"{_greeting(opp)}\n\n"
        f"We are reviewing {opp.display_name} and would appreciate the following diligence items:\n\n"
        f"{numbered}\n\n{policy.outreach.signature}"
    )
    return OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="information_request",
        to_email=opp.broker_email,
        subject=f"Diligence questions: {opp.display_name}",
        body=body,
        questions=questions,
        request_ids=[request.request_id for request in requests],
        requires_approval=not policy.outreach.auto_send_information_requests,
        in_reply_to_message_id=in_reply_to,
    )


def compose_follow_up(
    opp: Opportunity,
    requests: list[DiligenceRequest],
    policy: InvestmentPolicy,
    *,
    follow_up_number: int,
    in_reply_to: str | None,
    as_of: datetime | None = None,
) -> OutboundDraft:
    questions = [request.question for request in requests]
    numbered = "\n".join(
        f"{index}. Follow-up #{request.follow_up_count or follow_up_number} on "
        f"{request.topic}: {request.question}"
        for index, request in enumerate(requests, 1)
    )
    body = (
        f"{_greeting(opp)}\n\n"
        f"Following up on the diligence items below for {opp.display_name}:\n\n"
        f"{numbered}\n\n{policy.outreach.signature}"
    )
    request_ids = sorted(request.request_id for request in requests)
    tick_date = (as_of or now_utc()).date().isoformat()
    dedupe_source = f"{opp.opportunity_id}:{','.join(request_ids)}:{tick_date}"
    dedupe_key = sha256(dedupe_source.encode("utf-8")).hexdigest()[:16]
    return OutboundDraft(
        draft_id=f"drf_fu_{dedupe_key}",
        opportunity_id=opp.opportunity_id,
        kind="follow_up",
        to_email=opp.broker_email,
        subject=f"Diligence follow-up: {opp.display_name} [DS-{dedupe_key}]",
        body=body,
        questions=questions,
        request_ids=request_ids,
        requires_approval=not policy.outreach.auto_follow_up_approved_threads,
        in_reply_to_message_id=in_reply_to,
    )


def compose_credit_request(
    opp: Opportunity,
    amount: Decimal,
    rationale: str,
    policy: InvestmentPolicy,
    *,
    in_reply_to: str | None,
) -> OutboundDraft:
    amount = Decimal(amount)
    body = (
        f"{_greeting(opp)}\n\n"
        f"Based on our diligence, we would need a ${amount:,.0f} credit. {rationale.strip()}\n\n"
        f"{policy.outreach.signature}"
    )
    return OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="credit_request",
        to_email=opp.broker_email,
        subject=f"Diligence credit request: {opp.display_name}",
        body=body,
        requires_approval=True,
        in_reply_to_message_id=in_reply_to,
    )


def _event(
    repo: Repo,
    draft: OutboundDraft,
    kind: EventType,
    actor: Actor,
    summary: str,
    *,
    principal: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "draft_id": draft.draft_id,
        "kind": draft.kind,
        "request_ids": draft.request_ids,
    }
    if principal is not None:
        payload["principal"] = principal
    repo.append_event(
        OpportunityEvent(
            opportunity_id=draft.opportunity_id,
            type=kind,
            actor=actor,
            summary=summary,
            payload=payload,
        )
    )


def _post_send(
    draft: OutboundDraft,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    actor: Actor,
    sent_at: datetime,
    follow_up_reserved: bool = False,
) -> None:
    if draft.kind == "information_request":
        _event(repo, draft, EventType.DILIGENCE_REQUEST_SENT, actor, "Diligence request sent to broker")
    elif draft.kind == "follow_up":
        _event(repo, draft, EventType.DILIGENCE_FOLLOW_UP_SENT, actor, "Diligence follow-up sent to broker")
    _event(repo, draft, EventType.BROKER_MESSAGE_SENT, actor, f"Broker message sent: {draft.subject}")
    due_at = sent_at + timedelta(days=policy.outreach.follow_up_after_days)
    for request_id in draft.request_ids:
        request = repo.get_diligence_request(request_id)
        if request is None or request.status in {"answered", "stalled", "withdrawn"}:
            continue
        cadence: dict[str, Any] = {}
        if draft.kind == "follow_up" and not follow_up_reserved:
            cadence = {
                "follow_up_count": request.follow_up_count + 1,
                "last_follow_up_at": sent_at,
            }
        repo.update_diligence_request(
            request.model_copy(
                update={
                    "status": "sent",
                    "sent_at": request.sent_at or sent_at,
                    "due_at": due_at,
                    **cadence,
                }
            )
        )


def dispatch(
    draft: OutboundDraft,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
    actor: Actor = Actor.AGENT,
) -> OutboundDraft:
    """Screen, persist, and either hold or send a newly composed message."""
    existing = repo.get_draft(draft.draft_id)
    if existing is not None:
        return existing

    combined = "\n".join([draft.subject, draft.body, *draft.questions])
    kind_seen = classify_outbound_text(combined)
    if kind_seen in {"credit_request", "offer"}:
        blocked = draft.model_copy(update={"kind": kind_seen, "requires_approval": True, "status": "pending"})
        repo.store_draft(blocked)
        if draft.kind not in {"credit_request", "offer"}:
            _event(
                repo,
                blocked,
                EventType.OUTBOUND_BLOCKED,
                actor,
                f"Outbound message blocked by policy screen: {kind_seen}",
            )
        else:
            _event(
                repo,
                blocked,
                EventType.BROKER_DRAFT_CREATED,
                actor,
                f"Broker draft created: {blocked.subject}",
            )
        return blocked

    requires_approval = draft.requires_approval or draft.kind in policy.outreach.always_require_approval
    if requires_approval:
        pending = draft.model_copy(update={"requires_approval": True, "status": "pending"})
        repo.store_draft(pending)
        _event(
            repo, pending, EventType.BROKER_DRAFT_CREATED, actor, f"Broker draft created: {pending.subject}"
        )
        return pending

    # Persist a retryable transport intent before the external side effect. The internal
    # approved->sending CAS also prevents two concurrent ticks from delivering the same draft.
    staged = draft.model_copy(update={"requires_approval": False, "status": "approved"})
    repo.store_draft(staged)
    if not repo.transition_draft(staged.draft_id, "approved", "sending"):
        return repo.get_draft(staged.draft_id) or staged
    try:
        delivery_ref = outbox.send(staged)
    except Exception:
        if repo.transition_draft(staged.draft_id, "sending", "approved"):
            repo.update_draft(staged)
        repo.record_draft_delivery_failure(staged.draft_id)
        raise
    sent_at = now_utc()
    sent = staged.model_copy(
        update={
            "requires_approval": False,
            "status": "sent",
            "sent_at": sent_at,
            "delivery_ref": delivery_ref,
        }
    )
    repo.update_draft(sent)
    _post_send(
        sent,
        repo=repo,
        policy=policy,
        actor=actor,
        sent_at=sent_at,
        follow_up_reserved=sent.kind == "follow_up",
    )
    return sent


class DraftNotPending(ValueError):
    """Raised when a draft cannot be claimed for an explicit approval/send attempt."""


def acknowledge_opportunity(opportunity_id: str, *, repo: Repo, principal: str) -> Opportunity:
    """Clear the human-attention latch and append the auditable acknowledgement NOTE (F25)."""
    opportunity = repo.get_opportunity(opportunity_id)
    if opportunity is None:
        raise LookupError(f"No opportunity {opportunity_id!r}")
    acknowledged = opportunity.model_copy(update={"human_attention_required": False})
    acknowledged = repo.save_opportunity(acknowledged)
    repo.append_event(
        OpportunityEvent(
            opportunity_id=opportunity_id,
            type=EventType.NOTE,
            actor=Actor.HUMAN,
            summary=f"Acknowledged by {principal}",
            payload={"principal": principal},
        )
    )
    return acknowledged


def reject_draft(
    draft_id: str,
    *,
    repo: Repo,
    principal: str,
    reason: str | None = None,
) -> OutboundDraft:
    """CAS-reject one pending draft and withdraw its still-draft requests (F2/F3)."""
    draft = repo.get_draft(draft_id)
    if draft is None:
        raise LookupError(f"No draft {draft_id!r}")
    if not repo.transition_draft(draft_id, "pending", "rejected"):
        current = repo.get_draft(draft_id)
        current_status = current.status if current is not None else "missing"
        raise DraftNotPending(f"Draft {draft_id} is {current_status}; expected pending")
    rejected = draft.model_copy(update={"status": "rejected", "decided_at": now_utc()})
    repo.update_draft(rejected)
    _event(
        repo,
        rejected,
        EventType.HUMAN_REJECTED_DRAFT,
        Actor.HUMAN,
        f"Human rejected broker draft {draft_id}" + (f": {reason}" if reason else ""),
        principal=principal,
    )
    for request_id in rejected.request_ids:
        request = repo.get_diligence_request(request_id)
        if request is None or request.status != "draft":
            continue
        withdrawn = request.model_copy(update={"status": "withdrawn", "due_at": None})
        repo.update_diligence_request(withdrawn)
        repo.append_event(
            OpportunityEvent(
                opportunity_id=rejected.opportunity_id,
                type=EventType.NOTE,
                actor=Actor.HUMAN,
                summary=f"Diligence request withdrawn: {request.topic}",
                payload={
                    "draft_id": draft_id,
                    "request_id": request_id,
                    "principal": principal,
                    "reason": reason,
                },
            )
        )
    acknowledge_opportunity(rejected.opportunity_id, repo=repo, principal=principal)
    return rejected


def approve_and_send(
    draft_id: str,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
    principal: str = "human:explicit",
    acknowledge: bool = True,
) -> OutboundDraft:
    """Atomically claim an explicit approval, deliver once, and apply post-send effects."""
    draft = repo.get_draft(draft_id)
    if draft is None:
        raise LookupError(f"No draft {draft_id!r}")
    if draft.status == "sent":
        if acknowledge:
            acknowledge_opportunity(draft.opportunity_id, repo=repo, principal=principal)
        return draft
    initial_status = draft.status
    if initial_status not in {"pending", "approved"}:
        if initial_status == "rejected":
            raise DraftNotPending("a rejected draft cannot be approved and sent")
        raise DraftNotPending(f"Draft {draft_id} is {initial_status}; expected pending")

    combined = "\n".join([draft.subject, draft.body, *draft.questions])
    kind_seen = classify_outbound_text(combined)
    screened_kind = kind_seen if kind_seen in {"credit_request", "offer"} else draft.kind
    approved_at = draft.decided_at or now_utc()
    approved = draft.model_copy(
        update={
            "kind": screened_kind,
            "requires_approval": True,
            "status": "approved",
            "decided_at": approved_at,
        }
    )

    if initial_status == "pending":
        if not repo.transition_draft(draft_id, "pending", "approved"):
            current = repo.get_draft(draft_id)
            if current is not None and current.status == "sent":
                return current
            current_status = current.status if current is not None else "missing"
            raise DraftNotPending(f"Draft {draft_id} is {current_status}; expected pending")
        _event(
            repo,
            approved,
            EventType.HUMAN_APPROVED_DRAFT,
            Actor.HUMAN,
            f"Human approved broker draft {draft_id}",
            principal=principal,
        )
    if acknowledge:
        acknowledge_opportunity(draft.opportunity_id, repo=repo, principal=principal)

    if not repo.transition_draft(draft_id, "approved", "sending"):
        current = repo.get_draft(draft_id)
        if current is not None and current.status == "sent":
            return current
        current_status = current.status if current is not None else "missing"
        raise DraftNotPending(f"Draft {draft_id} is {current_status}; send already claimed")

    try:
        delivery_ref = outbox.send(approved)
    except Exception as exc:
        restored = repo.transition_draft(draft_id, "sending", "approved")
        if restored:
            repo.update_draft(approved)
        failures = repo.record_draft_delivery_failure(draft_id)
        repo.append_event(
            OpportunityEvent(
                opportunity_id=draft.opportunity_id,
                type=EventType.NOTE,
                actor=Actor.SYSTEM,
                summary=f"Broker draft delivery failed; explicit retry required: {draft.subject}",
                payload={
                    "draft_id": draft_id,
                    "error": str(exc),
                    "principal": principal,
                    "delivery_failures": failures,
                },
            )
        )
        raise
    sent_at = now_utc()
    sent = approved.model_copy(update={"status": "sent", "sent_at": sent_at, "delivery_ref": delivery_ref})
    repo.update_draft(sent)
    _post_send(sent, repo=repo, policy=policy, actor=Actor.HUMAN, sent_at=sent_at)
    return sent


def _topic_tokens(topic: str) -> set[str]:
    return set(_TOPIC_TOKEN_RE.findall(topic.casefold()))


def _topics_match(left: str, right: str) -> bool:
    left_tokens, right_tokens = _topic_tokens(left), _topic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if any(left_tokens & family and right_tokens & family for family in _FAMILIES):
        return True
    return len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens)) >= 0.5


def match_answers(
    requests: Iterable[DiligenceRequest], analysis: DocumentAnalysis
) -> list[tuple[DiligenceRequest, RequestAnswer]]:
    matches: list[tuple[DiligenceRequest, RequestAnswer]] = []
    for request in requests:
        for answer in analysis.answers:
            if _topics_match(request.topic, answer.request_topic):
                matches.append((request, answer))
                break
    return matches


def _evidence_for_topic(evidence_ids_by_topic: dict[str, Any], topic: str) -> list[str]:
    value = evidence_ids_by_topic.get(topic)
    if value is None:
        normalized = _normalized_topic(topic)
        value = next(
            (ids for key, ids in evidence_ids_by_topic.items() if _normalized_topic(str(key)) == normalized),
            [],
        )
    if isinstance(value, str):
        return [value]
    return list(value or [])


def apply_answers(
    matches: Iterable[tuple[DiligenceRequest, RequestAnswer]],
    analysis: DocumentAnalysis,
    *,
    repo: Repo,
    evidence_ids_by_topic: dict[str, Any],
) -> list[DiligenceRequest]:
    updated: list[DiligenceRequest] = []
    answered_at = now_utc()
    for request, answer in matches:
        changes: dict[str, Any] = {
            "answer_summary": answer.answer if answer.resolves else f"partial: {answer.answer}",
        }
        if answer.resolves:
            changes.update(
                status="answered",
                answered_at=answered_at,
                answered_by_document=analysis.filename,
                answer_evidence_ids=_evidence_for_topic(evidence_ids_by_topic, answer.request_topic),
                due_at=None,
            )
        revised = request.model_copy(update=changes)
        repo.update_diligence_request(revised)
        repo.append_event(
            OpportunityEvent(
                opportunity_id=request.opportunity_id,
                type=EventType.DILIGENCE_ANSWERED,
                summary=f"Diligence {'answered' if answer.resolves else 'partially answered'}: {request.topic}",
                payload={
                    "request_id": request.request_id,
                    "analysis_id": analysis.analysis_id,
                    "resolves": answer.resolves,
                },
            )
        )
        updated.append(revised)
    return updated


def immediate_capex_from(items: list[CapexItem], policy: InvestmentPolicy) -> Decimal:
    total = Decimal("0")
    for item in items:
        if item.urgency not in policy.capex.count_as_immediate:
            continue
        total += item.midpoint if policy.capex.use_midpoint else Decimal(item.high)
    return total


@dataclass
class SweepReport:
    as_of: datetime
    drafts_retried: int = 0
    drafts_sent: list[str] | None = None
    draft_failures: list[str] | None = None
    notifications_retried: int = 0
    notifications_delivered: list[str] | None = None
    notification_failures: list[str] | None = None
    failed_inbound_messages: list[dict[str, str | None]] | None = None

    def __post_init__(self) -> None:
        if self.drafts_sent is None:
            self.drafts_sent = []
        if self.draft_failures is None:
            self.draft_failures = []
        if self.notifications_delivered is None:
            self.notifications_delivered = []
        if self.notification_failures is None:
            self.notification_failures = []
        if self.failed_inbound_messages is None:
            self.failed_inbound_messages = []

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return asdict(self)


_MAX_DRAFT_DELIVERY_FAILURES = 3


def _deliver_sweep_notification(
    notification: Notification,
    *,
    repo: Repo,
    notifier: Any,
    report: SweepReport,
) -> None:
    report.notifications_retried += 1
    try:
        delivery_ref = notifier.send(notification)
    except Exception as exc:
        report.notification_failures.append(f"{notification.notification_id}: {type(exc).__name__}: {exc}")
        return
    delivered = notification.model_copy(update={"delivered": True, "delivery_ref": delivery_ref})
    repo.update_notification(delivered)
    repo.append_event(
        OpportunityEvent(
            opportunity_id=notification.opportunity_id,
            type=EventType.HUMAN_NOTIFIED,
            actor=Actor.SYSTEM,
            summary=f"Human notified: {notification.title}",
            payload={
                "notification_id": notification.notification_id,
                "kind": notification.kind,
                "delivery_ref": delivery_ref,
                "resumed": True,
            },
        )
    )
    report.notifications_delivered.append(notification.notification_id)


def _escalate_draft_failure(
    draft: OutboundDraft,
    *,
    repo: Repo,
    notifier: Any,
    report: SweepReport,
) -> None:
    if not repo.mark_draft_retry_escalated(draft.draft_id):
        return
    notification = Notification(
        opportunity_id=draft.opportunity_id,
        kind="status_update",
        channel=getattr(notifier, "channel", Channel.MANUAL),
        title="Outbound mail is not going out",
        body=(
            f"Draft {draft.draft_id} could not be delivered after "
            f"{_MAX_DRAFT_DELIVERY_FAILURES} attempts: {draft.subject}"
        ),
        actions=[NotificationAction(label="Review", action="review")],
        dedupe_key=f"{draft.opportunity_id}:outbound_delivery_failed:{draft.draft_id}",
    )
    try:
        repo.store_notification(notification)
    except DuplicateNotification:
        return
    _deliver_sweep_notification(notification, repo=repo, notifier=notifier, report=report)


def sweep(
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
    notifier: Any,
    as_of: datetime | None = None,
) -> SweepReport:
    """Resume recoverable delivery intents without re-running inbound processing (F8/F20/F22).

    Each invocation makes at most one transport attempt per approved draft and per previously
    undelivered notification. Draft transport stops after three recorded failures and creates one
    ``status_update`` notification. Failed inbound messages are reported but never claimed here.
    """
    tick = as_of or now_utc()
    report = SweepReport(as_of=tick)
    # Snapshot first: an escalation created below whose notifier call fails is intentionally retried
    # on the next scheduled sweep, not hammered again in this same invocation.
    undelivered = [
        item for item in repo.list_notifications() if not item.delivered and item.delivery_ref is None
    ]

    for draft in repo.list_drafts(status="approved"):
        failures = repo.draft_delivery_failures(draft.draft_id)
        if failures >= _MAX_DRAFT_DELIVERY_FAILURES:
            _escalate_draft_failure(draft, repo=repo, notifier=notifier, report=report)
            continue
        report.drafts_retried += 1
        try:
            sent = approve_and_send(
                draft.draft_id,
                repo=repo,
                policy=policy,
                outbox=outbox,
                principal="system:sweep",
                acknowledge=False,
            )
        except Exception as exc:
            failures = repo.draft_delivery_failures(draft.draft_id)
            report.draft_failures.append(f"{draft.draft_id}: {type(exc).__name__}: {exc}")
            if failures >= _MAX_DRAFT_DELIVERY_FAILURES:
                _escalate_draft_failure(draft, repo=repo, notifier=notifier, report=report)
        else:
            if sent.status == "sent":
                report.drafts_sent.append(sent.draft_id)

    for notification in undelivered:
        # It may have been delivered through another path since the snapshot was taken.
        current = repo.get_notification(notification.notification_id)
        if current is None or current.delivered or current.delivery_ref is not None:
            continue
        _deliver_sweep_notification(current, repo=repo, notifier=notifier, report=report)

    report.failed_inbound_messages.extend(
        {
            "message_id": message.message_id,
            "error": error,
        }
        for message, error in repo.list_failed_messages()
    )
    return report


@dataclass
class FollowUpReport:
    as_of: datetime
    follow_ups_sent: int = 0
    stalled: int = 0
    requests_followed_up: list[DiligenceRequest] | None = None
    notifications: list[str] | None = None
    sweep_report: SweepReport | None = None

    def __post_init__(self) -> None:
        if self.requests_followed_up is None:
            self.requests_followed_up = []
        if self.notifications is None:
            self.notifications = []

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return asdict(self)


def run_follow_ups(
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
    notifier: Any,
    as_of: datetime | None = None,
) -> FollowUpReport:
    """Advance all due requests one cadence step, with per-opportunity batching."""
    tick = as_of or now_utc()
    report = FollowUpReport(
        as_of=tick,
        sweep_report=sweep(repo, policy, outbox, notifier, tick),
    )
    due = [
        request
        for request in repo.list_diligence_requests()
        if request.status in {"sent", "overdue"} and request.due_at is not None and request.due_at <= tick
    ]
    grouped: dict[str, list[DiligenceRequest]] = defaultdict(list)
    for request in due:
        grouped[request.opportunity_id].append(request)

    for opportunity_id, requests in grouped.items():
        try:
            _run_opportunity_follow_ups(
                opportunity_id,
                requests,
                repo=repo,
                policy=policy,
                outbox=outbox,
                notifier=notifier,
                tick=tick,
                report=report,
            )
        except Exception as exc:
            # One malformed opportunity or failing integration cannot abort the scheduled tick.
            repo.append_event(
                OpportunityEvent(
                    opportunity_id=opportunity_id,
                    type=EventType.NOTE,
                    actor=Actor.SYSTEM,
                    summary="Diligence tick failed for this opportunity; other deals continued",
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
    return report


def _run_opportunity_follow_ups(
    opportunity_id: str,
    requests: list[DiligenceRequest],
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
    notifier: Any,
    tick: datetime,
    report: FollowUpReport,
) -> None:
    opp = repo.get_opportunity(opportunity_id)
    if opp is None:
        return

    eligible = [request for request in requests if request.follow_up_count < policy.outreach.max_follow_ups]
    eligible_ids = sorted(request.request_id for request in eligible)
    held = next(
        (
            draft
            for draft in repo.list_drafts(status="pending", opportunity_id=opportunity_id)
            if draft.kind == "follow_up" and sorted(draft.request_ids) == eligible_ids
        ),
        None,
    )
    if held is not None:
        eligible = []
    reserved: list[tuple[DiligenceRequest, DiligenceRequest]] = []
    for snapshot in eligible:
        # ``overdue`` is only a projection. Rollback snapshots must retain stored ``sent``.
        snapshot = snapshot.model_copy(update={"status": "sent"})
        if not repo.reserve_follow_up(snapshot.request_id, snapshot.follow_up_count, tick):
            continue
        current = repo.get_diligence_request(snapshot.request_id)
        if current is not None and current.status == "sent":
            reserved.append((snapshot, current))

    if reserved:
        claimed = [current for _, current in reserved]
        number = min(request.follow_up_count for request in claimed)
        previous = next(
            (
                draft
                for draft in reversed(repo.list_drafts(opportunity_id=opportunity_id))
                if draft.status == "sent"
            ),
            None,
        )
        follow_up = compose_follow_up(
            opp,
            claimed,
            policy,
            follow_up_number=number,
            in_reply_to=previous.in_reply_to_message_id if previous else None,
            as_of=tick,
        )
        try:
            sent = dispatch(follow_up, repo=repo, policy=policy, outbox=outbox)
        except Exception as exc:
            for snapshot, reserved_request in reserved:
                current = repo.get_diligence_request(snapshot.request_id)
                if (
                    current is not None
                    and current.status == "sent"
                    and current.follow_up_count == reserved_request.follow_up_count
                    and current.last_follow_up_at == tick
                ):
                    repo.update_diligence_request(snapshot)
            repo.append_event(
                OpportunityEvent(
                    opportunity_id=opportunity_id,
                    type=EventType.NOTE,
                    actor=Actor.SYSTEM,
                    summary="Diligence follow-up delivery failed; reservations rolled back",
                    payload={
                        "draft_id": follow_up.draft_id,
                        "request_ids": [request.request_id for request in claimed],
                        "error": str(exc),
                    },
                )
            )
            return
        if sent.status == "sent":
            report.follow_ups_sent += 1
            for request in claimed:
                revised = repo.get_diligence_request(request.request_id) or request
                if revised.status == "sent":
                    revised = revised.model_copy(
                        update={
                            "due_at": tick + timedelta(days=policy.outreach.follow_up_after_days),
                        }
                    )
                    repo.update_diligence_request(revised)
                    report.requests_followed_up.append(revised)
        else:
            # A policy screen or approval policy held the composed follow-up. It was not
            # transported, so release every cadence reservation for a later explicit tick.
            for snapshot, reserved_request in reserved:
                current = repo.get_diligence_request(snapshot.request_id)
                if (
                    current is not None
                    and current.status == "sent"
                    and current.follow_up_count == reserved_request.follow_up_count
                    and current.last_follow_up_at == tick
                ):
                    repo.update_diligence_request(snapshot)

    stalled_requests: list[DiligenceRequest] = []
    for request in requests:
        if request.follow_up_count < policy.outreach.max_follow_ups:
            continue
        current = repo.get_diligence_request(request.request_id)
        if current is None or current.status not in {"sent", "overdue"}:
            continue
        revised = current.model_copy(update={"status": "stalled", "due_at": None})
        repo.update_diligence_request(revised)
        repo.append_event(
            OpportunityEvent(
                opportunity_id=opportunity_id,
                type=EventType.DILIGENCE_STALLED,
                summary=f"Diligence stalled after {request.follow_up_count} follow-ups: {request.topic}",
                payload={"request_id": request.request_id, "topic": request.topic},
            )
        )
        stalled_requests.append(revised)
    report.stalled += len(stalled_requests)

    if not stalled_requests:
        return

    notification = format_stalled_alert(
        opp,
        stalled_requests,
        channel=getattr(notifier, "channel", Channel.MANUAL),
    ).model_copy(
        update={
            "dedupe_key": f"{opportunity_id}:diligence_stalled:{','.join(sorted(r.request_id for r in stalled_requests))}"
        }
    )
    try:
        repo.store_notification(notification)
    except DuplicateNotification:
        return
    try:
        delivery_ref = notifier.send(notification)
    except Exception:
        # The persisted undelivered row is the retry intent consumed by the next sweep.
        return
    delivered = notification.model_copy(update={"delivered": True, "delivery_ref": delivery_ref})
    repo.update_notification(delivered)
    report.notifications.append(delivered.notification_id)


__all__ = [
    "DraftNotPending",
    "FollowUpReport",
    "SweepReport",
    "acknowledge_opportunity",
    "apply_answers",
    "approve_and_send",
    "build_requests",
    "classify_outbound_text",
    "compose_credit_request",
    "compose_follow_up",
    "compose_information_request",
    "dispatch",
    "immediate_capex_from",
    "match_answers",
    "reject_draft",
    "run_follow_ups",
    "sweep",
]
