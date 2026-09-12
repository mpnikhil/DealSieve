"""Merge ExtractedClaims into WorkingValues and detect what changed.

Deterministic rules (W2 implements):
- First claims for an opportunity establish WorkingValues (asking_price and gross_scheduled_income required;
  if gross income is missing but stated_noi and expenses exist, derive it; else raise MissingInputs).
- Later claims: a new asking_price replaces the old one and yields ASKING_PRICE_CHANGED.
  A new stated_noi that differs > 1% yields NOI_CHANGED. New tenants list yields RENT_ROLL_UPDATED.
- Fields absent from new claims keep their prior working value.
- Contradictions between sources in the same message (e.g. email body vs OM) are recorded in
  `conflicts` and the higher-confidence evidence wins. Nothing is deleted.
- provenance maps field -> evidence_id of the winning evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from dealsieve.schemas import EventType, Evidence, ExpenseClaims, ExtractedClaims, TenantClaim, WorkingValues

_NOI_CHANGE_THRESHOLD = Decimal("0.01")
_EXPENSE_FIELDS = (
    "property_tax",
    "insurance",
    "repairs_maintenance",
    "utilities",
    "management",
    "cam_other",
    "total",
)


class MissingInputs(ValueError):
    """Raised when claims lack what underwriting needs; carries the list of missing fields."""

    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"missing required inputs: {', '.join(missing)}")
        self.missing = missing


@dataclass
class DetectedChange:
    type: EventType
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)


def _merge_expenses(existing: ExpenseClaims | None, new: ExpenseClaims | None) -> ExpenseClaims:
    data: dict[str, Any] = {}
    for name in _EXPENSE_FIELDS:
        new_val = getattr(new, name) if new is not None else None
        old_val = getattr(existing, name) if existing is not None else None
        data[name] = new_val if new_val is not None else old_val
    return ExpenseClaims(**data)


def _derive_gpr(claims: ExtractedClaims) -> Decimal | None:
    if claims.tenants:
        rents = [t.annual_rent for t in claims.tenants if t.annual_rent is not None]
        if rents:
            total = Decimal("0")
            for rent in rents:
                total += rent
            return total
    if claims.stated_gross_income is not None:
        return claims.stated_gross_income
    if (
        claims.stated_noi is not None
        and claims.stated_expenses is not None
        and claims.stated_expenses.total is not None
    ):
        return claims.stated_noi + claims.stated_expenses.total
    return None


def _resolve_provenance_and_conflicts(
    claims: ExtractedClaims, prior_provenance: dict[str, str], prior_conflicts: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Group this message's evidence by field; higher-confidence evidence wins; nothing is deleted."""
    provenance = dict(prior_provenance)
    conflicts = list(prior_conflicts)

    by_field: dict[str, list[Evidence]] = {}
    for ev in claims.evidence:
        by_field.setdefault(ev.field, []).append(ev)

    for field_name, items in by_field.items():
        if len(items) == 1:
            provenance[field_name] = items[0].evidence_id
            continue
        ranked = sorted(items, key=lambda i: i.confidence, reverse=True)
        winner = ranked[0]
        distinct_values = {repr(i.value) for i in items}
        if len(distinct_values) > 1:
            detail = ", ".join(f"{i.value!r} (conf {i.confidence:.2f} from {i.source_document})" for i in items)
            conflicts.append(
                f"{field_name}: conflicting values among sources this message -- {detail} "
                f"-- kept {winner.value!r} from {winner.source_document}"
            )
        provenance[field_name] = winner.evidence_id

    return provenance, conflicts


