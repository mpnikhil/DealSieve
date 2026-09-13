"""The deterministic tools the acquisition agent may call.

Design rule: **the model decides what to say, the tools decide what is true.** Every gate lives in
Python here, not in the prompt. The model cannot underwrite, cannot invent a status, cannot notify a
human without a real threshold crossing, cannot chase diligence on a deal that is not in REVIEW, and
cannot put a dollar figure in front of a broker without a human approving it. If the model skips a
step, `dealsieve.pipeline`'s safety net performs it as the SYSTEM actor.

Invariants worth calling out because they were real defects (see the review findings in
`docs/CONTRACTS.md` and the Phase 2 hardening pass):

* **R5, the phase machine.** Tool calls for one message must follow one order, and repeats change
  nothing. Before the phase machine existed, a model that called `underwrite` twice cleared the
  `threshold_crossed` latch on the second call and the human was never told. Order and latches are
  now enforced in :class:`ProcessingSession`, not in the prompt.
* **R3, intent before delivery.** A notification is persisted with `delivered=False` *first*, under a
  unique `dedupe_key`; only then is it delivered and marked. A crash between the two leaves a record
  that says "we tried", so a retry resumes *that* delivery instead of alerting twice -- and a
  delivery that raised is never reported as a delivery that happened.
* **G1/G2, capex is verified and aggregated.** Capex items are model *proposals*: an item enters the
  underwriting basis only when both of its dollar figures actually appear in the document's text
  (:func:`verify_capex_items`), and the basis is the union of every accepted item across every
  analysis of the opportunity (:func:`aggregate_capex_items`), never just the newest document's.
* **G4/G5, the model does not choose the asks.** Diligence items are reconciled against the skeptic's
  own concerns server-side, and a credit request needs a real reason in this message.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from strands import tool

from dealsieve.evidence.reconcile import MissingInputs, reconcile
from dealsieve.identity.resolver import extract_identity_keys, normalize_address, resolve
from dealsieve.notifications import format_threshold_alert
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    Attachment,
    CapexItem,
    DiligenceRequest,
    DocumentAnalysis,
    DocumentFinding,
    EventType,
    Evidence,
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
    from dealsieve.outbound import Outbox
    from dealsieve.persistence import Repo

logger = logging.getLogger(__name__)


class DuplicateNotificationFallback(RuntimeError):
    """Stand-in used only when the repo layer predates `DuplicateNotification` (W10)."""


def duplicate_notification_error() -> type[BaseException]:
    """The exception `repo.store_notification` raises on a `dedupe_key` clash.

    Resolved lazily so this module imports cleanly against a persistence layer that has not grown
    the constraint yet; once it has, the real class is used.
    """
    try:
        from dealsieve.persistence import DuplicateNotification
    except ImportError:  # pragma: no cover - only before W10 lands
        return DuplicateNotificationFallback
    return DuplicateNotification


def diligence_module() -> ModuleType:
    """The deterministic diligence engine (W10), imported lazily so tests can substitute a fake."""
    from dealsieve import diligence

    return diligence


def _accepts(func: Any, parameter: str) -> bool:
    """True when `func` takes a keyword parameter of that name (or **kwargs)."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins / C callables
        return False
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return True
    return parameter in signature.parameters


# --------------------------------------------------------------------------- phase machine (R5)

#: The one legal order of tool calls for a single message. Same rank means "either, in any order,
#: each at most once": once the deal's outcome is known the agent takes exactly the branch that
#: applies (skeptic + diligence on a crossing, a credit request on a loss).
TOOL_RANK: dict[str, int] = {
    "record_claims": 0,
    "analyze_document": 1,
    "underwrite": 2,
    "request_skeptic_review": 3,
    "request_diligence": 3,
    "request_price_adjustment": 3,
    "notify_human": 4,
}

#: Documents arrive several to a message, so this one may run as often as there are attachments.
REPEATABLE_TOOLS = frozenset({"analyze_document"})

PHASE_ORDER_TEXT = (
    "record_claims -> analyze_document (per document) -> underwrite -> "
    "request_skeptic_review / request_diligence / request_price_adjustment -> notify_human"
)


# --------------------------------------------------------------------------- session


