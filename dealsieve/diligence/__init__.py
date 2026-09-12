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
    Opportunity,
    OpportunityEvent,
    OutboundDraft,
    OutboundKind,
    RequestAnswer,
    now_utc,
)

_OFFER_RE = re.compile(r"\b(?:loi|letter\s+of\s+intent|purchase\s+agreement|our\s+offer)\b", re.I)
_CREDIT_RE = re.compile(
    r"(?:\$\s*\d|\b(?:credit|reduce\s+the\s+price|price\s+reduction|would\s+the\s+seller\s+accept|"
    r"we\s+would\s+pay|discount|concession|terms)\b)",
    re.I,
)
_TOPIC_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PREFIX_RE = re.compile(r"^(?:\s*re\s*:\s*)+", re.I)

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
    if _OFFER_RE.search(text):
        return "offer"
    if _CREDIT_RE.search(text):
        return "credit_request"
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
                source_concern=str(item.get("source_concern") or source) if (item.get("source_concern") or source) else None,
            )
        )
    return requests


def _reply_subject(subject: str) -> str:
    return f"Re: {_PREFIX_RE.sub('', subject).strip()}"


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
        subject=_reply_subject(original_subject),
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
) -> OutboundDraft:
    questions = [request.question for request in requests]
    numbered = "\n".join(f"{index}. {question}" for index, question in enumerate(questions, 1))
    body = (
        f"{_greeting(opp)}\n\n"
        f"Following up #{follow_up_number} on the diligence items below for {opp.display_name}:\n\n"
        f"{numbered}\n\n{policy.outreach.signature}"
    )
    return OutboundDraft(
        opportunity_id=opp.opportunity_id,
        kind="follow_up",
        to_email=opp.broker_email,
        subject=_reply_subject(opp.display_name),
        body=body,
        questions=questions,
        request_ids=[request.request_id for request in requests],
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
        subject=_reply_subject(opp.display_name),
        body=body,
        requires_approval=True,
        in_reply_to_message_id=in_reply_to,
    )


def _event(repo: Repo, draft: OutboundDraft, kind: EventType, actor: Actor, summary: str) -> None:
    repo.append_event(
        OpportunityEvent(
            opportunity_id=draft.opportunity_id,
            type=kind,
            actor=actor,
            summary=summary,
            payload={"draft_id": draft.draft_id, "kind": draft.kind, "request_ids": draft.request_ids},
        )
    )


def _post_send(
    draft: OutboundDraft,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    actor: Actor,
    sent_at: datetime,
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
        repo.update_diligence_request(
            request.model_copy(update={"status": "sent", "sent_at": request.sent_at or sent_at, "due_at": due_at})
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
    combined = "\n".join([draft.body, *draft.questions])
    kind_seen = classify_outbound_text(combined)
    if draft.kind in ("information_request", "follow_up") and kind_seen != "information_request":
        blocked = draft.model_copy(update={"kind": kind_seen, "requires_approval": True, "status": "pending"})
        repo.store_draft(blocked)
        _event(repo, blocked, EventType.OUTBOUND_BLOCKED, actor, f"Outbound message blocked by policy screen: {kind_seen}")
        return blocked

    requires_approval = draft.requires_approval or draft.kind in policy.outreach.always_require_approval
    if requires_approval:
        pending = draft.model_copy(update={"requires_approval": True, "status": "pending"})
        repo.store_draft(pending)
        _event(repo, pending, EventType.BROKER_DRAFT_CREATED, actor, f"Broker draft created: {pending.subject}")
        return pending

    sent_at = now_utc()
    delivery_ref = outbox.send(draft)
    sent = draft.model_copy(
        update={"requires_approval": False, "status": "sent", "sent_at": sent_at, "delivery_ref": delivery_ref}
    )
    repo.store_draft(sent)
    _post_send(sent, repo=repo, policy=policy, actor=actor, sent_at=sent_at)
    return sent


def approve_and_send(
    draft_id: str,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    outbox: Any,
) -> OutboundDraft:
    """Record explicit approval, deliver, then perform every request/event side effect."""
    draft = repo.get_draft(draft_id)
    if draft is None:
        raise LookupError(f"No draft {draft_id!r}")
    if draft.status == "sent":
        return draft
    if draft.status == "rejected":
        raise ValueError("a rejected draft cannot be approved and sent")
    approved_at = now_utc()
    approved = draft.model_copy(update={"status": "approved", "decided_at": approved_at})
    repo.update_draft(approved)
    _event(repo, approved, EventType.HUMAN_APPROVED_DRAFT, Actor.HUMAN, f"Human approved broker draft {draft_id}")
    delivery_ref = outbox.send(approved)
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
class FollowUpReport:
    as_of: datetime
    follow_ups_sent: int = 0
    stalled: int = 0
    requests_followed_up: list[DiligenceRequest] | None = None
    notifications: list[str] | None = None

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
    report = FollowUpReport(as_of=tick)
    due = [
        request
        for request in repo.list_diligence_requests()
        if request.status in {"sent", "overdue"} and request.due_at is not None and request.due_at <= tick
    ]
    grouped: dict[str, list[DiligenceRequest]] = defaultdict(list)
    for request in due:
        grouped[request.opportunity_id].append(request)

    for opportunity_id, requests in grouped.items():
        opp = repo.get_opportunity(opportunity_id)
        if opp is None:
            continue
        if all(request.follow_up_count < policy.outreach.max_follow_ups for request in requests):
            number = max(request.follow_up_count for request in requests) + 1
            previous = next(
                (draft for draft in reversed(repo.list_drafts(opportunity_id=opportunity_id)) if draft.status == "sent"),
                None,
            )
            follow_up = compose_follow_up(
                opp,
                requests,
                policy,
                follow_up_number=number,
                in_reply_to=previous.in_reply_to_message_id if previous else None,
            )
            sent = dispatch(follow_up, repo=repo, policy=policy, outbox=outbox)
            if sent.status == "sent":
                report.follow_ups_sent += 1
                for request in requests:
                    revised = repo.get_diligence_request(request.request_id) or request
                    revised = revised.model_copy(
                        update={
                            "follow_up_count": request.follow_up_count + 1,
                            "last_follow_up_at": tick,
                            "due_at": tick + timedelta(days=policy.outreach.follow_up_after_days),
                        }
                    )
                    repo.update_diligence_request(revised)
                    report.requests_followed_up.append(revised)
            continue

        stalled_requests: list[DiligenceRequest] = []
        for request in requests:
            revised = request.model_copy(update={"status": "stalled", "due_at": None})
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
            continue
        delivery_ref = notifier.send(notification)
        delivered = notification.model_copy(update={"delivered": True, "delivery_ref": delivery_ref})
        repo.update_notification(delivered)
        report.notifications.append(delivered.notification_id)

    return report


__all__ = [
    "FollowUpReport",
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
    "run_follow_ups",
]
