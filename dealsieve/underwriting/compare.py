"""Human-readable broker-versus-normalized comparison rows."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from dealsieve.schemas import (
    ComparisonRow,
    ExpenseLine,
    FinancingResult,
    NormalizedEconomics,
    WorkingValues,
)


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"${rounded:,.0f}"


def _percent(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value * 100:.2f}%"


def _multiple(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}x"


def _expense(normalized: NormalizedEconomics, name: str) -> ExpenseLine:
    return next(line for line in normalized.expenses if line.name == name)


def broker_vs_dealsieve(
    values: WorkingValues,
    normalized: NormalizedEconomics,
    financing: FinancingResult,
) -> list[ComparisonRow]:
    """Return the eight documented, presentation-ready comparison rows."""
    vacancy_rate = normalized.vacancy_loss / normalized.gross_potential_rent
    management = _expense(normalized, "Management")
    property_tax = _expense(normalized, "Property tax")
    capex = _expense(normalized, "CapEx reserve")
    broker_price_per_sqft = None
    if values.building_sqft:
        broker_price_per_sqft = financing.purchase_price / Decimal(values.building_sqft)

    rows = [
        ComparisonRow(metric="NOI", broker=_money(values.stated_noi), dealsieve=_money(normalized.noi) or ""),
        ComparisonRow(
            metric="Cap rate",
            broker=_percent(normalized.broker_cap_rate),
            dealsieve=_percent(normalized.normalized_cap_rate) or "",
        ),
        ComparisonRow(
            metric="Vacancy",
            broker=_percent(values.stated_vacancy_pct),
            dealsieve=_percent(vacancy_rate) or "",
            note="normalized vacancy floor",
        ),
        ComparisonRow(
            metric="Management",
            broker=_money(management.broker),
            dealsieve=_money(management.normalized) or "",
            note=management.basis,
        ),
        ComparisonRow(
            metric="Property tax",
            broker=_money(property_tax.broker),
            dealsieve=_money(property_tax.normalized) or "",
            note=property_tax.basis,
        ),
        ComparisonRow(
            metric="CapEx reserve",
            broker=_money(capex.broker),
            dealsieve=_money(capex.normalized) or "",
            note=capex.basis,
        ),
    ]
    if financing.immediate_capex > 0:
        rows.extend(
            [
                ComparisonRow(
                    metric="Immediate capex",
                    broker=None,
                    dealsieve=_money(financing.immediate_capex) or "",
                ),
                ComparisonRow(
                    metric="All-in basis",
                    broker=_money(financing.purchase_price),
                    dealsieve=_money(financing.all_in_basis) or "",
                ),
            ]
        )
    rows.extend(
        [
        ComparisonRow(metric="DSCR", broker=None, dealsieve=_multiple(financing.dscr) or ""),
        ComparisonRow(
            metric="Price / sf",
            broker=_money(broker_price_per_sqft),
            dealsieve=_money(normalized.price_per_sqft) or "—",
        ),
        ]
    )
    return rows