@dataclass
class ProcessingSession:
    """Mutable state shared by every tool call for one inbound message."""

    repo: Repo
    policy: InvestmentPolicy
    notifier: Notifier
    message: InboundMessage
    model_backend: str

    # Only meaningful for the scripted backend; forwarded to the skeptic and inspector models.
    script: str | None = None

    #: Where approved broker mail actually goes. `None` until the pipeline supplies one.
    outbox: Outbox | None = None

    # -- mutable state -----------------------------------------------------
    opportunity_id: str | None = None
    created: bool = False
    status_before: OpportunityStatus | None = None
    run_before: UnderwritingResult | None = None
    run_after: UnderwritingResult | None = None
    threshold_crossed: bool = False
    threshold_lost: bool = False
    skeptic_report: SkepticReport | None = None
    notification: Notification | None = None
    """The alert that was actually delivered. `None` while nothing has reached the human."""
    undelivered_notification: Notification | None = None
    """A stored alert whose delivery raised. The message is retryable until it goes out (G3)."""
    notification_error: str | None = None
    draft: OutboundDraft | None = None
    pending_request: OutboundDraft | None = None
    credit_draft: OutboundDraft | None = None
    analyses: list[DocumentAnalysis] = field(default_factory=list)
    diligence_requests: list[DiligenceRequest] = field(default_factory=list)
    events_created: list[str] = field(default_factory=list)
    claims_recorded: bool = False
    errors: list[str] = field(default_factory=list)

    # -- bookkeeping used to build prompts and trigger references ----------
    claims: ExtractedClaims | None = None
    last_event_id: str | None = None
    underwrite_result: dict[str, Any] | None = None
    event_types_created: set[EventType] = field(default_factory=set)
    """Every event type this message appended. Gates that ask "did anything actually change?" use it."""

    # -- R5 phase machine --------------------------------------------------
    phase: int = 0
    """Lowest tool rank still allowed. Only ever moves forward within one message."""
    phase_tool: str | None = None
    tools_called: set[str] = field(default_factory=set)

    def phase_check(self, tool_name: str) -> str | None:
        """Return why `tool_name` may not run now, or None when it may. Mutates nothing."""
        rank = TOOL_RANK[tool_name]
        if tool_name in self.tools_called and tool_name not in REPEATABLE_TOOLS:
            return f"{tool_name} already ran for this message; it runs at most once"
        if rank < self.phase:
            return (
                f"{tool_name} cannot run after {self.phase_tool} for this message; "
                f"the order is {PHASE_ORDER_TEXT}"
            )
        return None

    def phase_advance(self, tool_name: str) -> None:
        """Record that `tool_name` completed. Called only on the paths that actually did something."""
        self.tools_called.add(tool_name)
        rank = TOOL_RANK[tool_name]
        if rank >= self.phase:
            self.phase = rank
            self.phase_tool = tool_name

    def latest_analysis(self) -> DocumentAnalysis | None:
        return self.analyses[-1] if self.analyses else None

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
        self.event_types_created.add(event_type)
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
    reason = session.phase_check("record_claims")
    if reason:
        return {"skipped": reason}
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
        session.phase_advance("record_claims")
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
    session.phase_advance("record_claims")

    return {
        "opportunity_id": opportunity_id,
        "deal_number": opp.deal_number,
        "created": created,
        "status": opp.status.value,
        "changes": [change.summary for change in changes],
        "conflicts": list(working.conflicts),
        "missing_fields": list(claims.missing_fields),
    }


# --------------------------------------------------------------------------- analyze_document

#: Statuses a diligence request can be in and still be waiting on the broker.
OPEN_REQUEST_STATUSES = ("draft", "sent", "overdue")


def find_attachment(message: InboundMessage, filename: str | None) -> Attachment | None:
    """Exact filename match; failing that, the message's only PDF.

    "The only PDF" is deliberate: when a broker attaches one report the model should not have to
    reproduce a long filename character-perfect, but when there are two we refuse to guess which
    one was meant.
    """
    wanted = (filename or "").strip()
    if wanted:
        for attachment in message.attachments:
            if attachment.filename == wanted:
                return attachment
        lowered = wanted.lower()
        for attachment in message.attachments:
            if attachment.filename.lower() == lowered:
                return attachment
    pdfs = [
        a
        for a in message.attachments
        if a.content_type == "application/pdf" or a.filename.lower().endswith(".pdf")
    ]
    if len(pdfs) == 1:
        return pdfs[0]
    return None


def finding_location(finding: DocumentFinding, image_paths: list[str]) -> str | None:
    """Provenance for the evidence row: "page 3", or "image 1" recovered from the stored path."""
    if finding.page is not None:
        return f"page {finding.page}"
    if finding.image_ref:
        if finding.image_ref in image_paths:
            return f"image {image_paths.index(finding.image_ref) + 1}"
        return finding.image_ref
    return None


def evidence_from_findings(analysis: DocumentAnalysis) -> list[Evidence]:
    """One evidence row per finding, so the document's conclusions join the permanent record."""
    return [
        Evidence(
            field=f"doc:{finding.topic}",
            value=finding.value,
            source_document=analysis.filename,
            location=finding_location(finding, analysis.image_paths),
            quote=finding.detail,
            confidence=finding.confidence,
            observed_by="inspector_agent",
        )
        for finding in analysis.findings
    ]


# ------------------------------------------------------------------ capex: verify (G1), aggregate (G2)

#: Whitespace and dash characters a PDF extractor emits that a naive match would trip over.
_TEXT_NORMALIZATION = str.maketrans(
    {
        " ": " ",  # no-break space
        " ": " ",  # figure space
        " ": " ",  # thin space
        " ": " ",  # narrow no-break space
        "–": "-",  # en dash, as in "$85,000–$95,000"
        "—": "-",  # em dash
        "−": "-",  # minus sign
    }
)

#: A money-ish figure in document text: "85000", "85,000", "$85,000.00", "$85k", "1.2M".
_AMOUNT_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?:\s*([kKmM])\b)?")

_SUFFIX_MULTIPLIER = {"k": Decimal(1_000), "m": Decimal(1_000_000)}


def document_amounts(text: str) -> set[Decimal]:
    """Every number the document text states, in every common money rendering.

    The verifier compares values rather than strings so that "$85,000", "85,000", "85000" and "$85k"
    all satisfy the same item, and so that thin spaces or an en dash inside a range cannot make a
    figure that is plainly in the document look absent.
    """
    amounts: set[Decimal] = set()
    for whole, fraction, suffix in _AMOUNT_RE.findall((text or "").translate(_TEXT_NORMALIZATION)):
        try:
            value = Decimal(whole.replace(",", "") + (f".{fraction}" if fraction else ""))
        except InvalidOperation:  # pragma: no cover - defensive
            continue
        if suffix:
            value *= _SUFFIX_MULTIPLIER[suffix.lower()]
        amounts.add(value)
    return amounts


def _item_amounts(item: CapexItem) -> list[Decimal]:
    """The item's low and high, deduplicated, in order."""
    return list(dict.fromkeys((Decimal(item.low), Decimal(item.high))))


def _unverified_amounts(item: CapexItem, amounts: set[Decimal]) -> list[Decimal]:
    return [amount for amount in _item_amounts(item) if amount not in amounts]


