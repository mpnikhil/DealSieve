from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.schemas import ExpenseClaims, OpportunityStatus, WorkingValues
from dealsieve.underwriting import run_underwriting
from dealsieve.underwriting.financing import compute_financing
from dealsieve.underwriting.gates import evaluate_gates
from dealsieve.underwriting.normalize import normalize_economics

EXPECTED_ORDER = [
    "tenant_count_min",
    "largest_tenant_pct_max",
    "absolute_max_price",
    "max_ltv",
    "min_normalized_cap_rate",
    "min_base_dscr",
]


def _gates(values: WorkingValues, policy):
    normalized = normalize_economics(values, policy, values.asking_price)
    financing = compute_financing(normalized.noi, values.asking_price, policy)
    return evaluate_gates(values, normalized, financing, policy)


def _gate(values: WorkingValues, policy, key: str):
    return next(gate for gate in _gates(values, policy) if gate.gate == key)


def _frictionless_policy(policy):
    normalization = policy.normalization.model_copy(
        update={
            "management_fee_pct": Decimal("0"),
            "normalized_vacancy_pct": Decimal("0"),
            "require_property_tax_reset": False,
            "require_capex_reserve": False,
            "min_insurance_per_sqft": Decimal("0"),
            "min_repairs_pct_of_egi": Decimal("0"),
        }
    )
    return policy.model_copy(update={"normalization": normalization})


def _values_with_no_expenses(demo_values, *, price: Decimal, noi: Decimal):
    return demo_values.model_copy(
        update={
            "asking_price": price,
            "gross_scheduled_income": noi,
            "other_income": Decimal("0"),
            "stated_vacancy_pct": Decimal("0"),
            "stated_expenses": ExpenseClaims(),
            "stated_noi": noi,
            "building_sqft": None,
        }
    )


def test_gate_order_is_stable(demo_values, policy):
    assert [gate.gate for gate in _gates(demo_values, policy)] == EXPECTED_ORDER


@pytest.mark.parametrize(
    ("tenant_count", "passed"),
    [(5, True), (4, False)],
)
def test_tenant_count_boundary(demo_values, policy, tenant_count, passed):
    values = demo_values.model_copy(update={"tenant_count": tenant_count})
    assert _gate(values, policy, "tenant_count_min").passed is passed


def test_unknown_tenant_count_passes_as_not_verifiable(demo_values, policy):
    values = demo_values.model_copy(update={"tenant_count": None})
    gate = _gate(values, policy, "tenant_count_min")
    assert gate.passed
    assert gate.actual is None
    assert gate.description == "not verifiable: tenant_count unknown"


@pytest.mark.parametrize(
    ("largest_tenant_pct", "passed"),
    [(Decimal("0.25"), True), (Decimal("0.250001"), False)],
)
def test_largest_tenant_boundary(demo_values, policy, largest_tenant_pct, passed):
    values = demo_values.model_copy(update={"largest_tenant_pct": largest_tenant_pct})
    assert _gate(values, policy, "largest_tenant_pct_max").passed is passed


def test_unknown_largest_tenant_passes_as_not_verifiable(demo_values, policy):
    values = demo_values.model_copy(update={"largest_tenant_pct": None})
    gate = _gate(values, policy, "largest_tenant_pct_max")
    assert gate.passed
    assert gate.actual is None
    assert gate.description == "not verifiable: largest_tenant_pct unknown"


