"""Deterministic acquisition financing calculations."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import FinancingResult

MONEY = Decimal("0.01")
RATE = Decimal("0.000001")
ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    return value.quantize(RATE, rounding=ROUND_HALF_UP)


def _monthly_payment_raw(principal: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    payments = years * 12
    if payments <= 0:
        raise ValueError("years must be positive")
    if principal <= ZERO:
        return ZERO
    if annual_rate == ZERO:
        return principal / Decimal(payments)
    monthly_rate = annual_rate / Decimal(12)
    factor = (Decimal(1) + monthly_rate) ** payments
    return principal * monthly_rate * factor / (factor - Decimal(1))


def monthly_payment(principal: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    """Return the level monthly payment, rounded to cents at the public boundary."""
    return _money(_monthly_payment_raw(principal, annual_rate, years))


def year1_principal_paydown(principal: Decimal, annual_rate: Decimal, years: int) -> Decimal:
    """Return principal repaid by the first twelve scheduled payments."""
    if principal <= ZERO:
        return ZERO
    payment = _monthly_payment_raw(principal, annual_rate, years)
    monthly_rate = annual_rate / Decimal(12)
    balance = principal
    for _ in range(min(12, years * 12)):
        interest = balance * monthly_rate
        principal_component = payment - interest
        balance -= principal_component
    return _money(principal - balance)


def compute_financing(
    noi: Decimal,
    price: Decimal,
    policy: InvestmentPolicy,
    immediate_capex: Decimal = ZERO,
) -> FinancingResult:
    """Return policy financing at a purchase price, using fixed deployable equity."""
    financing, _, _ = _compute_financing(noi, price, policy, immediate_capex)
    return financing


def _compute_financing(
    noi: Decimal,
    price: Decimal,
    policy: InvestmentPolicy,
    immediate_capex: Decimal = ZERO,
) -> tuple[FinancingResult, Decimal, Decimal]:
    """Return the emitted financing and unrounded LTV and DSCR gate values."""
    closing_costs = price * policy.financing.closing_cost_pct
    all_in_basis = price + immediate_capex
    total_cost = all_in_basis + closing_costs
    deployable_equity = policy.capital.acquisition_equity - policy.capital.reserve_target
    loan_amount = max(ZERO, total_cost - deployable_equity)
    equity_deployed = total_cost - loan_amount
    ltv = loan_amount / price if price > ZERO else ZERO
    payment = _monthly_payment_raw(
        loan_amount,
        policy.financing.assumed_interest_rate,
        policy.financing.amortization_years,
    )
    annual_debt_service = payment * Decimal(12)
    if loan_amount > ZERO:
        dscr = noi / annual_debt_service
    elif noi > ZERO:
        dscr = Decimal("999")
    else:
        dscr = ZERO
    cash_flow_after_debt = noi - annual_debt_service
    cash_on_cash = cash_flow_after_debt / equity_deployed if equity_deployed > ZERO else Decimal("999")
    principal_paydown = year1_principal_paydown(
        loan_amount,
        policy.financing.assumed_interest_rate,
        policy.financing.amortization_years,
    )

    financing = FinancingResult(
        purchase_price=_money(price),
        closing_costs=_money(closing_costs),
        immediate_capex=_money(immediate_capex),
        all_in_basis=_money(all_in_basis),
        total_acquisition_cost=_money(total_cost),
        equity_deployed=_money(equity_deployed),
        loan_amount=_money(loan_amount),
        ltv=_rate(ltv),
        interest_rate=_rate(policy.financing.assumed_interest_rate),
        amortization_years=policy.financing.amortization_years,
        monthly_debt_service=_money(payment),
        annual_debt_service=_money(annual_debt_service),
        dscr=_rate(dscr),
        cash_flow_after_debt=_money(cash_flow_after_debt),
        cash_on_cash=_rate(cash_on_cash),
        year1_principal_paydown=_money(principal_paydown),
    )
    return financing, ltv, dscr
