"""Small in-memory fakes so W3 can be tested before W1/W2/W4 land.

These are deliberately dumb: they implement exactly the slice of the interfaces that
`dealsieve.agents` and `dealsieve.pipeline` call, with the same semantics the contracts promise
(append-only events with sequential seq, immutable runs, deal numbers starting at 101).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from dealsieve.schemas import (
    Attachment,
    Channel,
    ConstraintKind,
    Evidence,
    ExpenseClaims,
    ExpenseLine,
    ExtractedClaims,
    FinancingResult,
    GateResult,
    InboundMessage,
    NormalizedEconomics,
    Notification,
    NotificationAction,
    Opportunity,
    OpportunityEvent,
    OpportunityStatus,
    OutboundDraft,
    Property,
    SkepticConcern,
    SkepticReport,
    UnderwritingResult,
    ViabilityFrontier,
    WorkingValues,
)

# --------------------------------------------------------------------------- fake repo


@dataclass
class FakeRepo:
    """In-memory stand-in for `dealsieve.persistence.Repo`."""

    messages: dict[str, InboundMessage] = field(default_factory=dict)
    message_links: dict[str, str] = field(default_factory=dict)
    properties: dict[str, Property] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    events: list[OpportunityEvent] = field(default_factory=list)
    evidence: dict[str, list[Evidence]] = field(default_factory=dict)
    documents: list[tuple[str, str, str, str, str | None]] = field(default_factory=list)
    runs: dict[str, UnderwritingResult] = field(default_factory=dict)
    policy_versions: list[tuple[str, str, str]] = field(default_factory=list)
    skeptic_reports: list[SkepticReport] = field(default_factory=list)
    drafts: dict[str, OutboundDraft] = field(default_factory=dict)
    notifications: list[Notification] = field(default_factory=list)
    _next_deal: int = 101

    # messages
    def store_inbound_message(self, message: InboundMessage) -> None:
        self.messages[message.message_id] = message

    def message_exists(self, message_id: str) -> bool:
        return message_id in self.messages

    def get_message(self, message_id: str) -> InboundMessage | None:
        return self.messages.get(message_id)

    def link_message_to_opportunity(self, message_id: str, opportunity_id: str) -> None:
        self.message_links[message_id] = opportunity_id

    def find_opportunity_id_by_message_id(self, message_id: str) -> str | None:
        return self.message_links.get(message_id)

    # properties & opportunities
    def upsert_property(self, prop: Property) -> Property:
        for existing in self.properties.values():
            if existing.normalized_address == prop.normalized_address:
                return existing
        self.properties[prop.property_id] = prop
        return prop

    def create_opportunity(self, opp: Opportunity) -> Opportunity:
        opp.deal_number = self._next_deal
        self._next_deal += 1
        self.opportunities[opp.opportunity_id] = opp.model_copy(deep=True)
        return self.opportunities[opp.opportunity_id]

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        stored = self.opportunities.get(opportunity_id)
        return stored.model_copy(deep=True) if stored else None

    def save_opportunity(self, opp: Opportunity) -> Opportunity:
        self.opportunities[opp.opportunity_id] = opp.model_copy(deep=True)
        return self.opportunities[opp.opportunity_id]

    # events
    def append_event(self, event: OpportunityEvent) -> OpportunityEvent:
        seq = sum(1 for e in self.events if e.opportunity_id == event.opportunity_id) + 1
        stored = event.model_copy(update={"seq": seq})
        self.events.append(stored)
        return stored

    def list_events(self, opportunity_id: str) -> list[OpportunityEvent]:
        return [e for e in self.events if e.opportunity_id == opportunity_id]

    def event_types(self, opportunity_id: str) -> list[str]:
        return [e.type.value for e in self.list_events(opportunity_id)]

    # evidence & documents
    def store_evidence(self, opportunity_id: str, evidence: list[Evidence]) -> None:
        self.evidence.setdefault(opportunity_id, []).extend(evidence)

    def list_evidence(self, opportunity_id: str) -> list[Evidence]:
        return list(self.evidence.get(opportunity_id, []))

    def store_document(
        self, opportunity_id: str, message_id: str, filename: str, sha256: str, text: str | None
    ) -> None:
        self.documents.append((opportunity_id, message_id, filename, sha256, text))

    # underwriting
    def store_underwriting_run(self, run: UnderwritingResult) -> None:
        self.runs[run.run_id] = run.model_copy(deep=True)

    def get_underwriting_run(self, run_id: str) -> UnderwritingResult | None:
        stored = self.runs.get(run_id)
        return stored.model_copy(deep=True) if stored else None

    def list_underwriting_runs(self, opportunity_id: str) -> list[UnderwritingResult]:
        return [r for r in self.runs.values() if r.opportunity_id == opportunity_id]

    def record_policy_version(self, policy_version: str, name: str, raw_yaml: str) -> None:
        self.policy_versions.append((policy_version, name, raw_yaml))

    # skeptic, drafts, notifications
    def store_skeptic_report(self, report: SkepticReport) -> None:
        self.skeptic_reports.append(report)

    def list_skeptic_reports(self, opportunity_id: str) -> list[SkepticReport]:
        return [r for r in self.skeptic_reports if r.opportunity_id == opportunity_id]

    def store_draft(self, draft: OutboundDraft) -> None:
        self.drafts[draft.draft_id] = draft

    def list_drafts(self, status: str | None = None, opportunity_id: str | None = None) -> list[OutboundDraft]:
        return [
            d
            for d in self.drafts.values()
            if (status is None or d.status == status)
            and (opportunity_id is None or d.opportunity_id == opportunity_id)
        ]

    def store_notification(self, notification: Notification) -> None:
        self.notifications.append(notification)

    def list_notifications(self, opportunity_id: str | None = None) -> list[Notification]:
        return [
            n for n in self.notifications if opportunity_id is None or n.opportunity_id == opportunity_id
        ]


# --------------------------------------------------------------------------- fake finance


def make_working_values(price: Decimal = Decimal("1550000")) -> WorkingValues:
    return WorkingValues(
        asking_price=price,
        gross_scheduled_income=Decimal("180000"),
        other_income=Decimal("0"),
        stated_vacancy_pct=Decimal("0"),
        stated_expenses=ExpenseClaims(total=Decimal("54000")),
        stated_noi=Decimal("126000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
    )


def make_run(
    opportunity_id: str,
    status: OpportunityStatus,
    *,
    price: Decimal = Decimal("1550000"),
    cap: Decimal = Decimal("0.0642"),
    dscr: Decimal = Decimal("1.05"),
    max_viable: Decimal | None = Decimal("1300000"),
    structural_failures: list[str] | None = None,
    trigger_event_id: str | None = None,
) -> UnderwritingResult:
    """A structurally complete UnderwritingResult with plausible numbers, for gating tests."""
    normalized = NormalizedEconomics(
        gross_potential_rent=Decimal("180000"),
        other_income=Decimal("0"),
        vacancy_loss=Decimal("9000"),
        effective_gross_income=Decimal("171000"),
        expenses=[
            ExpenseLine(
                name="Property tax",
                broker=Decimal("15500"),
                normalized=price * Decimal("0.0125"),
                basis="1.25% of price (reassessed at sale)",
            )
        ],
        total_expenses=Decimal("71500"),
        noi=cap * price,
        broker_noi=Decimal("126000"),
        broker_cap_rate=Decimal("126000") / price,
        normalized_cap_rate=cap,
        price_per_sqft=price / Decimal("20000"),
        noi_per_sqft=(cap * price) / Decimal("20000"),
    )
    financing = FinancingResult(
        purchase_price=price,
        closing_costs=price * Decimal("0.02"),
        total_acquisition_cost=price * Decimal("1.02"),
        equity_deployed=Decimal("500000"),
        loan_amount=price * Decimal("0.7"),
        ltv=Decimal("0.70"),
        interest_rate=Decimal("0.0675"),
        amortization_years=25,
        monthly_debt_service=Decimal("7500"),
        annual_debt_service=Decimal("90000"),
        dscr=dscr,
        cash_flow_after_debt=Decimal("10000"),
        cash_on_cash=Decimal("0.02"),
        year1_principal_paydown=Decimal("22000"),
    )
    failures = structural_failures or []
    gates = [
        GateResult(
            gate="tenant_count_min",
            kind=ConstraintKind.STRUCTURAL,
            description="tenant count",
            comparator=">=",
            threshold=5,
            actual=8,
            passed="tenant_count_min" not in failures,
            price_dependent=False,
        ),
        GateResult(
            gate="largest_tenant_pct_max",
            kind=ConstraintKind.STRUCTURAL,
            description="largest tenant share",
            comparator="<=",
            threshold=Decimal("0.25"),
            actual=Decimal("0.19"),
            passed="largest_tenant_pct_max" not in failures,
            price_dependent=False,
        ),
        GateResult(
            gate="min_normalized_cap_rate",
            kind=ConstraintKind.ECONOMIC,
            description="normalized cap rate",
            comparator=">=",
            threshold=Decimal("0.08"),
            actual=cap,
            passed=cap >= Decimal("0.08"),
            price_dependent=True,
        ),
        GateResult(
            gate="min_base_dscr",
            kind=ConstraintKind.ECONOMIC,
            description="base DSCR",
            comparator=">=",
            threshold=Decimal("1.35"),
            actual=dscr,
            passed=dscr >= Decimal("1.35"),
            price_dependent=True,
        ),
    ]
    viability = ViabilityFrontier(
        current_price=price,
        max_viable_price=max_viable,
        distance_pct=None if max_viable is None else (price - max_viable) / price,
        binding_constraints=[] if status == OpportunityStatus.REVIEW else ["min_normalized_cap_rate"],
        structural_failures=failures,
    )
    return UnderwritingResult(
        opportunity_id=opportunity_id,
        policy_version="v1-test",
        trigger_event_id=trigger_event_id,
        inputs=make_working_values(price),
        normalized=normalized,
        financing=financing,
        stress=[],
        gates=gates,
        status=status,
        viability=viability,
        comparison=[],
        failure_summary=f"test run: {status.value}",
    )


def make_skeptic_report(opportunity_id: str, run_id: str) -> SkepticReport:
    return SkepticReport(
        opportunity_id=opportunity_id,
        run_id=run_id,
        verdict="proceed_with_questions",
        summary="Diligence gaps remain.",
        concerns=[
            SkepticConcern(
                topic="roof age",
                severity="high",
                why_it_matters="A new roof erases years of cash flow.",
                evidence_status="missing",
                question_for_broker="How old is the roof?",
            )
        ],
    )


def fake_threshold_alert(
    opportunity: Opportunity,
    previous_run: UnderwritingResult | None,
    new_run: UnderwritingResult,
    skeptic: SkepticReport | None,
    *,
    channel: Channel = Channel.TELEGRAM,
) -> Notification:
    """Stand-in for W4's `format_threshold_alert`."""
    return Notification(
        opportunity_id=opportunity.opportunity_id,
        kind="threshold_crossed",
        channel=channel,
        title=f"DEAL #{opportunity.deal_number} JUST BECAME INVESTABLE",
        body=f"{opportunity.display_name}\n{new_run.failure_summary}",
        actions=[NotificationAction(label="Review", action="review")],
    )


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def fake_repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def inbound_message() -> InboundMessage:
    return InboundMessage(
        message_id="<om-2026-0912-power-inn@brokerage.example>",
        channel=Channel.EMAIL,
        sender="broker@brokerage.example",
        sender_name="Dana Ruiz",
        subject="Off-market: 8-unit small-bay industrial, Sacramento",
        body_text="Asking $1,550,000, NOI $126,000. OM attached.",
        attachments=[
            Attachment(
                filename="Power_Inn_OM.md",
                content_type="text/markdown",
                sha256="a" * 64,
                size_bytes=2048,
                text="# Offering Memorandum\nRent roll ...",
            )
        ],
        thread_id="<om-2026-0912-power-inn@brokerage.example>",
    )


@pytest.fixture
def claims() -> ExtractedClaims:
    return ExtractedClaims(
        address_line="8330 Power Inn Road",
        city="Sacramento",
        state="CA",
        postal_code="95826",
        asking_price=Decimal("1550000"),
        stated_noi=Decimal("126000"),
        stated_gross_income=Decimal("180000"),
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
        evidence=[
            Evidence(
                field="asking_price",
                value=1550000,
                source_document="<om-2026-0912-power-inn@brokerage.example>",
                location="email body",
                quote="Asking $1,550,000",
                confidence=1.0,
            )
        ],
        missing_fields=["roof_age"],
    )


@pytest.fixture
def recording_notifier():
    class _Recorder:
        name = "recording"
        channel = Channel.MANUAL

        def __init__(self) -> None:
            self.sent: list[Notification] = []

        def send(self, notification: Notification) -> str | None:
            self.sent.append(notification)
            return f"recorded-{len(self.sent)}"

    return _Recorder()


@pytest.fixture
def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "underwrite",
            "description": "Run the engine.",
            "inputSchema": {"json": {"type": "object", "properties": {}, "required": []}},
        },
        {
            "name": "notify_human",
            "description": "Interrupt the human.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                }
            },
        },
    ]
