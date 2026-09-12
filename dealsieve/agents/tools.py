"""The deterministic tools the acquisition agent may call.

Design rule: **the model decides what to say, the tools decide what is true.** Every gate lives in
Python here, not in the prompt. The model cannot underwrite, cannot invent a status, cannot notify a
human without a real threshold crossing, and cannot draft a broker message without a skeptic review.
If the model skips a step, `dealsieve.pipeline`'s safety net performs it as the SYSTEM actor.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from strands import tool

from dealsieve.evidence.reconcile import MissingInputs, reconcile
from dealsieve.identity.resolver import extract_identity_keys, normalize_address, resolve
from dealsieve.notifications import format_threshold_alert
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    EventType,
    ExtractedClaims,
    InboundMessage,
    Notification,
    Opportunity,
    OpportunityEvent,
    OpportunityStatus,
    OutboundDraft,
    Property,
    SkepticReport,
    UnderwritingResult,
)
from dealsieve.underwriting import run_underwriting

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dealsieve.notifications import Notifier
    from dealsieve.persistence import Repo

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- session


@dataclass
class ProcessingSession:
    """Mutable state shared by every tool call for one inbound message."""

    repo: Repo
    policy: InvestmentPolicy
    notifier: Notifier
    message: InboundMessage
    model_backend: str

    # Only meaningful for the scripted backend; forwarded to the skeptic agent's model.
    script: str | None = None

    # -- mutable state -----------------------------------------------------
    opportunity_id: str | None = None
    created: bool = False
    status_before: OpportunityStatus | None = None
    run_before: UnderwritingResult | None = None
    run_after: UnderwritingResult | None = None
    threshold_crossed: bool = False
    skeptic_report: SkepticReport | None = None
    notification: Notification | None = None
    draft: OutboundDraft | None = None
    events_created: list[str] = field(default_factory=list)
    claims_recorded: bool = False
    errors: list[str] = field(default_factory=list)

    # -- bookkeeping used to build prompts and trigger references ----------
    claims: ExtractedClaims | None = None
    last_event_id: str | None = None

    def append_event(
        self,
        event_type: EventType,
        summary: str,
        payload: dict[str, Any] | None = None,
        *,
        actor: Actor = Actor.AGENT,
    ) -> OpportunityEvent:
        """Append an immutable event and remember its id. Every event goes through here."""
        if self.opportunity_id is None:
            raise ValueError("cannot append an event before an opportunity exists")
        stored = self.repo.append_event(
            OpportunityEvent(
                opportunity_id=self.opportunity_id,
                type=event_type,
                actor=actor,
                source_message_id=self.message.message_id,
                summary=summary,
                payload=payload or {},
            )
        )
        self.events_created.append(stored.event_id)
        self.last_event_id = stored.event_id
        return stored

    def opportunity(self) -> Opportunity | None:
        if self.opportunity_id is None:
            return None
        return self.repo.get_opportunity(self.opportunity_id)


# --------------------------------------------------------------------------- helpers


def _money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"${Decimal(value):,.0f}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{Decimal(value) * 100:.2f}%"


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _display_name(claims: ExtractedClaims, message: InboundMessage) -> str:
    if claims.address_line:
        parts = [claims.address_line]
        if claims.city:
            parts.append(claims.city)
        if claims.state:
            parts.append(claims.state)
        return ", ".join(parts)
    if message.subject:
        return message.subject.strip()
    return f"Untitled opportunity ({message.message_id})"


def _policy_yaml(policy: InvestmentPolicy) -> str:
    try:
        return Path(policy.source_path).read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        return ""


# --------------------------------------------------------------------------- record_claims


def perform_record_claims(
    session: ProcessingSession, claims: ExtractedClaims | dict[str, Any], *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Resolve identity, persist evidence, reconcile into WorkingValues, append events.

    Accepts a dict as well as an instance: Strands' `@tool` decorator validates a Pydantic
    parameter against the model (which is what produces the rich input schema) but hands the
    function `validated.model_dump()`, i.e. a plain dict. See `make_tools`.
    """
    if not isinstance(claims, ExtractedClaims):
        claims = ExtractedClaims.model_validate(claims)
    repo = session.repo
    message = session.message
    session.claims = claims

    if not repo.message_exists(message.message_id):
        repo.store_inbound_message(message)

    keys = extract_identity_keys(message, claims)
    resolution = resolve(keys, repo)

    created = False
    if resolution.opportunity_id:
        opportunity_id = resolution.opportunity_id
        opp = repo.get_opportunity(opportunity_id)
        if opp is None:  # pragma: no cover - repo inconsistency
            raise ValueError(f"resolver returned unknown opportunity {opportunity_id}")
        session.status_before = opp.status
    else:
        normalized = normalize_address(
            claims.address_line, claims.city, claims.state, claims.postal_code
        ) or f"UNRESOLVED:{message.message_id}"
        prop = repo.upsert_property(
            Property(
                canonical_address=claims.address_line or _display_name(claims, message),
                normalized_address=normalized,
                city=claims.city,
                state=claims.state,
                postal_code=claims.postal_code,
                apn=claims.apn,
                building_sqft=claims.building_sqft,
                property_type=claims.property_type,
            )
        )
        opp = repo.create_opportunity(
            Opportunity(
                property_id=prop.property_id,
                display_name=_display_name(claims, message),
                status=OpportunityStatus.NEW,
                broker_email=message.sender,
                broker_name=message.sender_name,
                broker_property_ref=claims.broker_property_ref,
                listing_url=claims.listing_url,
            )
        )
        opportunity_id = opp.opportunity_id
        created = True
        session.status_before = None

    session.opportunity_id = opportunity_id
    session.created = created

    # --- events, in contract order ---------------------------------------
    session.append_event(
        EventType.MESSAGE_RECEIVED,
        f"{message.channel.value} message from {message.sender or 'unknown sender'}"
        + (f": {message.subject}" if message.subject else ""),
        {
            "message_id": message.message_id,
            "channel": message.channel.value,
            "sender": message.sender,
            "subject": message.subject,
            "attachments": [a.filename for a in message.attachments],
        },
        actor=actor,
    )

    for attachment in message.attachments:
        repo.store_document(
            opportunity_id, message.message_id, attachment.filename, attachment.sha256, attachment.text
        )
        session.append_event(
            EventType.DOCUMENT_ADDED,
            f"Document attached: {attachment.filename}",
            {
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "sha256": attachment.sha256,
                "size_bytes": attachment.size_bytes,
            },
            actor=actor,
        )

    if created:
        session.append_event(
            EventType.DEAL_DISCOVERED,
            f"Deal #{opp.deal_number} discovered: {opp.display_name}",
            {"deal_number": opp.deal_number, "display_name": opp.display_name},
            actor=actor,
        )

    if resolution.needs_human and resolution.ambiguous_candidates:
        best = resolution.ambiguous_candidates[0]
        candidate_ref = best.get("deal_number") or best.get("opportunity_id")
        session.append_event(
            EventType.NOTE,
            f"possible duplicate of #{candidate_ref} (confidence {resolution.confidence:.2f})",
            {"candidates": _jsonable(resolution.ambiguous_candidates), "confidence": resolution.confidence},
            actor=actor,
        )

    if claims.evidence:
        repo.store_evidence(opportunity_id, claims.evidence)

    session.append_event(
        EventType.CLAIMS_EXTRACTED,
        f"Claims extracted from {message.message_id}"
        + (" (price change)" if claims.is_price_change else ""),
        {
            "asking_price": str(claims.asking_price) if claims.asking_price is not None else None,
            "stated_noi": str(claims.stated_noi) if claims.stated_noi is not None else None,
            "is_price_change": claims.is_price_change,
            "evidence_count": len(claims.evidence),
            "missing_fields": list(claims.missing_fields),
        },
        actor=actor,
    )

    # --- reconcile --------------------------------------------------------
    try:
        working, changes = reconcile(opp.working_values, claims)
    except MissingInputs as exc:
        session.errors.append(f"missing inputs: {', '.join(exc.missing)}")
        session.append_event(
            EventType.NOTE,
            f"Cannot underwrite yet: missing {', '.join(exc.missing)}",
            {"missing": list(exc.missing)},
            actor=actor,
        )
        repo.link_message_to_opportunity(message.message_id, opportunity_id)
        return {
            "opportunity_id": opportunity_id,
            "deal_number": opp.deal_number,
            "created": created,
            "error": str(exc),
            "missing": list(exc.missing),
        }

    for change in changes:
        session.append_event(change.type, change.summary, _jsonable(change.payload), actor=actor)

    opp.working_values = working
    opp.current_asking_price = working.asking_price
    if message.sender and not opp.broker_email:
        opp.broker_email = message.sender
    if message.sender_name and not opp.broker_name:
        opp.broker_name = message.sender_name
    if claims.broker_property_ref:
        opp.broker_property_ref = claims.broker_property_ref
    if claims.listing_url:
        opp.listing_url = claims.listing_url
    opp = repo.save_opportunity(opp)

    repo.link_message_to_opportunity(message.message_id, opportunity_id)
    session.claims_recorded = True

    return {
        "opportunity_id": opportunity_id,
        "deal_number": opp.deal_number,
        "created": created,
        "status": opp.status.value,
        "changes": [change.summary for change in changes],
        "conflicts": list(working.conflicts),
        "missing_fields": list(claims.missing_fields),
    }


