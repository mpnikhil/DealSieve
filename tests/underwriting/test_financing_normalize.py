from __future__ import annotations

from decimal import Decimal

from dealsieve.underwriting.financing import (
    compute_financing,
    monthly_payment,
    year1_principal_paydown,
)
from dealsieve.underwriting.normalize import normalize_economics


def test_known_amortizing_payment():
    payment = monthly_payment(Decimal("1000000"), Decimal("0.07"), 25)
    assert payment == Decimal("7067.79")


def test_zero_rate_payment():
    assert monthly_payment(Decimal("120000"), Decimal(0), 10) == Decimal("1000")


def test_year_one_principal_paydown():
    paydown = year1_principal_paydown(Decimal("1000000"), Decimal("0.07"), 25)
    assert paydown == Decimal("15298.13")


def test_property_tax_resets_at_sale_and_preserves_broker_value(demo_values, policy):
    normalized = normalize_economics(demo_values, policy, Decimal("1550000"))
    tax = next(line for line in normalized.expenses if line.name == "Property tax")
    assert tax.broker == Decimal("15500.00")
    assert tax.normalized == Decimal("19375.00")
    assert tax.basis == "1.25% of price (reassessed at sale)"


def test_financing_uses_fixed_deployable_equity(demo_values, policy):
    normalized = normalize_economics(demo_values, policy, Decimal("1550000"))
    financing = compute_financing(normalized.noi, Decimal("1550000"), policy)
    assert financing.closing_costs == Decimal("31000.00")
    assert financing.equity_deployed == Decimal("425000.00")
    assert financing.loan_amount == Decimal("1156000.00")
    assert financing.ltv == Decimal("0.745806")
    assert financing.monthly_debt_service == Decimal("8170.37")


def test_cap_rate_and_financing_use_all_in_basis(demo_values, policy):
    values = demo_values.model_copy(update={"immediate_capex": Decimal("90000")})
    normalized = normalize_economics(values, policy, Decimal("1250000"))
    financing = compute_financing(
        normalized.noi,
        Decimal("1250000"),
        policy,
        values.immediate_capex,
    )

    assert normalized.normalized_cap_rate == Decimal("0.077108")
    assert financing.immediate_capex == Decimal("90000.00")
    assert financing.all_in_basis == Decimal("1340000.00")
    assert financing.closing_costs == Decimal("25000.00")
    assert financing.total_acquisition_cost == Decimal("1365000.00")
    assert financing.loan_amount == Decimal("940000.00")
    assert financing.ltv == Decimal("0.752000")


def test_zero_capex_keeps_existing_financing_values(demo_values, policy):
    normalized = normalize_economics(demo_values, policy, Decimal("1550000"))
    financing = compute_financing(normalized.noi, Decimal("1550000"), policy)

    assert financing.immediate_capex == Decimal("0.00")
    assert financing.all_in_basis == Decimal("1550000.00")
    assert financing.total_acquisition_cost == Decimal("1581000.00")


def test_zero_loan_dscr_sentinel_requires_positive_noi(policy):
    positive = compute_financing(Decimal("1"), Decimal("100000"), policy)
    zero = compute_financing(Decimal("0"), Decimal("100000"), policy)
    negative = compute_financing(Decimal("-1"), Decimal("100000"), policy)

    assert positive.loan_amount == 0
    assert positive.dscr == Decimal("999.000000")
    assert zero.dscr == 0
    assert negative.dscr == 0