def reconcile(existing: WorkingValues | None, claims: ExtractedClaims) -> tuple[WorkingValues, list[DetectedChange]]:
    provenance, conflicts = _resolve_provenance_and_conflicts(
        claims,
        existing.provenance if existing else {},
        existing.conflicts if existing else [],
    )

    new_price = claims.asking_price
    if new_price is not None:
        asking_price = new_price
    elif existing is not None:
        asking_price = existing.asking_price
    else:
        asking_price = None

    derived_gpr = _derive_gpr(claims)
    if derived_gpr is not None:
        gross_scheduled_income = derived_gpr
    elif existing is not None:
        gross_scheduled_income = existing.gross_scheduled_income
    else:
        gross_scheduled_income = None

    missing: list[str] = []
    if asking_price is None:
        missing.append("asking_price")
    if gross_scheduled_income is None:
        missing.append("gross_scheduled_income")
    if missing:
        raise MissingInputs(missing)

    other_income = (
        claims.stated_other_income
        if claims.stated_other_income is not None
        else (existing.other_income if existing else Decimal("0"))
    )
    stated_vacancy_pct = (
        claims.stated_vacancy_pct
        if claims.stated_vacancy_pct is not None
        else (existing.stated_vacancy_pct if existing else Decimal("0"))
    )
    stated_expenses = _merge_expenses(existing.stated_expenses if existing else None, claims.stated_expenses)

    new_noi = claims.stated_noi
    if new_noi is not None:
        stated_noi = new_noi
    elif existing is not None:
        stated_noi = existing.stated_noi
    else:
        stated_noi = None

    building_sqft = (
        claims.building_sqft if claims.building_sqft is not None else (existing.building_sqft if existing else None)
    )

    tenants: list[TenantClaim]
    if claims.tenants:
        tenants = list(claims.tenants)
    elif existing is not None:
        tenants = list(existing.tenants)
    else:
        tenants = []

    if claims.tenant_count is not None:
        tenant_count = claims.tenant_count
    elif claims.tenants:
        tenant_count = len(claims.tenants)
    elif existing is not None:
        tenant_count = existing.tenant_count
    else:
        tenant_count = None

    if claims.largest_tenant_pct is not None:
        largest_tenant_pct = claims.largest_tenant_pct
    elif claims.tenants:
        rents = [t.annual_rent for t in claims.tenants if t.annual_rent is not None]
        if rents and gross_scheduled_income:
            largest_tenant_pct = max(rents) / gross_scheduled_income
        else:
            largest_tenant_pct = existing.largest_tenant_pct if existing else None
    elif existing is not None:
        largest_tenant_pct = existing.largest_tenant_pct
    else:
        largest_tenant_pct = None

    occupancy_pct = (
        claims.occupancy_pct if claims.occupancy_pct is not None else (existing.occupancy_pct if existing else None)
    )
    property_type = (
        claims.property_type if claims.property_type is not None else (existing.property_type if existing else None)
    )

    working = WorkingValues(
        asking_price=asking_price,
        gross_scheduled_income=gross_scheduled_income,
        other_income=other_income,
        stated_vacancy_pct=stated_vacancy_pct,
        stated_expenses=stated_expenses,
        stated_noi=stated_noi,
        building_sqft=building_sqft,
        tenant_count=tenant_count,
        largest_tenant_pct=largest_tenant_pct,
        occupancy_pct=occupancy_pct,
        tenants=tenants,
        property_type=property_type,
        provenance=provenance,
        conflicts=conflicts,
    )

    changes: list[DetectedChange] = []
    if existing is not None:
        if new_price is not None and new_price != existing.asking_price:
            old_price = existing.asking_price
            pct = (new_price - old_price) / old_price * Decimal("100") if old_price != 0 else Decimal("0")
            changes.append(
                DetectedChange(
                    type=EventType.ASKING_PRICE_CHANGED,
                    summary=f"Asking price ${old_price:,.0f} -> ${new_price:,.0f} ({pct:+.1f}%)",
                    payload={"from": float(old_price), "to": float(new_price), "pct": float(pct)},
                )
            )

        if new_noi is not None and existing.stated_noi is not None and new_noi != existing.stated_noi:
            old_noi = existing.stated_noi
            noi_pct = abs(new_noi - old_noi) / old_noi if old_noi != 0 else Decimal("1")
            if noi_pct > _NOI_CHANGE_THRESHOLD:
                changes.append(
                    DetectedChange(
                        type=EventType.NOI_CHANGED,
                        summary=f"NOI ${old_noi:,.0f} -> ${new_noi:,.0f}",
                        payload={"from": float(old_noi), "to": float(new_noi)},
                    )
                )

        if claims.tenants and list(claims.tenants) != list(existing.tenants):
            changes.append(
                DetectedChange(
                    type=EventType.RENT_ROLL_UPDATED,
                    summary=f"Rent roll updated: {len(claims.tenants)} tenants",
                    payload={"tenant_count": len(claims.tenants)},
                )
            )

    return working, changes
