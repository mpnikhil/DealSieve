"""Orchestrates one underwriting run: normalize -> finance -> stress -> gates -> viability -> classify.

W1 implements this module and its siblings:
  normalize.py   normalize_economics(values, policy, purchase_price) -> NormalizedEconomics
  financing.py   compute_financing(noi, purchase_price, policy) -> FinancingResult
                 monthly_payment(principal, annual_rate, years) -> Decimal
                 year1_principal_paydown(principal, annual_rate, years) -> Decimal
  stress.py      run_stress(values, policy, purchase_price) -> list[StressResult]
  gates.py       evaluate_gates(values, normalized, financing, policy) -> list[GateResult]
  viability.py   solve_max_viable_price(values, policy) -> ViabilityFrontier
  classify.py    classify(gates, viability, purchase_price, policy) -> OpportunityStatus
  compare.py     broker_vs_dealsieve(values, normalized, financing) -> list[ComparisonRow]
"""

from __future__ import annotations

from decimal import Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import GateResult, OpportunityStatus, UnderwritingResult, WorkingValues
from dealsieve.underwriting.classify import classify
from dealsieve.underwriting.compare import broker_vs_dealsieve
from dealsieve.underwriting.financing import _compute_financing
from dealsieve.underwriting.gates import evaluate_gates
from dealsieve.underwriting.normalize import _normalize_economics
from dealsieve.underwriting.stress import run_stress
from dealsieve.underwriting.viability import solve_max_viable_price


def _money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('1')):,.0f}"


def _percent(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def _failure_summary(
    status: OpportunityStatus,
    gates: list[GateResult],
    price: Decimal,
    immediate_capex: Decimal,
) -> str:
    failed = {gate.gate: gate for gate in gates if not gate.passed}
    if status == OpportunityStatus.DEAD:
        parts: list[str] = []
        largest = failed.get("largest_tenant_pct_max")
        if largest is not None:
            parts.append(
                f"largest tenant {_percent(Decimal(largest.actual))} > "
                f"{_percent(Decimal(largest.threshold))}"
            )
        tenants = failed.get("tenant_count_min")
        if tenants is not None:
            parts.append(f"{tenants.actual} tenants < {tenants.threshold}")
        return "Structural: " + ", ".join(parts)
    if status == OpportunityStatus.REVIEW:
        return f"Passes all gates at {_money(price)}"

    parts = []
    cap = failed.get("min_normalized_cap_rate")
    if cap is not None:
        cap_label = "cap" if immediate_capex > 0 else "normalized cap"
        parts.append(
            f"{cap_label} {_percent(Decimal(cap.actual))} < "
            f"{_percent(Decimal(cap.threshold))}"
        )
    dscr = failed.get("min_base_dscr")
    if dscr is not None:
        parts.append(f"DSCR {Decimal(dscr.actual):.2f}x < {Decimal(dscr.threshold):.2f}x")
    ltv = failed.get("max_ltv")
    if ltv is not None:
        parts.append(
            f"LTV {_percent(Decimal(ltv.actual))} > {_percent(Decimal(ltv.threshold))}"
        )
    absolute = failed.get("absolute_max_price")
    if absolute is not None:
        parts.append(f"price {_money(Decimal(absolute.actual))} > {_money(Decimal(absolute.threshold))}")
    if immediate_capex > 0:
        return f"Fails on valuation after {_money(immediate_capex)} immediate capex: " + ", ".join(parts)
    return "Fails on valuation: " + ", ".join(parts)


class InvalidInputs(ValueError):
    """Raised when authoritative underwriting inputs cannot produce a valid run."""


def run_underwriting(
    values: WorkingValues,
    policy: InvestmentPolicy,
    *,
    opportunity_id: str,
    trigger_event_id: str | None = None,
) -> UnderwritingResult:
    """Run a complete, deterministic underwriting pass at values.asking_price.

    Must be a pure function of (values, policy). Same inputs, same output, every time.
    """
    price = values.asking_price
    if price <= 0:
        raise InvalidInputs("asking price must be greater than zero")

    normalized, raw_noi = _normalize_economics(values, policy, price)
    financing, raw_ltv, raw_dscr = _compute_financing(
        raw_noi,
        price,
        policy,
        values.immediate_capex,
    )
    raw_cap = raw_noi / (price + values.immediate_capex)
    gates = evaluate_gates(
        values,
        normalized,
        financing,
        policy,
        raw_price=price,
        raw_ltv=raw_ltv,
        raw_normalized_cap_rate=raw_cap,
        raw_dscr=raw_dscr,
    )
    viability = solve_max_viable_price(values, policy)
    status = classify(gates, viability, price, policy)
    return UnderwritingResult(
        opportunity_id=opportunity_id,
        policy_version=policy.policy_version,
        trigger_event_id=trigger_event_id,
        inputs=values,
        normalized=normalized,
        financing=financing,
        stress=run_stress(values, policy, price),
        gates=gates,
        status=status,
        viability=viability,
        comparison=broker_vs_dealsieve(values, normalized, financing),
        failure_summary=_failure_summary(status, gates, price, values.immediate_capex),
    )
