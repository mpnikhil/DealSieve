"""Normalize broker economics under the immutable investment policy."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import ExpenseLine, NormalizedEconomics, WorkingValues

MONEY = Decimal("0.01")
RATE = Decimal("0.000001")
ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _rate(value: Decimal) -> Decimal:
    return value.quantize(RATE, rounding=ROUND_HALF_UP)


def _percent_label(value: Decimal) -> str:
    return format((value * Decimal(100)).normalize(), "f")


def normalize_economics(
    values: WorkingValues,
    policy: InvestmentPolicy,
    price: Decimal,
) -> NormalizedEconomics:
    """Return policy-normalized property economics at ``price``."""
    gross_potential_rent = values.gross_scheduled_income
    vacancy_pct = max(
        policy.normalization.normalized_vacancy_pct,
        values.stated_vacancy_pct,
    )
    vacancy_loss = gross_potential_rent * vacancy_pct
    effective_gross_income = gross_potential_rent - vacancy_loss + values.other_income
    stated = values.stated_expenses

    if policy.normalization.require_property_tax_reset:
        property_tax = price * policy.normalization.property_tax_rate_pct
        property_tax_basis = (
            f"{_percent_label(policy.normalization.property_tax_rate_pct)}% of price "
            "(reassessed at sale)"
        )
    else:
        property_tax = stated.property_tax or ZERO
        property_tax_basis = "as stated"

    stated_insurance = stated.insurance or ZERO
    if values.building_sqft is None:
        insurance = stated_insurance
        insurance_basis = "as stated"
    else:
        insurance_floor = Decimal(values.building_sqft) * policy.normalization.min_insurance_per_sqft
        insurance = max(stated_insurance, insurance_floor)
        insurance_basis = f"max(as stated, ${policy.normalization.min_insurance_per_sqft}/sf)"

    repairs_floor = effective_gross_income * policy.normalization.min_repairs_pct_of_egi
    repairs_maintenance = max(stated.repairs_maintenance or ZERO, repairs_floor)
    management_floor = effective_gross_income * policy.normalization.management_fee_pct
    management = max(stated.management or ZERO, management_floor)
    utilities = stated.utilities or ZERO
    cam_other = stated.cam_other or ZERO

    if policy.normalization.require_capex_reserve and values.building_sqft is not None:
        capex_reserve = Decimal(values.building_sqft) * policy.normalization.capex_reserve_per_sqft
        capex_basis = f"${policy.normalization.capex_reserve_per_sqft}/sf reserve"
    else:
        capex_reserve = ZERO
        capex_basis = "not required or square footage unknown"

    raw_lines = [
        ("Property tax", stated.property_tax, property_tax, property_tax_basis),
        ("Insurance", stated.insurance, insurance, insurance_basis),
        (
            "Repairs & maintenance",
            stated.repairs_maintenance,
            repairs_maintenance,
            "max(as stated, "
            f"{_percent_label(policy.normalization.min_repairs_pct_of_egi)}% of EGI)",
        ),
        ("Utilities", stated.utilities, utilities, "as stated"),
        (
            "Management",
            stated.management,
            management,
            f"{_percent_label(policy.normalization.management_fee_pct)}% of EGI",
        ),
        ("CAM / other", stated.cam_other, cam_other, "as stated"),
        ("CapEx reserve", None, capex_reserve, capex_basis),
    ]
    total_expenses = sum((line[2] for line in raw_lines), ZERO)
    noi = effective_gross_income - total_expenses

    broker_cap_rate = None
    if values.stated_noi is not None:
        broker_cap_rate = _rate(values.stated_noi / price)

    price_per_sqft = None
    noi_per_sqft = None
    if values.building_sqft:
        sqft = Decimal(values.building_sqft)
        price_per_sqft = _money(price / sqft)
        noi_per_sqft = _money(noi / sqft)

    return NormalizedEconomics(
        gross_potential_rent=_money(gross_potential_rent),
        other_income=_money(values.other_income),
        vacancy_loss=_money(vacancy_loss),
        effective_gross_income=_money(effective_gross_income),
        expenses=[
            ExpenseLine(
                name=name,
                broker=_money(broker) if broker is not None else None,
                normalized=_money(normalized),
                basis=basis,
            )
            for name, broker, normalized, basis in raw_lines
        ],
        total_expenses=_money(total_expenses),
        noi=_money(noi),
        broker_noi=_money(values.stated_noi) if values.stated_noi is not None else None,
        broker_cap_rate=broker_cap_rate,
        normalized_cap_rate=_rate(noi / price),
        price_per_sqft=price_per_sqft,
        noi_per_sqft=noi_per_sqft,
    )