# --------------------------------------------------------------------------- underwrite


def perform_underwrite(session: ProcessingSession, *, actor: Actor = Actor.AGENT) -> dict[str, Any]:
    """Run the deterministic engine, store the immutable run, update derived state."""
    repo = session.repo
    opp = session.opportunity()
    if opp is None:
        return {"error": "no opportunity has been recorded yet; call record_claims first"}
    if opp.working_values is None:
        return {"error": "no working values for this opportunity; record_claims must succeed first"}

    if opp.latest_run_id and session.run_before is None:
        session.run_before = repo.get_underwriting_run(opp.latest_run_id)

    run = run_underwriting(
        opp.working_values,
        session.policy,
        opportunity_id=opp.opportunity_id,
        trigger_event_id=session.last_event_id,
    )
    repo.store_underwriting_run(run)
    repo.record_policy_version(
        session.policy.policy_version, session.policy.name, _policy_yaml(session.policy)
    )

    viability = run.viability
    session.append_event(
        EventType.UNDERWRITING_COMPLETED,
        f"Underwritten at {_money(opp.working_values.asking_price)}: {run.failure_summary}",
        {
            "run_id": run.run_id,
            "status": run.status.value,
            "normalized_cap": str(run.normalized.normalized_cap_rate),
            "dscr": str(run.financing.dscr),
            "max_viable_price": str(viability.max_viable_price)
            if viability.max_viable_price is not None
            else None,
            "failure_summary": run.failure_summary,
        },
        actor=actor,
    )

    previous = opp.status
    if run.status != previous:
        session.append_event(
            EventType.STATUS_CHANGED,
            f"{previous.value} -> {run.status.value}",
            {"from": previous.value, "to": run.status.value},
            actor=actor,
        )
        opp.previous_status = previous

    opp.status = run.status
    opp.latest_run_id = run.run_id
    opp.viability = viability
    opp.reason_summary = run.failure_summary
    opp.human_attention_required = run.status == OpportunityStatus.REVIEW
    repo.save_opportunity(opp)

    session.run_after = run
    session.threshold_crossed = run.status == OpportunityStatus.REVIEW and previous != OpportunityStatus.REVIEW

    if session.threshold_crossed:
        next_step = (
            "This crossed into REVIEW. Call request_skeptic_review, then draft_broker_questions with the "
            "skeptic's questions, then notify_human. Finish with a one-line summary."
        )
    elif run.status == OpportunityStatus.DEAD:
        next_step = "Structurally dead. Do not notify anyone. Reply with a one-line summary."
    else:
        next_step = (
            f"Status is {run.status.value}; no human attention is justified. "
            "Reply with a one-line summary and stop."
        )

    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "previous_status": previous.value,
        "normalized_cap_rate": _pct(run.normalized.normalized_cap_rate),
        "broker_cap_rate": _pct(run.normalized.broker_cap_rate),
        "dscr": f"{Decimal(run.financing.dscr):.2f}x",
        "max_viable_price": _money(viability.max_viable_price),
        "distance_pct": _pct(viability.distance_pct),
        "binding_constraints": list(viability.binding_constraints),
        "structural_failures": list(viability.structural_failures),
        "failure_summary": run.failure_summary,
        "threshold_crossed": session.threshold_crossed,
        "next_step": next_step,
    }


