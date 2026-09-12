"""Hard policy-gate evaluation."""

from __future__ import annotations

from decimal import Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    ConstraintKind,
    FinancingResult,
    GateResult,
    NormalizedEconomics,
    WorkingValues,
)


def evaluate_gates(
    values: WorkingValues,
    normalized: NormalizedEconomics,
    financing: FinancingResult,
    policy: InvestmentPolicy,
    *,
    raw_price: Decimal | None = None,
    raw_ltv: Decimal | None = None,
    raw_normalized_cap_rate: Decimal | None = None,
    raw_dscr: Decimal | None = None,
) -> list[GateResult]:
    """Evaluate all hard gates in their stable contract order.

    Callers composing calculations pass the unrounded values. The result objects
    remain the source of the presentation-ready ``actual`` fields.
    """
    gate_price = financing.purchase_price if raw_price is None else raw_price
    gate_ltv = financing.ltv if raw_ltv is None else raw_ltv
    gate_cap = (
        normalized.normalized_cap_rate
        if raw_normalized_cap_rate is None
        else raw_normalized_cap_rate
    )
    gate_dscr = financing.dscr if raw_dscr is None else raw_dscr
    tenant_count_unknown = values.tenant_count is None
    largest_tenant_unknown = values.largest_tenant_pct is None
    tenant_count_passed = tenant_count_unknown or (
        values.tenant_count >= policy.property.tenant_count_min
    )
    largest_tenant_passed = largest_tenant_unknown or (
        values.largest_tenant_pct <= policy.property.largest_tenant_pct_max
    )

    return [
        GateResult(
            gate="tenant_count_min",
            kind=ConstraintKind.STRUCTURAL,
            description=(
                "not verifiable: tenant_count unknown"
                if tenant_count_unknown
                else "tenant count meets minimum diversification"
            ),
            comparator=">=",
            threshold=policy.property.tenant_count_min,
            actual=values.tenant_count,
            passed=tenant_count_passed,
            price_dependent=False,
        ),
        GateResult(
            gate="largest_tenant_pct_max",
            kind=ConstraintKind.STRUCTURAL,
            description=(
                "not verifiable: largest_tenant_pct unknown"
                if largest_tenant_unknown
                else "largest tenant concentration is within maximum"
            ),
            comparator="<=",
            threshold=policy.property.largest_tenant_pct_max,
            actual=values.largest_tenant_pct,
            passed=largest_tenant_passed,
            price_dependent=False,
        ),
        GateResult(
            gate="absolute_max_price",
            kind=ConstraintKind.ECONOMIC,
            description="purchase price is within absolute maximum",
            comparator="<=",
            threshold=policy.purchase.absolute_max,
            actual=financing.purchase_price,
            passed=gate_price <= policy.purchase.absolute_max,
            price_dependent=True,
        ),
        GateResult(
            gate="max_ltv",
            kind=ConstraintKind.ECONOMIC,
            description="loan-to-value is within maximum",
            comparator="<=",
            threshold=policy.financing.max_ltv,
            actual=financing.ltv,
            passed=gate_ltv <= policy.financing.max_ltv,
            price_dependent=True,
        ),
        GateResult(
            gate="min_normalized_cap_rate",
            kind=ConstraintKind.ECONOMIC,
            description="normalized cap rate meets minimum",
            comparator=">=",
            threshold=policy.underwriting.min_normalized_cap_rate,
            actual=normalized.normalized_cap_rate,
            passed=gate_cap >= policy.underwriting.min_normalized_cap_rate,
            price_dependent=True,
        ),
        GateResult(
            gate="min_base_dscr",
            kind=ConstraintKind.ECONOMIC,
            description="base DSCR meets minimum",
            comparator=">=",
            threshold=policy.underwriting.min_base_dscr,
            actual=financing.dscr,
            passed=gate_dscr >= policy.underwriting.min_base_dscr,
            price_dependent=True,
        ),
    ]