def capex_rejection_reason(item: CapexItem, document_text: str) -> str:
    """Why this proposed capex item is not usable: which of its figures the document never states."""
    missing = _unverified_amounts(item, document_amounts(document_text))
    if not missing:  # pragma: no cover - only called for rejected items
        return "verified"
    figures = " and ".join(_money(amount) for amount in missing)
    return f"{figures} does not appear in the text of {item.source_document}"


def verify_capex_items(
    items: list[CapexItem], document_text: str
) -> tuple[list[CapexItem], list[CapexItem]]:
    """Split model-proposed capex into (accepted, rejected) against what the document actually says.

    Capex moves the underwriting basis, so an item is a *proposal* until both of its dollar figures
    are found in the document's own text. Anything else -- a hallucinated range, a figure lifted from
    prose injected into the document, a helpful "typical" cost -- is rejected. `location` is not
    required: a model that cannot name the page but quotes the right numbers is still verifiable.
    """
    amounts = document_amounts(document_text)
    accepted: list[CapexItem] = []
    rejected: list[CapexItem] = []
    for item in items:
        (rejected if _unverified_amounts(item, amounts) else accepted).append(item)
    return accepted, rejected


def _capex_key(item_text: str) -> str:
    """Dedupe key for a capex item: casefolded, whitespace-collapsed item text."""
    return " ".join(item_text.split()).casefold()


def aggregate_capex_items(
    analyses: Iterable[DocumentAnalysis],
) -> tuple[list[CapexItem], list[str]]:
    """Union the verified capex of every analysis of one opportunity, newest estimate winning.

    A second document must not erase the first document's capital work (that defect quietly halved
    the basis), and the same item priced twice must not count twice. Items are matched on normalized
    item text; when two documents price the same item differently the later figure is used and the
    disagreement is returned as a conflict string, because contradictory source data is recorded,
    never silently resolved.
    """
    merged: dict[str, CapexItem] = {}
    conflicts: list[str] = []
    for analysis in sorted(analyses, key=lambda a: a.created_at):
        for item in analysis.capex_items:
            key = _capex_key(item.item)
            previous = merged.get(key)
            if previous is not None and _item_amounts(previous) != _item_amounts(item):
                conflicts.append(
                    f"Capex '{item.item}': {previous.source_document} says "
                    f"{_money(previous.low)}-{_money(previous.high)}, {item.source_document} says "
                    f"{_money(item.low)}-{_money(item.high)}; the later document is used"
                )
            merged[key] = item
    return list(merged.values()), conflicts


