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

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import UnderwritingResult, WorkingValues


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
    raise NotImplementedError("W1: dealsieve.underwriting.engine.run_underwriting")
