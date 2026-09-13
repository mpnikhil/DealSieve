"""Deterministic, no-model text for the memory hooks in ``dealsieve.diligence``.

Every function here composes a one-line, human-readable sentence from contract objects already in
hand (a draft, an opportunity, a broker outcome) and hands it to ``MemoryStore.record``. Nothing here
calls a model: the sentences are plain string formatting so the same inputs always produce the same
memory, in tests and in the demo.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from dealsieve.memory.store import MemoryStore
from dealsieve.schemas import MemoryEvent, MemoryHit, Opportunity, OutboundDraft, Property

_AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

_DOCUMENT_LABELS: dict[str, str] = {
    "inspection_report": "a condition report",
    "roof_report": "a roof report",
    "phase_i": "a Phase I report",
    "cam_statement": "a CAM statement",
    "rent_roll": "a rent roll",
    "lease": "a lease",
    "offering_memorandum": "an offering memorandum",
    "other": "a document",
}

_ALERT_LABELS: dict[str, str] = {
    "threshold_crossed": "just-became-investable",
    "fell_below_threshold": "fell-below",
    "diligence_stalled": "diligence-stalled",
    "structural_dead": "structural-dead",
    "status_update": "status-update",
    "draft_pending": "draft-pending",
}


def investor_namespace(principal: str) -> str:
    return f"investor/{principal}"


def broker_namespace(email: str) -> str:
    return f"broker/{email.strip().lower()}"


def document_kind_label(document_type: str) -> str:
    return _DOCUMENT_LABELS.get(document_type, "a document")


def alert_kind_label(notification_kind: str) -> str:
    return _ALERT_LABELS.get(notification_kind, notification_kind.replace("_", "-"))


def deal_label(opportunity: Opportunity) -> str:
    if opportunity.deal_number is not None:
        return f"deal #{opportunity.deal_number}"
    return opportunity.display_name


def _credit_amount(draft: OutboundDraft) -> Decimal | None:
    match = _AMOUNT_RE.search(draft.body)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _draft_topics(store: MemoryStore, draft: OutboundDraft) -> list[str]:
    repo = getattr(store, "repo", None)
    if repo is None:
        return []
    topics: list[str] = []
    for request_id in draft.request_ids:
        request = repo.get_diligence_request(request_id)
        if request is not None:
            topics.append(request.topic)
    return topics


def _capex_label(opportunity: Opportunity) -> str:
    working_values = opportunity.working_values
    if working_values is None or not working_values.capex_items:
        return "capex"
    items = [item for item in working_values.capex_items if item.urgency == "immediate"]
    items = items or list(working_values.capex_items)
    top = max(items, key=lambda item: item.midpoint)
    first_word = top.item.strip().split()[0].lower() if top.item.strip() else ""
    return f"{first_word} capex" if first_word else "capex"


def _approved_credit_detail(opportunity: Opportunity) -> str | None:
    parts: list[str] = []
    working_values = opportunity.working_values
    if working_values is not None and working_values.immediate_capex:
        parts.append(f"{_capex_label(opportunity)} ${working_values.immediate_capex:,.0f}")
    status = getattr(opportunity.status, "value", opportunity.status)
    viability = opportunity.viability
    if viability is not None and viability.distance_pct is not None:
        parts.append(f"{status}, {viability.distance_pct * 100:.1f}% under ask")
    else:
        parts.append(str(status))
    return "; ".join(parts) if parts else None


def _ensure_period(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _kind_label(draft: OutboundDraft) -> str:
    return "follow-up" if draft.kind == "follow_up" else "information request"


def _decision_text(
    store: MemoryStore,
    draft: OutboundDraft,
    opportunity: Opportunity,
    outcome: Literal["approved", "rejected"],
    reason: str | None,
) -> str:
    deal = deal_label(opportunity)
    broker = f" to {draft.to_email}" if draft.to_email else ""
    verb = "Approved" if outcome == "approved" else "Rejected"

    if draft.kind == "credit_request":
        amount = _credit_amount(draft)
        amount_text = f"${amount:,.0f}" if amount is not None else "a"
        text = f"{verb} a {amount_text} credit request{broker} on {deal}"
        if outcome == "approved":
            detail = _approved_credit_detail(opportunity)
            text += f" ({detail})." if detail else "."
        else:
            text += "."
            if reason:
                text += f" Reason: {_ensure_period(reason)}"
        return text

    if draft.kind == "offer":
        text = f"{verb} the offer{broker} on {deal}."
        if outcome == "rejected" and reason:
            text += f" Reason: {_ensure_period(reason)}"
        return text

    topics = _draft_topics(store, draft)
    topic_text = f" (topics: {', '.join(topics)})" if topics else ""
    text = f"{verb} the {_kind_label(draft)} on {deal}{topic_text}."
    if outcome == "rejected" and reason:
        text += f" Reason: {_ensure_period(reason)}"
    return text


def remember_decision(
    store: MemoryStore,
    *,
    principal: str,
    draft: OutboundDraft,
    opportunity: Opportunity,
    outcome: Literal["approved", "rejected"],
    reason: str | None = None,
) -> MemoryEvent:
    """Record a human's approve/reject decision on an outbound draft."""
    text = _decision_text(store, draft, opportunity, outcome, reason)
    payload: dict[str, object] = {
        "draft_id": draft.draft_id,
        "draft_kind": draft.kind,
        "outcome": outcome,
        "topics": _draft_topics(store, draft),
    }
    amount = _credit_amount(draft)
    if amount is not None:
        payload["amount"] = str(amount)
    if reason:
        payload["reason"] = reason
    event = MemoryEvent(
        namespace=investor_namespace(principal),
        kind="decision",
        actor=principal,
        opportunity_id=opportunity.opportunity_id,
        deal_number=opportunity.deal_number,
        broker_email=draft.to_email,
        text=text,
        payload=payload,
    )
    return store.record(event)


