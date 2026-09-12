from __future__ import annotations

from decimal import Decimal

from dealsieve.underwriting.stress import run_stress


def test_stress_scenarios_recompute_noi_and_dscr(demo_values, policy):
    results = run_stress(demo_values, policy, Decimal("1250000"))
    assert [result.scenario for result in results] == [
        "vacancy_20pct",
        "rent_haircut_10pct",
        "combined",
    ]
    assert [result.noi for result in results] == [
        Decimal("77675.00"),
        Decimal("87080.00"),
        Decimal("63995.00"),
    ]
    assert results[2].dscr < results[0].dscr < results[1].dscr
    assert all(
        result.covers_debt == (result.dscr >= policy.stress.min_stress_dscr)
        for result in results
    )
