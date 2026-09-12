"""DealSieve shared contracts.

Rules that these types encode:
- Money and rates are ``Decimal`` inside Python (finance truth) and serialize to
  plain JSON numbers for the API and dashboard.
- Extracted claims (probabilistic, from a model) are a different type from
  WorkingValues (reconciled, deterministic inputs). Nothing underwrites a claim.
- Events and underwriting runs are immutable records. Opportunities carry
  derived current state only.
- Nothing in this module performs finance. It only describes shapes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# --------------------------------------------------------------------------- primitives

Money = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
"""Currency amount (USD). Decimal in Python, number in JSON."""

Rate = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
"""Fractional rate: 0.08 means 8%. Decimal in Python, number in JSON."""


def now_utc() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class DSModel(BaseModel):
    """Base for all DealSieve contracts."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, use_enum_values=False)


# --------------------------------------------------------------------------- enums


class OpportunityStatus(StrEnum):
    NEW = "NEW"
    SCREENING = "SCREENING"
    DEAD = "DEAD"        # structural failure; repricing does not fix it; not monitored
    WATCH = "WATCH"      # economically unacceptable today; viability frontier stored; monitored
    NEAR = "NEAR"        # within classification.near_threshold_pct of max viable price
    REVIEW = "REVIEW"    # passes every hard gate; human attention justified (not BUY)


class EventType(StrEnum):
    DEAL_DISCOVERED = "DEAL_DISCOVERED"
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    DOCUMENT_ADDED = "DOCUMENT_ADDED"
    CLAIMS_EXTRACTED = "CLAIMS_EXTRACTED"
    ASKING_PRICE_CHANGED = "ASKING_PRICE_CHANGED"
    NOI_CHANGED = "NOI_CHANGED"
    RENT_ROLL_UPDATED = "RENT_ROLL_UPDATED"
    FINANCING_CHANGED = "FINANCING_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    UNDERWRITING_COMPLETED = "UNDERWRITING_COMPLETED"
    STATUS_CHANGED = "STATUS_CHANGED"
    SKEPTIC_REVIEW_COMPLETED = "SKEPTIC_REVIEW_COMPLETED"
    HUMAN_NOTIFIED = "HUMAN_NOTIFIED"
    BROKER_DRAFT_CREATED = "BROKER_DRAFT_CREATED"
    HUMAN_APPROVED_DRAFT = "HUMAN_APPROVED_DRAFT"
    HUMAN_REJECTED_DRAFT = "HUMAN_REJECTED_DRAFT"
    BROKER_MESSAGE_SENT = "BROKER_MESSAGE_SENT"
    NOTE = "NOTE"


class Channel(StrEnum):
    EMAIL = "email"
    TELEGRAM = "telegram"
    URL = "url"
    MANUAL = "manual"
    FIXTURE = "fixture"


class ConstraintKind(StrEnum):
    STRUCTURAL = "structural"   # failing it => DEAD; price cannot fix it
    ECONOMIC = "economic"       # failing it => WATCH/NEAR; a price exists that fixes it


class Actor(StrEnum):
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"


class ModelPurpose(StrEnum):
    ACQUISITION = "acquisition"
    SKEPTIC = "skeptic"
    EXTRACTION = "extraction"


# --------------------------------------------------------------------------- inbound


class Attachment(DSModel):
    filename: str
    content_type: str = "application/octet-stream"
    sha256: str
    size_bytes: int
    text: str | None = Field(default=None, description="Extracted text when available (PDF/TXT/MD).")
    stored_path: str | None = None


class InboundMessage(DSModel):
    """Channel-agnostic inbound message. Email, Telegram, URL paste and fixtures all become this."""

    message_id: str = Field(description="Stable id: email Message-ID, Telegram update id, or content hash.")
    channel: Channel
    received_at: datetime = Field(default_factory=now_utc)
    sender: str | None = None
    sender_name: str | None = None
    subject: str | None = None
    body_text: str
    attachments: list[Attachment] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)
    in_reply_to: str | None = None
    thread_id: str | None = None
    raw_ref: str | None = Field(default=None, description="Path/key of the preserved raw payload (MIME, JSON).")


# --------------------------------------------------------------------------- evidence & claims


