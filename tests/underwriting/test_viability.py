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


# --------------------------------------------------------------------- F18: no price fixes it


def _no_income_values(demo_values):
    """Fixed costs alone exceed the income: NOI is negative at every purchase price.

    Nothing structural fails -- eight tenants, none of them dominant -- so this is not DEAD; it is
    a deal no purchase price can rescue, which the frontier used to report as a blank.
    """
    return demo_values.model_copy(
        update={
            "gross_scheduled_income": Decimal("12000"),
            "stated_noi": Decimal("-28000"),
            "tenants": [],
        }
    )


def test_no_price_passes_is_flagged_rather_than_reported_as_a_blank_frontier(demo_values, policy):
    frontier = solve_max_viable_price(_no_income_values(demo_values), policy)

    assert frontier.no_viable_price is True
    assert frontier.max_viable_price is None
    assert frontier.distance_pct is None
    assert frontier.paths == [], "no price change fixes this deal, so no path claims one does"
    assert frontier.structural_failures == [], "nothing structural failed"
    assert frontier.binding_constraints, "the gates that no price can satisfy are still named"


def test_an_ordinary_frontier_does_not_set_the_flag(demo_values, policy):
    assert solve_max_viable_price(demo_values, policy).no_viable_price is False


def test_a_structural_failure_is_not_a_no_viable_price(demo_values, policy):
    values = demo_values.model_copy(
        update={"tenant_count": 2, "largest_tenant_pct": Decimal("0.78")}
    )
    frontier = solve_max_viable_price(values, policy)

    assert frontier.no_viable_price is False, "DEAD is its own outcome, with its own reason"
    assert frontier.structural_failures


def test_the_run_watches_it_and_says_why(demo_values, policy):
    from dealsieve.schemas import OpportunityStatus
    from dealsieve.underwriting import run_underwriting

    run = run_underwriting(_no_income_values(demo_values), policy, opportunity_id="opp_no_price")

    assert run.status == OpportunityStatus.WATCH
    assert run.viability.no_viable_price is True
    assert run.failure_summary.startswith("No purchase price passes the economic gates (NOI ")
    assert "$-" in run.failure_summary, "the NOI that makes it unfixable is in the reason"
