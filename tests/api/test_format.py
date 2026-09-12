"""Golden-text test for format_threshold_alert.

Builds an Opportunity/UnderwritingResult pair by hand from the schemas (mirroring the demo property from
docs/CONTRACTS.md: DEAL #101, Power Inn Rd, $1,550,000 -> $1,250,000, normalized cap 6.42% -> 8.27%, DSCR
1.05x -> 1.43x, largest tenant 19%). Does not need W1/W2 -- everything here is constructed directly from
dealsieve.schemas.
"""

from __future__ import annotations

from decimal import Decimal

from dealsieve.notifications import format_threshold_alert
from dealsieve.notifications.console import render_box
from dealsieve.schemas import (
    ComparisonRow,
    ConstraintKind,
    FinancingResult,
    GateResult,
    NormalizedEconomics,
    Opportunity,
    OpportunityStatus,
    SkepticConcern,
    SkepticReport,
    UnderwritingResult,
    ViabilityFrontier,
    WorkingValues,
)

EXPECTED_BODY = """8-unit small-bay industrial, Power Inn Rd, Sacramento

Price            $1,550,000 -> $1,250,000
Normalized cap   6.42% -> 8.27%   PASS
DSCR             1.05x -> 1.43x   PASS
Largest tenant   19%              PASS

Previously failed solely on valuation.

Still unresolved:
- roof age
- Phase I environmental
- CAM reconciliation"""


def _gates(*, cap_passed: bool, dscr_passed: bool, tenant_passed: bool = True) -> list[GateResult]:
    return [
        GateResult(
            gate="tenant_count_min",
            kind=ConstraintKind.STRUCTURAL,
            description="tenant count",
            comparator=">=",
            threshold=5,
            actual=8,
            passed=True,
            price_dependent=False,
        ),
        GateResult(
            gate="largest_tenant_pct_max",
            kind=ConstraintKind.STRUCTURAL,
            description="largest tenant share",
            comparator="<=",
            threshold=Decimal("0.25"),
            actual=Decimal("0.19"),
            passed=tenant_passed,
            price_dependent=False,
        ),
        GateResult(
            gate="absolute_max_price",
            kind=ConstraintKind.ECONOMIC,
            description="price ceiling",
            comparator="<=",
            threshold=Decimal("2000000"),
            actual=Decimal("1550000"),
            passed=True,
            price_dependent=True,
        ),
        GateResult(
            gate="max_ltv",
            kind=ConstraintKind.ECONOMIC,
            description="loan to value",
            comparator="<=",
            threshold=Decimal("0.75"),
            actual=Decimal("0.5"),
            passed=True,
            price_dependent=True,
        ),
        GateResult(
            gate="min_normalized_cap_rate",
            kind=ConstraintKind.ECONOMIC,
            description="normalized cap rate",
            comparator=">=",
            threshold=Decimal("0.08"),
            actual=Decimal("0.0642"),
            passed=cap_passed,
            price_dependent=True,
        ),
        GateResult(
            gate="min_base_dscr",
            kind=ConstraintKind.ECONOMIC,
            description="debt service coverage",
            comparator=">=",
            threshold=Decimal("1.35"),
            actual=Decimal("1.05"),
            passed=dscr_passed,
            price_dependent=True,
        ),
    ]


def _normalized(*, cap_rate: Decimal, noi: Decimal) -> NormalizedEconomics:
    return NormalizedEconomics(
        gross_potential_rent=Decimal("180000"),
        other_income=Decimal("0"),
        vacancy_loss=Decimal("9000"),
        effective_gross_income=Decimal("171000"),
        expenses=[],
        total_expenses=Decimal("171000") - noi,
        noi=noi,
        broker_noi=Decimal("126000"),
        broker_cap_rate=Decimal("0.0813"),
        normalized_cap_rate=cap_rate,
    )


def _financing(*, price: Decimal, dscr: Decimal) -> FinancingResult:
    closing_costs = price * Decimal("0.02")
    return FinancingResult(
        purchase_price=price,
        closing_costs=closing_costs,
        total_acquisition_cost=price + closing_costs,
        equity_deployed=Decimal("425000"),
        loan_amount=price + closing_costs - Decimal("425000"),
        ltv=Decimal("0.5"),
        interest_rate=Decimal("0.07"),
        amortization_years=25,
        monthly_debt_service=Decimal("8000"),
        annual_debt_service=Decimal("96000"),
        dscr=dscr,
        cash_flow_after_debt=Decimal("3500"),
        cash_on_cash=Decimal("0.008"),
        year1_principal_paydown=Decimal("15000"),
    )


