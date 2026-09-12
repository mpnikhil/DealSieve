from __future__ import annotations

from decimal import Decimal

from dealsieve.schemas import ConstraintKind
from dealsieve.underwriting.financing import compute_financing
from dealsieve.underwriting.gates import evaluate_gates
from dealsieve.underwriting.normalize import normalize_economics
from dealsieve.underwriting.viability import solve_max_viable_price


def _economic_gates(values, policy, price):
    normalized = normalize_economics(values, policy, price)
    financing = compute_financing(normalized.noi, price, policy)
    return [
        gate
        for gate in evaluate_gates(values, normalized, financing, policy)
        if gate.kind == ConstraintKind.ECONOMIC
    ]


def test_solver_finds_passing_frontier_and_failing_probe(demo_values, policy):
    frontier = solve_max_viable_price(demo_values, policy)
    assert frontier.max_viable_price is not None
    assert Decimal("1270000") <= frontier.max_viable_price <= Decimal("1330000")
    assert all(
        gate.passed
        for gate in _economic_gates(demo_values, policy, frontier.max_viable_price)
    )
    probe = frontier.max_viable_price + Decimal("1000")
    assert any(not gate.passed for gate in _economic_gates(demo_values, policy, probe))
    assert frontier.binding_constraints == ["min_normalized_cap_rate"]
    assert frontier.paths[0].variable == "purchase_price"
    assert frontier.paths[0].required_value == frontier.max_viable_price


def test_structural_failure_has_no_price_solution(demo_values, policy):
    values = demo_values.model_copy(
        update={"tenant_count": 2, "largest_tenant_pct": Decimal("0.78")}
    )
    frontier = solve_max_viable_price(values, policy)
    assert frontier.max_viable_price is None
    assert frontier.distance_pct is None
    assert frontier.structural_failures == [
        "tenant_count_min",
        "largest_tenant_pct_max",
    ]


def test_already_passing_price_reports_zero_distance(demo_values, policy):
    values = demo_values.model_copy(update={"asking_price": Decimal("1250000")})
    frontier = solve_max_viable_price(values, policy)
    assert frontier.distance_pct == Decimal("0.000000")