# --------------------------------------------------------------------------- skeptic review


def perform_skeptic_review(session: ProcessingSession, *, actor: Actor = Actor.AGENT) -> dict[str, Any]:
    """Run the independent skeptic agent. Only on REVIEW."""
    from dealsieve.agents.skeptic import run_skeptic

    opp = session.opportunity()
    if opp is None:
        return {"skipped": "no opportunity yet"}
    if session.run_after is None:
        return {"skipped": "underwrite has not run for this message"}
    if opp.status != OpportunityStatus.REVIEW:
        return {"skipped": f"status is {opp.status.value}; the skeptic only reviews REVIEW opportunities"}
    if session.skeptic_report is not None:
        return {"skipped": "a skeptic review already ran for this message"}

    report = run_skeptic(session)
    session.repo.store_skeptic_report(report)
    session.skeptic_report = report

    session.append_event(
        EventType.SKEPTIC_REVIEW_COMPLETED,
        f"Skeptic verdict: {report.verdict} ({len(report.concerns)} concerns)",
        {
            "report_id": report.report_id,
            "run_id": report.run_id,
            "verdict": report.verdict,
            "summary": report.summary,
            "concerns": [c.topic for c in report.concerns],
        },
        actor=actor,
    )

    return {
        "report_id": report.report_id,
        "verdict": report.verdict,
        "summary": report.summary,
        "concerns": [
            {
                "topic": c.topic,
                "severity": c.severity,
                "evidence_status": c.evidence_status,
                "why_it_matters": c.why_it_matters,
            }
            for c in report.concerns
        ],
        "suggested_questions": suggested_questions(report),
    }