def _run(
    *, price: Decimal, cap_rate: Decimal, dscr: Decimal, status: OpportunityStatus, cap_passed: bool, dscr_passed: bool
) -> UnderwritingResult:
    working_values = WorkingValues(
        asking_price=price,
        gross_scheduled_income=Decimal("180000"),
        stated_noi=Decimal("126000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
    )
    noi = cap_rate * price
    normalized = _normalized(cap_rate=cap_rate, noi=noi)
    financing = _financing(price=price, dscr=dscr)
    gates = _gates(cap_passed=cap_passed, dscr_passed=dscr_passed)
    viability = ViabilityFrontier(
        current_price=price,
        max_viable_price=Decimal("1300000") if status != OpportunityStatus.REVIEW else None,
        distance_pct=Decimal("0") if status == OpportunityStatus.REVIEW else Decimal("0.16"),
        binding_constraints=[] if status == OpportunityStatus.REVIEW else ["min_normalized_cap_rate", "min_base_dscr"],
        structural_failures=[],
    )
    return UnderwritingResult(
        opportunity_id="opp_power_inn",
        policy_version="v1-testpolicy",
        inputs=working_values,
        normalized=normalized,
        financing=financing,
        stress=[],
        gates=gates,
        status=status,
        viability=viability,
        comparison=[ComparisonRow(metric="NOI", broker="$126,000", dealsieve=f"${noi:,.0f}")],
        failure_summary=(
            "Fails on valuation: normalized cap 6.42% < 8.00%, DSCR 1.05x < 1.35x"
            if status != OpportunityStatus.REVIEW
            else "Passes all gates at $1,250,000"
        ),
    )


def _opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp_power_inn",
        deal_number=101,
        property_id="prop_power_inn",
        display_name="8-unit small-bay industrial, Power Inn Rd, Sacramento",
        status=OpportunityStatus.REVIEW,
        previous_status=OpportunityStatus.WATCH,
    )


def _skeptic() -> SkepticReport:
    return SkepticReport(
        opportunity_id="opp_power_inn",
        run_id="run_after",
        verdict="proceed_with_questions",
        summary="Attractive on the numbers; several physical/legal items are unverified.",
        concerns=[
            SkepticConcern(
                topic="roof age",
                severity="medium",
                why_it_matters="Unknown capex exposure if replacement is imminent.",
                evidence_status="missing",
            ),
            SkepticConcern(
                topic="Phase I environmental",
                severity="high",
                why_it_matters="Industrial site; undisclosed contamination is a deal-breaker.",
                evidence_status="missing",
            ),
            SkepticConcern(
                topic="CAM reconciliation",
                severity="low",
                why_it_matters="True-up risk if CAM has not been reconciled recently.",
                evidence_status="weak",
            ),
            SkepticConcern(
                topic="seller-related tenants",
                severity="low",
                why_it_matters="Related-party leases can overstate market rent.",
                evidence_status="unverified",
            ),
        ],
    )


def test_format_threshold_alert_matches_contracts_example() -> None:
    opportunity = _opportunity()
    previous_run = _run(
        price=Decimal("1550000"),
        cap_rate=Decimal("0.0642"),
        dscr=Decimal("1.05"),
        status=OpportunityStatus.WATCH,
        cap_passed=False,
        dscr_passed=False,
    )
    new_run = _run(
        price=Decimal("1250000"),
        cap_rate=Decimal("0.0827"),
        dscr=Decimal("1.43"),
        status=OpportunityStatus.REVIEW,
        cap_passed=True,
        dscr_passed=True,
    )
    skeptic = _skeptic()

    notification = format_threshold_alert(opportunity, previous_run, new_run, skeptic)

    assert notification.title == "DEAL #101 JUST BECAME INVESTABLE"
    assert notification.body == EXPECTED_BODY
    assert notification.kind == "threshold_crossed"
    assert notification.opportunity_id == "opp_power_inn"
    assert [a.action for a in notification.actions] == ["review", "draft_questions", "ignore"]
    assert [a.label for a in notification.actions] == ["Review", "Draft broker questions", "Ignore"]

    # The fourth concern ("seller-related tenants") is "unverified", not missing/weak -- must not appear.
    assert "seller-related tenants" not in notification.body

    # Full reconstruction (title + body + action row) matches the exact block from docs/CONTRACTS.md.
    button_row = " ".join(f"[{a.label}]" for a in notification.actions)
    full_text = f"{notification.title}\n{notification.body}\n\n{button_row}"
    expected_full = (
        "DEAL #101 JUST BECAME INVESTABLE\n"
        + EXPECTED_BODY
        + "\n\n[Review] [Draft broker questions] [Ignore]"
    )
    assert full_text == expected_full