class Evidence(DSModel):
    """A single observed claim with provenance. Contradictory evidence is preserved, never deleted."""

    evidence_id: str = Field(default_factory=lambda: new_id("ev"))
    field: str
    value: Any
    source_document: str = Field(description="message_id or attachment filename the value came from.")
    location: str | None = Field(default=None, description='e.g. "email body", "OM page 7", "rent roll row 3"')
    quote: str | None = Field(default=None, description="Short verbatim excerpt supporting the value.")
    source_timestamp: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    observed_by: str = "acquisition_agent"


class TenantClaim(DSModel):
    name: str
    suite: str | None = None
    sqft: int | None = None
    annual_rent: Money | None = None
    lease_end: str | None = Field(default=None, description="ISO date or free text as stated.")
    notes: str | None = None


class ExpenseClaims(DSModel):
    """Operating expenses as stated by the source (annual)."""

    property_tax: Money | None = None
    insurance: Money | None = None
    repairs_maintenance: Money | None = None
    utilities: Money | None = None
    management: Money | None = None
    cam_other: Money | None = None
    total: Money | None = None


class ExtractedClaims(DSModel):
    """What a source asserts about a property. Probabilistic output of a model.

    Never used for arithmetic directly: it is reconciled into WorkingValues first.
    """

    address_line: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    apn: str | None = None
    property_type: str | None = None
    year_built: int | None = None
    building_sqft: int | None = None
    asking_price: Money | None = None
    stated_noi: Money | None = None
    stated_cap_rate: Rate | None = None
    stated_gross_income: Money | None = Field(default=None, description="Annual gross scheduled income.")
    stated_other_income: Money | None = None
    stated_vacancy_pct: Rate | None = None
    stated_expenses: ExpenseClaims | None = None
    tenant_count: int | None = None
    occupancy_pct: Rate | None = None
    largest_tenant_pct: Rate | None = Field(default=None, description="Largest tenant's share of gross rent.")
    tenants: list[TenantClaim] = Field(default_factory=list)
    broker_property_ref: str | None = None
    listing_url: str | None = None
    seller_financing_offered: bool | None = None
    is_price_change: bool = Field(default=False, description="True if this message announces a new asking price.")
    evidence: list[Evidence] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    notes: str | None = None


class WorkingValues(DSModel):
    """Reconciled, deterministic inputs to underwriting. One working value per field, with provenance."""

    asking_price: Money
    gross_scheduled_income: Money = Field(description="Annual, at the current rent roll.")
    other_income: Money = Decimal("0")
    stated_vacancy_pct: Rate = Decimal("0")
    stated_expenses: ExpenseClaims = Field(default_factory=ExpenseClaims)
    stated_noi: Money | None = None
    building_sqft: int | None = None
    tenant_count: int | None = None
    largest_tenant_pct: Rate | None = None
    occupancy_pct: Rate | None = None
    tenants: list[TenantClaim] = Field(default_factory=list)
    property_type: str | None = None
    provenance: dict[str, str] = Field(default_factory=dict, description="field -> evidence_id")
    conflicts: list[str] = Field(
        default_factory=list, description="Unresolved contradictions, human-readable. Never silently dropped."
    )


# --------------------------------------------------------------------------- underwriting


class ExpenseLine(DSModel):
    name: str
    broker: Money | None = Field(default=None, description="As stated by the source, if stated.")
    normalized: Money
    basis: str = Field(description='How DealSieve derived it, e.g. "5% of EGI", "1.25% of price", "as stated".')


class NormalizedEconomics(DSModel):
    gross_potential_rent: Money
    other_income: Money
    vacancy_loss: Money
    effective_gross_income: Money
    expenses: list[ExpenseLine]
    total_expenses: Money
    noi: Money
    broker_noi: Money | None = None
    broker_cap_rate: Rate | None = None
    normalized_cap_rate: Rate
    price_per_sqft: Money | None = None
    noi_per_sqft: Money | None = None


class FinancingResult(DSModel):
    purchase_price: Money
    closing_costs: Money
    total_acquisition_cost: Money
    equity_deployed: Money
    loan_amount: Money
    ltv: Rate
    interest_rate: Rate
    amortization_years: int
    monthly_debt_service: Money
    annual_debt_service: Money
    dscr: Rate
    cash_flow_after_debt: Money
    cash_on_cash: Rate
    year1_principal_paydown: Money