def suggested_questions(report: SkepticReport | None) -> list[str]:
    if report is None:
        return []
    return [c.question_for_broker for c in report.concerns if c.question_for_broker]


# --------------------------------------------------------------------------- broker draft


MAX_BROKER_QUESTIONS = 5


def _reply_subject(base: str) -> str:
    """Prepend "Re: " unless `base` is already a reply subject (case-insensitive)."""
    return base if base.lower().startswith("re:") else f"Re: {base}"


def _draft_body(opp: Opportunity, questions: list[str]) -> str:
    greeting = f"Hi {opp.broker_name.split()[0]}," if opp.broker_name else "Hi,"
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    return (
        f"{greeting}\n\n"
        f"Thanks for sending {opp.display_name}. Before we go further, a few diligence items:\n\n"
        f"{numbered}\n\n"
        "Happy to move quickly once those are clear.\n\nBest regards"
    )


def perform_draft_broker_questions(
    session: ProcessingSession, questions: list[str], *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Create a pending broker draft. Nothing is ever sent from here."""
    opp = session.opportunity()
    if opp is None:
        return {"skipped": "no opportunity yet"}
    report = session.skeptic_report
    if report is None:
        return {"skipped": "no skeptic report; call request_skeptic_review first"}
    if session.run_after is not None and report.run_id != session.run_after.run_id:
        return {"skipped": "the skeptic report is not for the current underwriting run"}

    cleaned = [q.strip() for q in questions if q and q.strip()][:MAX_BROKER_QUESTIONS]
    if not cleaned:
        return {"skipped": "no questions supplied"}

    subject = _reply_subject(session.message.subject or opp.display_name)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        to_email=opp.broker_email,
        subject=subject,
        body=_draft_body(opp, cleaned),
        questions=cleaned,
        status="pending",
    )
    session.repo.store_draft(draft)
    session.draft = draft

    session.append_event(
        EventType.BROKER_DRAFT_CREATED,
        f"Drafted {len(cleaned)} broker question(s), pending human approval",
        {"draft_id": draft.draft_id, "questions": cleaned, "to_email": draft.to_email},
        actor=actor,
    )

    return {
        "draft_id": draft.draft_id,
        "status": draft.status,
        "to_email": draft.to_email,
        "subject": draft.subject,
        "questions": cleaned,
        "sent": False,
        "note": "Nothing is sent until a human approves this draft.",
    }


# --------------------------------------------------------------------------- notify human


def perform_notify_human(
    session: ProcessingSession, note: str = "", *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Interrupt the human. Only on a real WATCH/NEAR -> REVIEW crossing, and only once."""
    if not session.threshold_crossed:
        return {"skipped": "no threshold crossing"}
    if session.notification is not None:
        return {"skipped": "the human has already been notified for this message"}
    opp = session.opportunity()
    if opp is None or session.run_after is None:
        return {"skipped": "no completed underwriting run"}

    # Tag the record with where the alert actually went, not a hardcoded channel. Notifiers
    # declare `channel` (base.Notifier); fall back to the formatter's default if one does not.
    channel = getattr(session.notifier, "channel", None)
    notification = format_threshold_alert(
        opp,
        session.run_before,
        session.run_after,
        session.skeptic_report,
        **({"channel": channel} if channel is not None else {}),
    )
    delivery_ref = session.notifier.send(notification)
    notification.delivered = delivery_ref is not None
    notification.delivery_ref = delivery_ref
    session.repo.store_notification(notification)
    session.notification = notification

    session.append_event(
        EventType.HUMAN_NOTIFIED,
        f"Human notified: {notification.title}",
        {
            "notification_id": notification.notification_id,
            "channel": notification.channel.value,
            "delivered": notification.delivered,
            "delivery_ref": delivery_ref,
            "note": note,
        },
        actor=actor,
    )

    return {
        "notification_id": notification.notification_id,
        "delivered": notification.delivered,
        "delivery_ref": delivery_ref,
        "channel": notification.channel.value,
        "title": notification.title,
    }


# --------------------------------------------------------------------------- tool factory


def make_tools(session: ProcessingSession) -> list[Any]:
    """Build the five @tool closures bound to one ProcessingSession.

    `record_claims` declares `claims: ExtractedClaims`. Strands' `@tool` does accept a Pydantic
    model parameter and generates the full JSON schema for it (`$defs`/`$ref` and all), which is
    exactly the guidance we want the model to see. It does *not*, however, pass the function a
    model instance: `FunctionToolMetadata.validate_input` returns `validated.model_dump()`, so the
    parameter arrives as a plain dict. `perform_record_claims` re-validates it, so the tool body
    always works with a real `ExtractedClaims`.

    Every tool body is wrapped by `_guard`: Strands turns an exception into an error tool result
    that only the model sees, and the model will happily carry on and write a confident summary.
    Recording the failure on the session instead keeps `ProcessingOutcome.summary` honest.
    """

    def _guard(name: str, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return call()
        except Exception as exc:
            error = f"{name} failed: {type(exc).__name__}: {exc}"
            logger.exception("tool %s failed", name)
            session.errors.append(error)
            return {"error": error}

    @tool
    def record_claims(claims: ExtractedClaims) -> dict:
        """Record what this message claims about a property, then resolve it to one opportunity.

        Stores the message and its documents, saves every evidence item with its provenance,
        matches the property against everything already known, creates the opportunity when it is
        new, reconciles the claims into deterministic working values and appends the events.
        Call this exactly once, first, for every message.

        Args:
            claims: Everything the message and its attachments assert about the property, with one
                evidence entry per fact. Leave unknown fields null. Never estimate a number.

        Returns:
            The opportunity id and deal number, whether it is new, its current status, the detected
            changes, unresolved conflicts and the fields still missing.
        """
        return _guard("record_claims", lambda: perform_record_claims(session, claims))

    @tool
    def underwrite() -> dict:
        """Underwrite the opportunity with the deterministic engine under the frozen policy.

        Normalizes the broker's economics, computes financing, evaluates every gate, solves the
        viability frontier and classifies the deal DEAD / WATCH / NEAR / REVIEW. You do not do any
        arithmetic yourself. Call this once, after record_claims.

        Returns:
            The run id, the resulting status, normalized cap rate, DSCR, the maximum viable price,
            the binding constraints, whether this crossed into REVIEW, and what to do next.
        """
        return _guard("underwrite", lambda: perform_underwrite(session))

    @tool
    def request_skeptic_review() -> dict:
        """Ask the independent skeptic agent for reasons to still reject this deal.

        Only runs when the opportunity is in REVIEW. The skeptic never recomputes finance; it looks
        for unverified claims and missing diligence.

        Returns:
            The verdict, a summary, the concerns with severity and evidence status, and suggested
            broker questions. Returns {"skipped": reason} when a review is not warranted.
        """
        return _guard("request_skeptic_review", lambda: perform_skeptic_review(session))

    @tool
    def draft_broker_questions(questions: list[str]) -> dict:
        """Draft the broker email carrying the diligence questions. Nothing is sent.

        Only allowed after a skeptic review of the current underwriting run. The draft is stored
        pending explicit human approval.

        Args:
            questions: The questions to ask the broker, one per item, in priority order. Use the
                skeptic's suggested questions.

        Returns:
            The draft id and its pending status, or {"skipped": reason}.
        """
        return _guard(
            "draft_broker_questions", lambda: perform_draft_broker_questions(session, questions)
        )

    @tool
    def notify_human(note: str) -> dict:
        """Interrupt the human, once, because this deal just became investable.

        Only allowed when the latest underwriting run moved the opportunity into REVIEW from
        something else. Every other situation stays silent.

        Args:
            note: One short line on why this deserves attention right now.

        Returns:
            The notification id and delivery result, or {"skipped": "no threshold crossing"}.
        """
        return _guard("notify_human", lambda: perform_notify_human(session, note))

    return [record_claims, underwrite, request_skeptic_review, draft_broker_questions, notify_human]


__all__ = [
    "MAX_BROKER_QUESTIONS",
    "ProcessingSession",
    "make_tools",
    "perform_draft_broker_questions",
    "perform_notify_human",
    "perform_record_claims",
    "perform_skeptic_review",
    "perform_underwrite",
    "suggested_questions",
]