def test_format_threshold_alert_omits_previously_failed_line_with_prior_structural_failure() -> None:
    opportunity = _opportunity()
    previous_run = _run(
        price=Decimal("1550000"),
        cap_rate=Decimal("0.0642"),
        dscr=Decimal("1.05"),
        status=OpportunityStatus.WATCH,
        cap_passed=False,
        dscr_passed=False,
    )
    previous_run = previous_run.model_copy(
        update={"viability": previous_run.viability.model_copy(update={"structural_failures": ["largest_tenant_pct_max"]})}
    )
    new_run = _run(
        price=Decimal("1250000"),
        cap_rate=Decimal("0.0827"),
        dscr=Decimal("1.43"),
        status=OpportunityStatus.REVIEW,
        cap_passed=True,
        dscr_passed=True,
    )

    notification = format_threshold_alert(opportunity, previous_run, new_run, None)

    assert "Previously failed solely on valuation." not in notification.body
    assert "Still unresolved" not in notification.body


def test_format_threshold_alert_handles_no_previous_run() -> None:
    opportunity = _opportunity()
    new_run = _run(
        price=Decimal("1250000"),
        cap_rate=Decimal("0.0827"),
        dscr=Decimal("1.43"),
        status=OpportunityStatus.REVIEW,
        cap_passed=True,
        dscr_passed=True,
    )

    notification = format_threshold_alert(opportunity, None, new_run, None)

    assert "->" not in notification.body.splitlines()[2]  # Price row has no before -> after arrow
    assert "$1,250,000" in notification.body
    assert "Previously failed solely on valuation." not in notification.body
    assert "Still unresolved" not in notification.body


def test_format_threshold_alert_caps_unresolved_list_at_four() -> None:
    opportunity = _opportunity()
    previous_run = _run(
        price=Decimal("1550000"),
        cap_rate=Decimal("0.0642"),
        dscr=Decimal("1.05"),
        status=OpportunityStatus.WATCH,
        cap_passed=False,
        dscr_passed=False,
    )
    new_run = _run(
        price=Decimal("1250000"),
        cap_rate=Decimal("0.0827"),
        dscr=Decimal("1.43"),
        status=OpportunityStatus.REVIEW,
        cap_passed=True,
        dscr_passed=True,
    )
    skeptic = SkepticReport(
        opportunity_id="opp_power_inn",
        run_id="run_after",
        verdict="proceed_with_questions",
        summary="Five open items, all unresolved.",
        concerns=[
            SkepticConcern(
                topic=topic,
                severity="medium",
                why_it_matters="Material to the decision.",
                evidence_status="missing",
            )
            for topic in ["roof age", "Phase I environmental", "CAM reconciliation", "lease rollover", "financing"]
        ],
    )

    notification = format_threshold_alert(opportunity, previous_run, new_run, skeptic)

    unresolved_lines = notification.body.splitlines()[notification.body.splitlines().index("Still unresolved:") + 1 :]
    assert unresolved_lines == [
        "- roof age",
        "- Phase I environmental",
        "- CAM reconciliation",
        "- lease rollover",
        "- +1 more in the dashboard",
    ]


def test_console_notifier_render_box_includes_actions() -> None:
    opportunity = _opportunity()
    previous_run = _run(
        price=Decimal("1550000"),
        cap_rate=Decimal("0.0642"),
        dscr=Decimal("1.05"),
        status=OpportunityStatus.WATCH,
        cap_passed=False,
        dscr_passed=False,
    )
    new_run = _run(
        price=Decimal("1250000"),
        cap_rate=Decimal("0.0827"),
        dscr=Decimal("1.43"),
        status=OpportunityStatus.REVIEW,
        cap_passed=True,
        dscr_passed=True,
    )
    notification = format_threshold_alert(opportunity, previous_run, new_run, _skeptic())

    box = render_box(notification)
    lines = box.splitlines()
    assert lines[0] == lines[-1]  # top and bottom border match
    assert all(line.startswith("+") or line.startswith("|") for line in lines)
    assert any("[Review] [Draft broker questions] [Ignore]" in line for line in lines)
    assert any("DEAL #101 JUST BECAME INVESTABLE" in line for line in lines)