@pytest.mark.parametrize(
    ("actual", "passed"),
    [(Decimal("2000000"), True), (Decimal("2000000.01"), False)],
)
def test_absolute_max_price_boundary(demo_values, policy, actual, passed):
    normalized = normalize_economics(demo_values, policy, actual)
    financing = compute_financing(normalized.noi, actual, policy)
    gate = next(
        item
        for item in evaluate_gates(demo_values, normalized, financing, policy)
        if item.gate == "absolute_max_price"
    )
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("actual", "passed"),
    [(Decimal("0.750000"), True), (Decimal("0.750001"), False)],
)
def test_ltv_boundary(demo_values, policy, actual, passed):
    normalized = normalize_economics(demo_values, policy, demo_values.asking_price)
    financing = compute_financing(normalized.noi, demo_values.asking_price, policy).model_copy(
        update={"ltv": actual}
    )
    gate = next(
        item
        for item in evaluate_gates(demo_values, normalized, financing, policy)
        if item.gate == "max_ltv"
    )
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("actual", "passed"),
    [(Decimal("0.080000"), True), (Decimal("0.079999"), False)],
)
def test_cap_rate_boundary(demo_values, policy, actual, passed):
    normalized = normalize_economics(
        demo_values, policy, demo_values.asking_price
    ).model_copy(update={"normalized_cap_rate": actual})
    financing = compute_financing(normalized.noi, demo_values.asking_price, policy)
    gate = next(
        item
        for item in evaluate_gates(demo_values, normalized, financing, policy)
        if item.gate == "min_normalized_cap_rate"
    )
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("actual", "passed"),
    [(Decimal("1.350000"), True), (Decimal("1.349999"), False)],
)
def test_dscr_boundary(demo_values, policy, actual, passed):
    normalized = normalize_economics(demo_values, policy, demo_values.asking_price)
    financing = compute_financing(normalized.noi, demo_values.asking_price, policy).model_copy(
        update={"dscr": actual}
    )
    gate = next(
        item
        for item in evaluate_gates(demo_values, normalized, financing, policy)
        if item.gate == "min_base_dscr"
    )
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("raw_cap", "passed"),
    [(Decimal("0.08"), True), (Decimal("0.0799996"), False)],
)
def test_engine_evaluates_cap_gate_before_display_rounding(
    demo_values,
    policy,
    raw_cap,
    passed,
):
    policy = _frictionless_policy(policy)
    price = Decimal("1000000")
    values = _values_with_no_expenses(demo_values, price=price, noi=price * raw_cap)

    result = run_underwriting(values, policy, opportunity_id="raw-cap")
    gate = next(item for item in result.gates if item.gate == "min_normalized_cap_rate")

    assert result.normalized.normalized_cap_rate == Decimal("0.080000")
    assert gate.passed is passed
    if passed:
        assert result.status == OpportunityStatus.REVIEW
    else:
        assert result.status != OpportunityStatus.REVIEW
        assert result.viability.max_viable_price < price


@pytest.mark.parametrize(
    ("raw_dscr", "passed"),
    [(Decimal("1.35"), True), (Decimal("1.3499996"), False)],
)
def test_engine_evaluates_dscr_gate_before_display_rounding(
    demo_values,
    policy,
    raw_dscr,
    passed,
):
    policy = _frictionless_policy(policy)
    financing = policy.financing.model_copy(
        update={
            "assumed_interest_rate": Decimal("0"),
            "amortization_years": 25,
            "max_ltv": Decimal("1"),
            "closing_cost_pct": Decimal("0"),
        }
    )
    capital = policy.capital.model_copy(
        update={"acquisition_equity": Decimal("0"), "reserve_target": Decimal("0")}
    )
    underwriting = policy.underwriting.model_copy(
        update={"min_normalized_cap_rate": Decimal("0")}
    )
    policy = policy.model_copy(
        update={"financing": financing, "capital": capital, "underwriting": underwriting}
    )
    values = _values_with_no_expenses(
        demo_values,
        price=Decimal("1000000"),
        noi=Decimal("40000") * raw_dscr,
    )

    result = run_underwriting(values, policy, opportunity_id="raw-dscr")
    gate = next(item for item in result.gates if item.gate == "min_base_dscr")

    assert result.financing.dscr == Decimal("1.350000")
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("price", "passed"),
    [(Decimal("2000000"), True), (Decimal("2000000.004"), False)],
)
def test_engine_evaluates_price_gate_before_display_rounding(
    demo_values,
    policy,
    price,
    passed,
):
    values = demo_values.model_copy(update={"asking_price": price})
    result = run_underwriting(values, policy, opportunity_id="raw-price")
    gate = next(item for item in result.gates if item.gate == "absolute_max_price")

    assert result.financing.purchase_price == Decimal("2000000.00")
    assert gate.passed is passed


@pytest.mark.parametrize(
    ("equity", "passed"),
    [(Decimal("250000"), True), (Decimal("249999.6"), False)],
)
def test_engine_evaluates_ltv_gate_before_display_rounding(
    demo_values,
    policy,
    equity,
    passed,
):
    policy = _frictionless_policy(policy)
    financing = policy.financing.model_copy(update={"closing_cost_pct": Decimal("0")})
    capital = policy.capital.model_copy(
        update={"acquisition_equity": equity, "reserve_target": Decimal("0")}
    )
    underwriting = policy.underwriting.model_copy(
        update={"min_normalized_cap_rate": Decimal("0"), "min_base_dscr": Decimal("0")}
    )
    policy = policy.model_copy(
        update={"financing": financing, "capital": capital, "underwriting": underwriting}
    )
    values = _values_with_no_expenses(
        demo_values,
        price=Decimal("1000000"),
        noi=Decimal("500000"),
    )

    result = run_underwriting(values, policy, opportunity_id="raw-ltv")
    gate = next(item for item in result.gates if item.gate == "max_ltv")

    assert result.financing.ltv == Decimal("0.750000")
    assert gate.passed is passed
