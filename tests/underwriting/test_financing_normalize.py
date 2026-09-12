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
