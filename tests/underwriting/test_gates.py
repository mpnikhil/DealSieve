from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.schemas import WorkingValues
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