class StressResult(DSModel):
    scenario: str
    noi: Money
    dscr: Rate
    cash_flow_after_debt: Money
    covers_debt: bool


class GateResult(DSModel):
    gate: str = Field(description='Stable key, e.g. "min_normalized_cap_rate", "largest_tenant_pct_max".')
    kind: ConstraintKind
    description: str
    comparator: Literal[">=", "<=", "==", "between"]
    threshold: Decimal | int | None
    actual: Decimal | int | None
    passed: bool
    price_dependent: bool = Field(description="True if changing purchase price can flip this gate.")


class ViabilityPath(DSModel):
    variable: str
    current_value: Decimal
    required_value: Decimal
    description: str


class ViabilityFrontier(DSModel):
    """The counterfactual: what exact change would make this opportunity pass."""

    current_price: Money
    max_viable_price: Money | None = Field(default=None, description="None when no price fixes it (structural).")
    distance_pct: Rate | None = Field(default=None, description="(current - max_viable) / current; None if structural.")
    binding_constraints: list[str] = Field(default_factory=list, description="Gate keys that bind at the frontier.")
    paths: list[ViabilityPath] = Field(default_factory=list)
    structural_failures: list[str] = Field(default_factory=list)


class ComparisonRow(DSModel):
    metric: str
    broker: str | None
    dealsieve: str
    note: str | None = None


class UnderwritingResult(DSModel):
    """Immutable. One per underwriting run. Never updated after creation."""

    run_id: str = Field(default_factory=lambda: new_id("run"))
    opportunity_id: str
    policy_version: str
    created_at: datetime = Field(default_factory=now_utc)
    trigger_event_id: str | None = None
    inputs: WorkingValues
    normalized: NormalizedEconomics
    financing: FinancingResult
    stress: list[StressResult]
    gates: list[GateResult]
    status: OpportunityStatus
    viability: ViabilityFrontier
    comparison: list[ComparisonRow] = Field(default_factory=list, description="Broker math vs DealSieve math.")
    failure_summary: str = Field(description='One line. e.g. "Fails on valuation: cap 6.65% < 8.0%, DSCR 1.19x < 1.35x"')


# --------------------------------------------------------------------------- opportunity, property, events


class Property(DSModel):
    property_id: str = Field(default_factory=lambda: new_id("prop"))
    canonical_address: str
    normalized_address: str = Field(description="Deterministic key from identity.normalize_address().")
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    apn: str | None = None
    building_sqft: int | None = None
    property_type: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class Opportunity(DSModel):
    """Derived current state. History lives in events and underwriting runs."""

    opportunity_id: str = Field(default_factory=lambda: new_id("opp"))
    deal_number: int | None = Field(default=None, description="Human-friendly sequential number, e.g. 184.")
    property_id: str
    display_name: str
    status: OpportunityStatus = OpportunityStatus.NEW
    previous_status: OpportunityStatus | None = None
    current_asking_price: Money | None = None
    working_values: WorkingValues | None = None
    latest_run_id: str | None = None
    viability: ViabilityFrontier | None = None
    broker_email: str | None = None
    broker_name: str | None = None
    broker_property_ref: str | None = None
    listing_url: str | None = None
    reason_summary: str | None = Field(default=None, description="Why it is in its current status, one line.")
    human_attention_required: bool = False
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class OpportunityEvent(DSModel):
    """Immutable, append-only. seq is assigned by the repository on append."""

    event_id: str = Field(default_factory=lambda: new_id("evt"))
    opportunity_id: str
    seq: int | None = None
    type: EventType
    occurred_at: datetime = Field(default_factory=now_utc)
    actor: Actor = Actor.AGENT
    source_message_id: str | None = None
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- identity


class IdentityKeys(DSModel):
    """Everything usable to decide whether two inputs describe the same property."""

    normalized_address: str | None = None
    apn: str | None = None
    broker_property_ref: str | None = None
    listing_url: str | None = None
    attachment_fingerprints: list[str] = Field(default_factory=list)
    building_name: str | None = None
    city: str | None = None
    building_sqft: int | None = None
    sender_email: str | None = None
    thread_id: str | None = None


