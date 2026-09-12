"""Policy stress scenarios."""

from __future__ import annotations

from decimal import Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import StressResult, WorkingValues
from dealsieve.underwriting.financing import _compute_financing
from dealsieve.underwriting.normalize import _normalize_economics


def run_stress(
    values: WorkingValues,
    policy: InvestmentPolicy,
    price: Decimal,
) -> list[StressResult]:
    """Run the three documented downside cases at ``price``."""
    haircut_gpr = values.gross_scheduled_income * (Decimal(1) - policy.stress.rent_haircut_pct)
    scenarios = [
        (
            "vacancy_20pct",
            values.model_copy(update={"stated_vacancy_pct": policy.stress.vacancy_pct}),
        ),
        (
            "rent_haircut_10pct",
            values.model_copy(update={"gross_scheduled_income": haircut_gpr}),
        ),
        (
            "combined",
            values.model_copy(
                update={
                    "gross_scheduled_income": haircut_gpr,
                    "stated_vacancy_pct": policy.stress.vacancy_pct,
                }
            ),
        ),
    ]
    results: list[StressResult] = []
    for name, stressed_values in scenarios:
        normalized, raw_noi = _normalize_economics(stressed_values, policy, price)
        financing, _, raw_dscr = _compute_financing(
            raw_noi,
            price,
            policy,
            values.immediate_capex,
        )
        results.append(
            StressResult(
                scenario=name,
                noi=normalized.noi,
                dscr=financing.dscr,
                cash_flow_after_debt=financing.cash_flow_after_debt,
                covers_debt=raw_dscr >= policy.stress.min_stress_dscr,
            )
        )
    return results