def perform_analyze_document(
    session: ProcessingSession, filename: str | None = None, *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Read one attached document with the inspector agent and fold it into the record.

    The document becomes four things: an immutable `DocumentAnalysis`, evidence rows with page and
    photo provenance, answers to whichever diligence requests it settles, and -- when it prices day-one
    capital work -- an `immediate_capex` figure that changes the basis the next underwriting run uses.

    Capex is the part a model must not be trusted with, so it goes through two deterministic steps:
    every proposed item is verified against the document's own text (G1) and only verified items are
    stored, and the opportunity's basis is then recomputed as the union of the verified items across
    every analysis of this deal (G2), so a second document adds to the first instead of replacing it.
    """
    from dealsieve.agents.inspector import run_inspector

    reason = session.phase_check("analyze_document")
    if reason:
        return {"skipped": reason}

    opp = session.opportunity()
    if opp is None:
        return {"skipped": "no opportunity yet; call record_claims first"}

    attachment = find_attachment(session.message, filename)
    if attachment is None:
        available = ", ".join(a.filename for a in session.message.attachments) or "(none)"
        return {"skipped": f"no attachment matching {filename!r} on this message; available: {available}"}
    if any(a.filename == attachment.filename for a in session.analyses):
        return {"skipped": f"{attachment.filename} has already been analyzed for this message"}

    repo = session.repo
    diligence = diligence_module()
    open_requests = [
        r
        for r in repo.list_diligence_requests(opportunity_id=opp.opportunity_id)
        if r.status in OPEN_REQUEST_STATUSES
    ]

    analysis = run_inspector(session, attachment, open_requests)

    # --- capex proposals are verified against the document before they are stored (G1) -
    accepted_capex, rejected_capex = verify_capex_items(analysis.capex_items, attachment.text or "")
    rejections = [
        {
            "item": item.item,
            "low": str(item.low),
            "high": str(item.high),
            "urgency": item.urgency,
            "location": item.location,
            "reason": capex_rejection_reason(item, attachment.text or ""),
        }
        for item in rejected_capex
    ]
    if rejected_capex:
        analysis = analysis.model_copy(update={"capex_items": accepted_capex})

    repo.store_document_analysis(analysis)
    session.analyses.append(analysis)

    # --- evidence: the findings become part of the property's permanent record ---------
    evidence = evidence_from_findings(analysis)
    if evidence:
        repo.store_evidence(opp.opportunity_id, evidence)
    evidence_ids_by_topic = {
        item.field.removeprefix("doc:"): [item.evidence_id] for item in evidence
    }

    session.append_event(
        EventType.DOCUMENT_ANALYZED,
        f"Read {analysis.filename}: {analysis.document_type} "
        f"({len(analysis.findings)} finding(s), {analysis.images_reviewed} image(s))",
        {
            "analysis_id": analysis.analysis_id,
            "filename": analysis.filename,
            "document_type": analysis.document_type,
            "findings": len(analysis.findings),
            "images_reviewed": analysis.images_reviewed,
            "red_flags": list(analysis.red_flags),
            "capex_items": [
                {"item": i.item, "low": str(i.low), "high": str(i.high), "urgency": i.urgency}
                for i in analysis.capex_items
            ],
            "rejected_capex": rejections,
        },
        actor=actor,
    )

    for rejection in rejections:
        session.append_event(
            EventType.NOTE,
            f"Capex item not verified against document text: {rejection['item']} "
            f"({_money(Decimal(rejection['low']))}-{_money(Decimal(rejection['high']))}); "
            f"{rejection['reason']}",
            {"filename": analysis.filename, "analysis_id": analysis.analysis_id, **rejection},
            actor=actor,
        )

    # --- answers: which of the open questions did this document settle? ----------------
    matches = diligence.match_answers(open_requests, analysis)
    answered = diligence.apply_answers(
        matches, analysis, repo=repo, evidence_ids_by_topic=evidence_ids_by_topic
    )
    answered_topics = [r.topic for r in answered if r.status == "answered"]
    still_open = [r.topic for r in open_requests if r.topic not in answered_topics]

    # --- capex: the basis is every verified item this opportunity knows about (G2) ------
    stored = list(repo.list_document_analyses(opp.opportunity_id))
    if all(other.analysis_id != analysis.analysis_id for other in stored):  # pragma: no cover
        stored.append(analysis)  # a repo that does not read its own write back
    all_items, capex_conflicts = aggregate_capex_items(stored)
    capex_total = diligence.immediate_capex_from(all_items, session.policy)

    working = opp.working_values
    if working is not None:
        previous = Decimal(working.immediate_capex or 0)
        new_conflicts = [c for c in capex_conflicts if c not in working.conflicts]
        changed = (
            capex_total != previous or list(working.capex_items) != all_items or bool(new_conflicts)
        )
        working.immediate_capex = capex_total
        working.capex_items = all_items
        working.conflicts = [*working.conflicts, *new_conflicts]
        if changed:
            repo.save_opportunity(opp)
        if capex_total != previous:
            session.append_event(
                EventType.CAPEX_ADJUSTED,
                f"Immediate capex {_money(previous)} -> {_money(capex_total)} from {analysis.filename}",
                {
                    "from": str(previous),
                    "to": str(capex_total),
                    "source_document": analysis.filename,
                    "analyses": len(stored),
                    "items": [
                        {
                            "item": i.item,
                            "low": str(i.low),
                            "high": str(i.high),
                            "urgency": i.urgency,
                            "source_document": i.source_document,
                        }
                        for i in all_items
                    ],
                    "conflicts": new_conflicts,
                },
                actor=actor,
            )

    session.phase_advance("analyze_document")

    return {
        "analysis_id": analysis.analysis_id,
        "document_type": analysis.document_type,
        "summary": analysis.summary,
        "findings": len(analysis.findings),
        "images_reviewed": analysis.images_reviewed,
        "answered_topics": answered_topics,
        "still_open_topics": still_open,
        "immediate_capex": str(capex_total),
        "capex_items": [i.item for i in all_items],
        "rejected_capex": rejections,
        "capex_conflicts": capex_conflicts,
        "red_flags": list(analysis.red_flags),
        "next_step": "call underwrite",
    }


# --------------------------------------------------------------------------- underwrite


def perform_underwrite(session: ProcessingSession, *, actor: Actor = Actor.AGENT) -> dict[str, Any]:
    """Run the deterministic engine, store the immutable run, update derived state.

    R5: exactly one run per message. A second call hands back the first result unchanged rather
    than producing a new run -- which is what used to clear the `threshold_crossed` latch and lose
    the human interruption.
    """
    if session.run_after is not None and session.underwrite_result is not None:
        return {
            **session.underwrite_result,
            "repeated": True,
            "note": "underwrite already ran for this message; this is the same result, no new run",
        }

    reason = session.phase_check("underwrite")
    if reason:
        return {"skipped": reason}

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

    # Latches: set once, never cleared within a message (R5).
    if run.status == OpportunityStatus.REVIEW and previous != OpportunityStatus.REVIEW:
        session.threshold_crossed = True
    if previous == OpportunityStatus.REVIEW and run.status != OpportunityStatus.REVIEW:
        session.threshold_lost = True

    if session.threshold_crossed:
        next_step = (
            "This crossed into REVIEW. Call request_skeptic_review, then request_diligence with the "
            "skeptic's missing-evidence concerns, then notify_human. Finish with a one-line summary."
        )
    elif session.threshold_lost:
        next_step = (
            "This FELL BACK OUT of REVIEW. Call request_price_adjustment with the gap between the "
            "current price and the maximum viable price, then notify_human. "
            "Finish with a one-line summary."
        )
    elif run.status == OpportunityStatus.DEAD:
        next_step = "Structurally dead. Do not notify anyone. Reply with a one-line summary."
    else:
        next_step = (
            f"Status is {run.status.value}; no human attention is justified. "
            "Reply with a one-line summary and stop."
        )

    result = {
        "run_id": run.run_id,
        "status": run.status.value,
        "previous_status": previous.value,
        "normalized_cap_rate": _pct(run.normalized.normalized_cap_rate),
        "broker_cap_rate": _pct(run.normalized.broker_cap_rate),
        "dscr": f"{Decimal(run.financing.dscr):.2f}x",
        "immediate_capex": _money(run.financing.immediate_capex),
        "all_in_basis": _money(run.financing.all_in_basis),
        "max_viable_price": _money(viability.max_viable_price),
        "distance_pct": _pct(viability.distance_pct),
        "binding_constraints": list(viability.binding_constraints),
        "structural_failures": list(viability.structural_failures),
        "failure_summary": run.failure_summary,
        "threshold_crossed": session.threshold_crossed,
        "threshold_lost": session.threshold_lost,
        "next_step": next_step,
    }
    session.underwrite_result = result
    session.phase_advance("underwrite")
    return result


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
    reason = session.phase_check("request_skeptic_review")
    if reason:
        return {"skipped": reason}

    report = run_skeptic(session)
    session.repo.store_skeptic_report(report)
    session.skeptic_report = report
    session.phase_advance("request_skeptic_review")

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


# --------------------------------------------------------------------------- diligence requests


class DiligenceItem(BaseModel):
    """One question to chase with the broker."""

    topic: str = Field(
        description='Short noun phrase naming the issue only, e.g. "Roof age", "Phase I environmental".'
    )
    question: str = Field(description="One specific, answerable question. Never two asks joined by 'and'.")
    category: Literal["document", "disclosure", "clarification"] = "document"


MAX_DILIGENCE_ITEMS = 5
_CHASE_ORDER = {"missing": 0, "weak": 1, "unverified": 2}


def diligence_items_from(report: SkepticReport | None) -> list[dict[str, Any]]:
    """The skeptic concerns worth chasing with the broker.

    A concern qualifies when the skeptic wrote a concrete `question_for_broker` and the evidence is not
    "contradicted" (a contradiction is for the human to weigh, not for the broker to paper over).
    "missing" concerns come first, then "weak", then "unverified"; at most MAX_DILIGENCE_ITEMS are chased
    so one email never reads like an audit. Real models label evidence inconsistently, which is why the
    rule keys on the presence of a question rather than on one exact label.
    """
    if report is None:
        return []
    eligible = [
        concern
        for concern in report.concerns
        if concern.question_for_broker and concern.evidence_status in _CHASE_ORDER
    ]
    eligible.sort(key=lambda concern: _CHASE_ORDER[concern.evidence_status])
    return [
        {
            "topic": concern.topic,
            "question": concern.question_for_broker,
            "category": "document",
            "source_concern": concern.topic,
        }
        for concern in eligible[:MAX_DILIGENCE_ITEMS]
    ]


#: Tokens that carry no topic meaning; they would let a short poisoned item "match" anything.
_MATCH_STOPWORDS = frozenset(
    """a an and are as at be by can could do does for from has have how in is it its may of on or
    please provide send show that the there this to us was we what when where which who why will
    with would you your""".split()
)

#: How much of the shorter phrase must be shared before a model item counts as the same ask.
DILIGENCE_MATCH_THRESHOLD = 0.6

_WORD_RE = re.compile(r"[a-z0-9]+")


def _match_tokens(text: str) -> set[str]:
    """Case-folded, punctuation-stripped content words. Falls back to raw tokens if all are stopwords."""
    tokens = set(_WORD_RE.findall((text or "").casefold()))
    content = tokens - _MATCH_STOPWORDS
    return content or tokens


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def diligence_items_match(proposed: dict[str, Any], derived: dict[str, Any]) -> bool:
    """True when a model-proposed item is asking the same thing as a skeptic-derived item."""
    proposed_topic = _match_tokens(str(proposed.get("topic", "")))
    proposed_question = _match_tokens(str(proposed.get("question", "")))
    derived_topic = _match_tokens(str(derived.get("topic", "")))
    derived_question = _match_tokens(str(derived.get("question", "")))
    return any(
        _overlap(left, right) >= DILIGENCE_MATCH_THRESHOLD
        for left, right in (
            (proposed_topic, derived_topic),
            (proposed_question, derived_question),
            (proposed_topic, derived_question),
            (proposed_question, derived_topic),
        )
    )


def reconcile_diligence_items(
    proposed: list[dict[str, Any]], report: SkepticReport | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reduce the model's items to the skeptic's own concerns. Returns (accepted, dropped).

    The model reads untrusted broker text, so what it asks the broker is not its decision: the set of
    legitimate questions is whatever `diligence_items_from` derives from the skeptic report. A model
    item is only a *selector* over that set -- the text that actually reaches the broker is always the
    skeptic-derived wording, so an injected instruction cannot become an outbound question. Items that
    select nothing are dropped and reported; if nothing the model passed selects anything, the whole
    derived set is chased instead of nothing.
    """
    derived = diligence_items_from(report)
    if not derived:
        return [], [{**item, "reason": "the skeptic raised no chaseable concern"} for item in proposed]
    accepted = [
        item for item in derived if any(diligence_items_match(p, item) for p in proposed)
    ]
    dropped = [
        {**item, "reason": "no matching concern in this message's skeptic report"}
        for item in proposed
        if not any(diligence_items_match(item, d) for d in derived)
    ]
    if not accepted:
        accepted = derived
    return accepted, dropped


def perform_request_diligence(
    session: ProcessingSession, items: list[dict[str, Any]], *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Turn the skeptic's unanswered questions into tracked requests and one broker message.

    The requests are the durable thing: each one is chased on the policy cadence until it is
    answered or the follow-up budget runs out. The message is composed here but `dispatch` decides
    whether it leaves the building -- under the default policy it does not, and waits for a human tap.
    """
    reason = session.phase_check("request_diligence")
    if reason:
        return {"skipped": reason}

    opp = session.opportunity()
    if opp is None:
        return {"skipped": "no opportunity yet"}
    if session.run_after is None:
        return {"skipped": "underwrite has not run for this message"}
    if opp.status != OpportunityStatus.REVIEW:
        return {"skipped": f"status is {opp.status.value}; diligence is only chased on REVIEW opportunities"}
    if session.skeptic_report is None:
        return {
            "skipped": (
                "no skeptic review ran for this message; diligence is derived from the skeptic's "
                "concerns, so call request_skeptic_review first"
            )
        }

    proposed = [
        {
            "topic": str(item.get("topic", "")).strip(),
            "question": str(item.get("question", "")).strip(),
        }
        for item in items
        if str(item.get("topic", "")).strip() and str(item.get("question", "")).strip()
    ]
    cleaned, dropped = reconcile_diligence_items(proposed, session.skeptic_report)
    if not cleaned:
        return {
            "skipped": "no diligence items: the skeptic report carries no concern with a broker question",
            "dropped": dropped,
        }

    diligence = diligence_module()
    requests = diligence.build_requests(
        opp.opportunity_id, cleaned, source=session.skeptic_report.report_id if session.skeptic_report else None
    )
    if not requests:
        return {"skipped": "every item duplicated an existing request"}
    for request in requests:
        session.repo.store_diligence_request(request)

    draft = diligence.compose_information_request(
        opp,
        requests,
        session.policy,
        in_reply_to=session.message.message_id,
        original_subject=session.message.subject or opp.display_name,
    )
    draft = diligence.dispatch(
        draft, repo=session.repo, policy=session.policy, outbox=session.outbox, actor=actor
    )

    session.diligence_requests = requests
    session.draft = draft
    if draft.status != "sent":
        session.pending_request = draft

    session.append_event(
        EventType.DILIGENCE_REQUESTED,
        f"{len(requests)} diligence request(s) raised: " + ", ".join(r.topic for r in requests),
        {
            "request_ids": [r.request_id for r in requests],
            "topics": [r.topic for r in requests],
            "draft_id": draft.draft_id,
            "draft_status": draft.status,
            "dropped_items": dropped,
        },
        actor=actor,
    )

    awaiting = draft.status != "sent"
    return {
        "request_ids": [r.request_id for r in requests],
        "topics": [r.topic for r in requests],
        "dropped": dropped,
        "draft_id": draft.draft_id,
        "status": draft.status,
        "awaiting_approval": awaiting,
        "outbox_ref": draft.delivery_ref,
        "note": (
            "The information request is drafted and waiting for the human to approve it."
            if awaiting
            else "The information request was sent autonomously under the outreach policy."
        ),
    }


# --------------------------------------------------------------------------- price adjustment

#: Credit requests are rounded to whole thousands: nobody negotiates to the dollar.
CREDIT_ROUNDING = Decimal("1000")

#: How far the model's own number may sit from the computed gap before the code overrules it.
CREDIT_TOLERANCE = Decimal("0.25")


def suggested_credit(current_price: Decimal, max_viable_price: Decimal) -> Decimal:
    """The gap between what is being asked and what the policy can pay, rounded up to $1,000."""
    gap = Decimal(current_price) - Decimal(max_viable_price)
    if gap <= 0:
        return Decimal("0")
    return Decimal(math.ceil(gap / CREDIT_ROUNDING)) * CREDIT_ROUNDING


def perform_request_price_adjustment(
    session: ProcessingSession,
    amount: Decimal | int | float | str | None = None,
    rationale: str = "",
    *,
    actor: Actor = Actor.AGENT,
) -> dict[str, Any]:
    """Draft a credit request for the amount that would put the deal back inside the policy.

    The model may propose a number, but it does not own it: the code computes the gap from the
    stored viability frontier and overrides anything more than 25% away from it. Money talk always
    waits for a human, so this only ever produces a pending draft.
    """
    reason = session.phase_check("request_price_adjustment")
    if reason:
        return {"skipped": reason}

    opp = session.opportunity()
    if opp is None:
        return {"skipped": "no opportunity yet"}
    run = session.run_after
    if run is None:
        return {"skipped": "underwrite has not run for this message"}

    frontier = run.viability.max_viable_price
    # A credit request needs something that happened in THIS message to justify it: the deal fell out
    # of REVIEW, or a document / a price change moved a NEAR-or-WATCH deal. Without that gate the
    # model can negotiate against any stale deal in the book just by being asked to.
    new_evidence = bool(session.analyses) or EventType.ASKING_PRICE_CHANGED in session.event_types_created
    eligible = session.threshold_lost or (
        run.status in (OpportunityStatus.NEAR, OpportunityStatus.WATCH)
        and frontier is not None
        and new_evidence
    )
    if not eligible:
        return {
            "skipped": (
                f"status is {run.status.value}, the deal did not fall out of REVIEW, and this message "
                "produced no document analysis and no price change; there is nothing to ask a credit for"
            )
        }
    if frontier is None:
        return {"skipped": "no viability frontier; no price fixes this deal"}

    current_price = Decimal(run.inputs.asking_price)
    suggested = suggested_credit(current_price, frontier)
    if suggested <= 0:
        return {"skipped": "the asking price is already at or below the maximum viable price"}

    requested = None
    if amount is not None:
        try:
            requested = Decimal(str(amount))
        except (ArithmeticError, ValueError):
            requested = None

    overridden = False
    if requested is None or requested <= 0:
        used = suggested
        overridden = requested is not None
    elif abs(requested - suggested) / suggested > CREDIT_TOLERANCE:
        used = suggested
        overridden = True
    else:
        used = requested

    diligence = diligence_module()
    draft = diligence.compose_credit_request(
        opp,
        used,
        rationale or f"Immediate capital work established by diligence moves the viable price to {_money(frontier)}.",
        session.policy,
        in_reply_to=session.message.message_id,
    )
    draft = diligence.dispatch(
        draft, repo=session.repo, policy=session.policy, outbox=session.outbox, actor=actor
    )
    session.credit_draft = draft
    if session.draft is None:
        session.draft = draft

    return {
        "draft_id": draft.draft_id,
        "amount": str(used),
        "suggested": str(suggested),
        "requested": str(requested) if requested is not None else None,
        "amount_overridden": overridden,
        "status": draft.status,
        "requires_approval": draft.requires_approval,
        "sent": draft.status == "sent",
        "note": (
            f"Your amount was more than 25% away from the ${suggested:,.0f} the frontier implies, "
            f"so ${used:,.0f} was used instead. Nothing is sent until a human approves this draft."
            if overridden
            else "Nothing is sent until a human approves this draft."
        ),
    }


# --------------------------------------------------------------------------- notify human


def build_alert(session: ProcessingSession, opp: Opportunity, kind: str) -> Notification:
    """The formatted alert for whichever decision change happened.

    Notifiers declare their own `channel`, so the record says where the alert actually went rather
    than the formatter's default. `pending_request` is passed to `format_threshold_alert` only when
    that formatter accepts it, so this works either side of W10b landing the parameter.
    """
    channel = getattr(session.notifier, "channel", None)
    extra: dict[str, Any] = {} if channel is None else {"channel": channel}

    if kind == "fell_below_threshold":
        from dealsieve.notifications import format_fell_below_alert

        return format_fell_below_alert(
            opp,
            session.run_before,
            session.run_after,
            session.latest_analysis(),
            session.credit_draft,
            **extra,
        )

    if _accepts(format_threshold_alert, "pending_request"):
        extra["pending_request"] = session.pending_request
    return format_threshold_alert(
        opp, session.run_before, session.run_after, session.skeptic_report, **extra
    )


def find_notification(session: ProcessingSession, dedupe_key: str) -> Notification | None:
    """The stored notification under this dedupe key, if the repo has one."""
    if session.opportunity_id is None:  # pragma: no cover - defensive
        return None
    try:
        stored = session.repo.list_notifications(opportunity_id=session.opportunity_id)
    except Exception:  # pragma: no cover - a repo that cannot list is not a reason to alert twice
        logger.exception("could not list notifications for %s", session.opportunity_id)
        return None
    return next((n for n in stored if n.dedupe_key == dedupe_key), None)


def deliver_notification(
    session: ProcessingSession,
    notification: Notification,
    *,
    kind: str,
    note: str = "",
    actor: Actor = Actor.AGENT,
) -> dict[str, Any]:
    """Send an already-persisted notification and record what actually happened.

    Delivery is "the notifier returned"; a notifier that raises did not deliver, and saying otherwise
    is how a human silently misses the one interruption that mattered. On failure the stored row stays
    `delivered=False` (body untouched, so a retry sends the same alert), the error becomes a NOTE on
    the deal, and the exception is re-raised so the tool guard and the safety net both record it.
    """
    try:
        delivery_ref = session.notifier.send(notification)
    except Exception as exc:
        error = f"notification delivery failed: {type(exc).__name__}: {exc}"
        notification.delivered = False
        notification.delivery_ref = None
        session.undelivered_notification = notification
        session.notification_error = error
        try:
            session.repo.update_notification(notification)
        except Exception:  # pragma: no cover - repo failure during error handling
            logger.exception("could not mark notification %s undelivered", notification.notification_id)
        try:
            session.append_event(
                EventType.NOTE,
                f"{error}; the alert is stored undelivered and will be retried",
                {
                    "notification_id": notification.notification_id,
                    "dedupe_key": notification.dedupe_key,
                    "kind": kind,
                    "delivered": False,
                    "error": error,
                },
                actor=actor,
            )
        except Exception:  # pragma: no cover - repo failure during error handling
            logger.exception("could not record the delivery failure as an event")
        raise

    notification.delivered = True
    notification.delivery_ref = delivery_ref
    session.repo.update_notification(notification)
    session.notification = notification
    session.undelivered_notification = None
    session.notification_error = None

    session.append_event(
        EventType.HUMAN_NOTIFIED,
        f"Human notified: {notification.title}",
        {
            "notification_id": notification.notification_id,
            "kind": kind,
            "dedupe_key": notification.dedupe_key,
            "channel": notification.channel.value,
            "delivered": True,
            "delivery_ref": delivery_ref,
            "note": note,
        },
        actor=actor,
    )

    return {
        "notification_id": notification.notification_id,
        "kind": kind,
        "delivered": True,
        "delivery_ref": delivery_ref,
        "channel": notification.channel.value,
        "title": notification.title,
    }


def resume_undelivered_notifications(
    session: ProcessingSession, *, actor: Actor = Actor.SYSTEM
) -> dict[str, Any]:
    """Deliver alerts whose intent was recorded but whose delivery never completed (R3/G3).

    The dedupe row means "we tried", not "the human knows". A re-run of the message -- or the next
    message on the deal -- finishes the delivery it started rather than treating it as handled.
    """
    if session.opportunity_id is None:
        return {"resumed": 0}
    try:
        stored = session.repo.list_notifications(opportunity_id=session.opportunity_id)
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not list notifications for %s", session.opportunity_id)
        return {"resumed": 0}

    resumed: list[str] = []
    for notification in [n for n in stored if not n.delivered]:
        deliver_notification(
            session,
            notification,
            kind=notification.kind,
            note="Delivery resumed: this alert was recorded but never reached you.",
            actor=actor,
        )
        resumed.append(notification.notification_id)
    return {"resumed": len(resumed), "notification_ids": resumed}


def perform_notify_human(
    session: ProcessingSession, note: str = "", *, actor: Actor = Actor.AGENT
) -> dict[str, Any]:
    """Interrupt the human, once, and only for a real change of decision.

    Two things justify the interruption: the deal crossed into REVIEW, or it fell back out of REVIEW
    after diligence. Everything else stays silent.

    R3, intent before delivery: the `Notification` is persisted with `delivered=False` under a unique
    `dedupe_key` *before* anything is sent. A duplicate key that is already delivered means "handled,
    do not alert twice"; a duplicate key that was never delivered means "finish what was started".
    """
    if session.threshold_crossed:
        kind = "threshold_crossed"
    elif session.threshold_lost:
        kind = "fell_below_threshold"
    else:
        return {"skipped": "no threshold crossing"}

    if session.notification is not None:
        return {"skipped": "the human has already been notified for this message"}
    reason = session.phase_check("notify_human")
    if reason:
        return {"skipped": reason}

    opp = session.opportunity()
    if opp is None or session.run_after is None:
        return {"skipped": "no completed underwriting run"}

    notification = build_alert(session, opp, kind)
    notification.dedupe_key = f"{opp.opportunity_id}:{session.run_after.run_id}:{kind}"
    notification.delivered = False
    notification.delivery_ref = None

    try:
        session.repo.store_notification(notification)
    except duplicate_notification_error():
        existing = find_notification(session, notification.dedupe_key)
        if existing is None or existing.delivered:
            logger.info("notification %s already delivered; not alerting again", notification.dedupe_key)
            return {"skipped": f"already handled: a {kind} alert exists for this run"}
        logger.info("notification %s was never delivered; resuming it", notification.dedupe_key)
        notification = existing

    session.phase_advance("notify_human")
    return deliver_notification(session, notification, kind=kind, note=note, actor=actor)


# --------------------------------------------------------------------------- tool factory


def make_tools(session: ProcessingSession) -> list[Any]:
    """Build the @tool closures bound to one ProcessingSession.

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
    def analyze_document(filename: str) -> dict:
        """Read one attached document -- its text and its photographs -- and record what it establishes.

        Use this for every PDF or image attached to the message, before underwriting. The inspector
        agent produces findings with page and photo provenance, answers to whichever diligence
        questions the document settles, and any capital work it prices. Every capex figure is checked
        against the document's own text before it counts; unverifiable ones are rejected and returned
        under "rejected_capex". Day-one capital work changes the basis the next underwriting run uses,
        so always call underwrite afterwards.

        Args:
            filename: The exact attachment filename to read.

        Returns:
            The document type, how many findings and images it produced, which open diligence topics
            it answered, which are still open, the immediate capex now established for the whole
            opportunity, any rejected capex, and the next step.
        """
        return _guard("analyze_document", lambda: perform_analyze_document(session, filename))

    @tool
    def underwrite() -> dict:
        """Underwrite the opportunity with the deterministic engine under the frozen policy.

        Normalizes the broker's economics, computes financing, evaluates every gate, solves the
        viability frontier and classifies the deal DEAD / WATCH / NEAR / REVIEW. You do not do any
        arithmetic yourself. Call this once, after record_claims.

        Returns:
            The run id, the resulting status, normalized cap rate, DSCR, immediate capex and all-in
            basis, the maximum viable price, the binding constraints, whether this crossed into
            REVIEW or fell back out of it, and what to do next.
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
    def request_diligence(items: list[DiligenceItem]) -> dict:
        """Start chasing the broker for the evidence this deal is missing.

        Only allowed on a REVIEW opportunity that had a skeptic review in this message, once per
        message. Pass the skeptic's concerns that carry a question for the broker -- nothing else:
        the code reconciles your items against that report, chases the skeptic's own wording, and
        drops anything that does not correspond to one of its concerns (they come back under
        "dropped"). Each surviving item becomes a tracked request, followed up on the policy cadence
        until it is answered. Whether the message is sent now or waits for a human tap is the
        outreach policy's decision, not yours.

        Args:
            items: The questions to chase, each with a short topic and one specific question.

        Returns:
            The request ids and topics actually raised, the items that were dropped, the draft id,
            whether it is awaiting approval, and the outbox reference when it was sent. Returns
            {"skipped": reason} when it is not warranted.
        """
        payload = [i if isinstance(i, dict) else i.model_dump() for i in items]
        return _guard("request_diligence", lambda: perform_request_diligence(session, payload))

    @tool
    def request_price_adjustment(amount: float, rationale: str) -> dict:
        """Draft a price-credit request for a deal that diligence pushed back out of reach.

        Only allowed when this message's underwriting fell out of REVIEW, or left the deal NEAR or
        WATCH with a viability frontier AND this message brought something new -- a document you
        analyzed or a change in the asking price. This is money talk: the draft always waits for explicit
        human approval, and nothing is sent. The code recomputes the credit from the stored frontier
        and will override your figure if it is more than 25% away from it.

        Args:
            amount: The credit to ask for, in dollars: current asking price minus the maximum
                viable price.
            rationale: One or two lines on what the diligence established that justifies it.

        Returns:
            The draft id, the amount actually used, whether your figure was overridden, and the
            pending status. Returns {"skipped": reason} when it is not warranted.
        """
        return _guard(
            "request_price_adjustment",
            lambda: perform_request_price_adjustment(session, amount, rationale),
        )

    @tool
    def notify_human(note: str) -> dict:
        """Interrupt the human, once, because this deal's decision just changed.

        Only allowed when the latest underwriting run moved the opportunity into REVIEW, or moved it
        back out of REVIEW after diligence. Every other situation stays silent.

        Args:
            note: One short line on why this deserves attention right now.

        Returns:
            The notification id and delivery result, or {"skipped": "no threshold crossing"}.
        """
        return _guard("notify_human", lambda: perform_notify_human(session, note))

    return [
        record_claims,
        analyze_document,
        underwrite,
        request_skeptic_review,
        request_diligence,
        request_price_adjustment,
        notify_human,
    ]


__all__ = [
    "CREDIT_ROUNDING",
    "CREDIT_TOLERANCE",
    "DILIGENCE_MATCH_THRESHOLD",
    "OPEN_REQUEST_STATUSES",
    "PHASE_ORDER_TEXT",
    "REPEATABLE_TOOLS",
    "TOOL_RANK",
    "DiligenceItem",
    "ProcessingSession",
    "aggregate_capex_items",
    "build_alert",
    "capex_rejection_reason",
    "deliver_notification",
    "diligence_items_from",
    "diligence_items_match",
    "diligence_module",
    "document_amounts",
    "duplicate_notification_error",
    "evidence_from_findings",
    "find_attachment",
    "find_notification",
    "finding_location",
    "make_tools",
    "perform_analyze_document",
    "perform_notify_human",
    "perform_record_claims",
    "perform_request_diligence",
    "perform_request_price_adjustment",
    "perform_skeptic_review",
    "perform_underwrite",
    "reconcile_diligence_items",
    "resume_undelivered_notifications",
    "suggested_credit",
    "suggested_questions",
    "verify_capex_items",
]