def remember_broker_outcome(
    store: MemoryStore,
    *,
    broker_email: str,
    opportunity: Opportunity,
    kind: Literal["answered", "stalled", "replied"],
    detail: str,
) -> MemoryEvent:
    """Record deterministic, ledger-derived broker behaviour. ``detail`` is the sentence tail after
    the broker's email, already composed by the caller from data it has on hand
    (e.g. "answered 'Roof age' with a condition report 3 days after the request")."""
    text = f"{broker_email} {_ensure_period(detail)}"
    event = MemoryEvent(
        namespace=broker_namespace(broker_email),
        kind="broker",
        actor=broker_email,
        opportunity_id=opportunity.opportunity_id,
        deal_number=opportunity.deal_number,
        broker_email=broker_email,
        text=text,
        payload={"outcome": kind},
    )
    return store.record(event)


def remember_alert_acknowledgement(
    store: MemoryStore,
    *,
    principal: str,
    opportunity: Opportunity,
    notification_id: str,
    notification_kind: str,
    action: Literal["ignore", "review"],
) -> MemoryEvent:
    """Record a human dismissing or opening an alert (the notification ignore/review path)."""
    label = alert_kind_label(notification_kind)
    deal = deal_label(opportunity)
    text = (
        f"Ignored the {label} alert on {deal}."
        if action == "ignore"
        else f"Opened the {label} alert for review on {deal}."
    )
    event = MemoryEvent(
        namespace=investor_namespace(principal),
        kind="alert",
        actor=principal,
        opportunity_id=opportunity.opportunity_id,
        deal_number=opportunity.deal_number,
        text=text,
        payload={"notification_id": notification_id, "notification_kind": notification_kind, "action": action},
    )
    return store.record(event)


def recall_for_deal(
    store: MemoryStore,
    opportunity: Opportunity,
    property: Property,  # noqa: A002 -- matches the CONTRACTS.md signature.
    *,
    topics: list[str],
    broker_email: str | None,
) -> list[MemoryHit]:
    """Top-5 memories relevant to this deal: investor decisions everywhere, plus this broker's record."""
    query = " ".join(part for part in (property.property_type, property.city, " ".join(topics)) if part)
    namespaces = [investor_namespace("*")]
    if broker_email:
        namespaces.append(broker_namespace(broker_email))
    if not query.strip():
        return []
    return store.recall(query, namespaces=namespaces, limit=5)


__all__ = [
    "alert_kind_label",
    "broker_namespace",
    "deal_label",
    "document_kind_label",
    "investor_namespace",
    "recall_for_deal",
    "remember_alert_acknowledgement",
    "remember_broker_outcome",
    "remember_decision",
]
