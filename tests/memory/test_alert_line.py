"""format_threshold_alert / format_fell_below_alert: the optional "You previously: ..." line.

Golden-text coverage for the base bodies already lives in tests/api/test_format.py; these tests only
cover the new `memories` parameter and must not need to touch that file's fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from dealsieve.notifications import format_fell_below_alert, format_threshold_alert
from dealsieve.schemas import (
    FinancingResult,
    MemoryHit,
    NormalizedEconomics,
    Opportunity,
    OpportunityStatus,
    UnderwritingResult,
    ViabilityFrontier,
    WorkingValues,
)


def _opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp_1",
        deal_number=101,
        property_id="prop_1",
        display_name="Power Inn",
        status=OpportunityStatus.REVIEW,
    )


def _run(*, status: OpportunityStatus = OpportunityStatus.REVIEW) -> UnderwritingResult:
    working_values = WorkingValues(asking_price=Decimal("1250000"), gross_scheduled_income=Decimal("180000"))
    normalized = NormalizedEconomics(
        gross_potential_rent=Decimal("180000"),
        other_income=Decimal("0"),
        vacancy_loss=Decimal("9000"),
        effective_gross_income=Decimal("171000"),
        expenses=[],
        total_expenses=Decimal("68000"),
        noi=Decimal("103000"),
        normalized_cap_rate=Decimal("0.0824"),
    )
    financing = FinancingResult(
        purchase_price=Decimal("1250000"),
        closing_costs=Decimal("25000"),
        total_acquisition_cost=Decimal("1275000"),
        equity_deployed=Decimal("425000"),
        loan_amount=Decimal("850000"),
        ltv=Decimal("0.68"),
        interest_rate=Decimal("0.07"),
        amortization_years=25,
        monthly_debt_service=Decimal("8000"),
        annual_debt_service=Decimal("96000"),
        dscr=Decimal("1.43"),
        cash_flow_after_debt=Decimal("7000"),
        cash_on_cash=Decimal("0.02"),
        year1_principal_paydown=Decimal("15000"),
    )
    viability = ViabilityFrontier(current_price=Decimal("1250000"))
    return UnderwritingResult(
        opportunity_id="opp_1",
        policy_version="v1",
        inputs=working_values,
        normalized=normalized,
        financing=financing,
        stress=[],
        gates=[],
        status=status,
        viability=viability,
        failure_summary="Passes all gates at $1,250,000",
    )


def _hit(text: str, score: float) -> MemoryHit:
    return MemoryHit(
        memory_event_id="mem_1",
        namespace="investor/human:local",
        kind="decision",
        text=text,
        score=score,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- format_threshold_alert


def test_format_threshold_alert_omits_memory_line_when_none() -> None:
    notification = format_threshold_alert(_opportunity(), None, _run(), None)
    assert "You previously" not in notification.body


def test_format_threshold_alert_omits_memory_line_below_score_threshold() -> None:
    hit = _hit("Approved the roof credit last time.", 0.4)
    notification = format_threshold_alert(_opportunity(), None, _run(), None, memories=[hit])
    assert "You previously" not in notification.body


def test_format_threshold_alert_appends_memory_line_as_final_line() -> None:
    hit = _hit("Approved a $42,000 credit request on a similar deal.", 0.72)
    notification = format_threshold_alert(_opportunity(), None, _run(), None, memories=[hit])
    lines = notification.body.splitlines()
    assert lines[-1] == "You previously: Approved a $42,000 credit request on a similar deal."


def test_format_threshold_alert_uses_only_the_top_hit() -> None:
    top = _hit("Top hit text.", 0.9)
    second = _hit("Second hit text.", 0.85)
    notification = format_threshold_alert(_opportunity(), None, _run(), None, memories=[top, second])
    assert notification.body.splitlines()[-1] == "You previously: Top hit text."


# --------------------------------------------------------------------------- format_fell_below_alert


def test_format_fell_below_alert_omits_memory_line_when_none() -> None:
    notification = format_fell_below_alert(_opportunity(), _run(), _run(status=OpportunityStatus.NEAR), None, None)
    assert "You previously" not in notification.body


def test_format_fell_below_alert_appends_memory_line_when_relevant() -> None:
    hit = _hit("Rejected a $60,000 ask as aggressive on a similar deal.", 0.81)
    notification = format_fell_below_alert(
        _opportunity(), _run(), _run(status=OpportunityStatus.NEAR), None, None, memories=[hit]
    )
    lines = notification.body.splitlines()
    assert lines[-1] == "You previously: Rejected a $60,000 ask as aggressive on a similar deal."