class ResolutionResult(DSModel):
    opportunity_id: str | None
    property_id: str | None
    created: bool
    confidence: float = Field(ge=0.0, le=1.0)
    matched_on: list[str] = Field(default_factory=list)
    ambiguous_candidates: list[dict[str, Any]] = Field(default_factory=list)
    needs_human: bool = Field(default=False, description="True when candidates exist but none is confident.")


# --------------------------------------------------------------------------- skeptic, notifications, drafts


class SkepticConcern(DSModel):
    topic: str
    severity: Literal["low", "medium", "high"]
    why_it_matters: str
    evidence_status: Literal["missing", "weak", "contradicted", "unverified"]
    question_for_broker: str | None = None


class SkepticReport(DSModel):
    report_id: str = Field(default_factory=lambda: new_id("skp"))
    opportunity_id: str
    run_id: str
    verdict: Literal["proceed", "proceed_with_questions", "reject"]
    summary: str
    concerns: list[SkepticConcern] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


class NotificationAction(DSModel):
    label: str
    action: Literal["review", "draft_questions", "ignore", "approve", "edit", "reject"]


class Notification(DSModel):
    notification_id: str = Field(default_factory=lambda: new_id("ntf"))
    opportunity_id: str
    kind: Literal["threshold_crossed", "structural_dead", "status_update", "draft_pending"]
    channel: Channel
    title: str
    body: str
    actions: list[NotificationAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)
    delivered: bool = False
    delivery_ref: str | None = None


class OutboundDraft(DSModel):
    """A broker message awaiting explicit human approval. Nothing is sent autonomously."""

    draft_id: str = Field(default_factory=lambda: new_id("drf"))
    opportunity_id: str
    to_email: str | None
    subject: str
    body: str
    questions: list[str] = Field(default_factory=list)
    status: Literal["pending", "approved", "rejected", "sent"] = "pending"
    created_at: datetime = Field(default_factory=now_utc)
    decided_at: datetime | None = None
    sent_at: datetime | None = None


# --------------------------------------------------------------------------- outcomes & dashboard


class ProcessingOutcome(DSModel):
    """Result of processing one inbound message end to end."""

    message_id: str
    opportunity_id: str | None
    created_opportunity: bool = False
    status_before: OpportunityStatus | None
    status_after: OpportunityStatus | None
    run_id: str | None = None
    events_created: list[str] = Field(default_factory=list)
    notified_human: bool = False
    notification_id: str | None = None
    skeptic_report_id: str | None = None
    draft_id: str | None = None
    summary: str
    model_backend: str


class DashboardStats(DSModel):
    encountered: int
    dead: int
    watch: int
    near: int
    review: int
    conditions_changed_7d: int
    threshold_crossings_7d: int
    human_interruptions_7d: int
    policy_version: str


class WatchlistItem(DSModel):
    opportunity_id: str
    deal_number: int | None
    display_name: str
    status: OpportunityStatus
    current_asking_price: Money | None
    max_viable_price: Money | None
    distance_pct: Rate | None
    binding_constraints: list[str]
    reason_summary: str | None
    updated_at: datetime


class OpportunityDetail(DSModel):
    opportunity: Opportunity
    property: Property
    latest_run: UnderwritingResult | None
    runs: list[UnderwritingResult]
    events: list[OpportunityEvent]
    evidence: list[Evidence]
    skeptic_reports: list[SkepticReport]
    drafts: list[OutboundDraft]
    notifications: list[Notification]


__all__ = [
    "Actor",
    "Attachment",
    "Channel",
    "ComparisonRow",
    "ConstraintKind",
    "DSModel",
    "DashboardStats",
    "Evidence",
    "EventType",
    "ExpenseClaims",
    "ExpenseLine",
    "ExtractedClaims",
    "FinancingResult",
    "GateResult",
    "IdentityKeys",
    "InboundMessage",
    "ModelPurpose",
    "Money",
    "NormalizedEconomics",
    "Notification",
    "NotificationAction",
    "Opportunity",
    "OpportunityDetail",
    "OpportunityEvent",
    "OpportunityStatus",
    "OutboundDraft",
    "ProcessingOutcome",
    "Property",
    "Rate",
    "ResolutionResult",
    "SkepticConcern",
    "SkepticReport",
    "StressResult",
    "TenantClaim",
    "UnderwritingResult",
    "ViabilityFrontier",
    "ViabilityPath",
    "WatchlistItem",
    "WorkingValues",
    "new_id",
    "now_utc",
]
