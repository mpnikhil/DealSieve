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
- R7: when this message's evidence includes one or more items for a field WorkingValues can hold
  directly (asking_price, gross_scheduled_income, stated_noi, other_income, stated_vacancy_pct,
  building_sqft, tenant_count, largest_tenant_pct, occupancy_pct, property_type), the reconciled
  value for that field is the winning (highest-confidence, latest-on-ties) Evidence.value, coerced
  to the field's type -- never the top-level claim field taken at face value. If the top-level
  claim stated a different value for that field, the disagreement is recorded in `conflicts` and
  the winning evidence value is used anyway: the reconciled value must never contradict the
  evidence actually recorded for it.
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


def _to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# R7 evidence normally uses ExtractedClaims names, while WorkingValues deliberately uses a few
# different names. Each entry maps evidence field -> (working field, claim field, caster). Accept
# the WorkingValues aliases too because callers may already emit those canonical names.
_EVIDENCE_FIELD_SPECS: dict[str, tuple[str, str, Any]] = {
    "asking_price": ("asking_price", "asking_price", _to_decimal),
    "gross_scheduled_income": (
        "gross_scheduled_income",
        "stated_gross_income",
        _to_decimal,
    ),
    "stated_gross_income": (
        "gross_scheduled_income",
        "stated_gross_income",
        _to_decimal,
    ),
    "other_income": ("other_income", "stated_other_income", _to_decimal),
    "stated_other_income": ("other_income", "stated_other_income", _to_decimal),
    "stated_vacancy_pct": ("stated_vacancy_pct", "stated_vacancy_pct", _to_decimal),
    "stated_noi": ("stated_noi", "stated_noi", _to_decimal),
    "building_sqft": ("building_sqft", "building_sqft", int),
    "tenant_count": ("tenant_count", "tenant_count", int),
    "largest_tenant_pct": ("largest_tenant_pct", "largest_tenant_pct", _to_decimal),
    "occupancy_pct": ("occupancy_pct", "occupancy_pct", _to_decimal),
    "property_type": ("property_type", "property_type", str),
}


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


def _winning_evidence(items: list[Evidence]) -> Evidence:
    """Highest confidence wins; ties go to the item that appears later in the list, which is
    taken to be the more recently asserted fact (R7: "latest on ties")."""
    return max(enumerate(items), key=lambda pair: (pair[1].confidence, pair[0]))[1]


def _resolve_provenance_and_conflicts(
    claims: ExtractedClaims, prior_provenance: dict[str, str], prior_conflicts: list[str]
) -> tuple[dict[str, str], list[str], dict[str, Any]]:
    """Group this message's evidence by field; higher-confidence evidence wins; nothing is deleted.

    Returns (provenance, conflicts, evidence_overrides). ``evidence_overrides`` holds, for every
    field in ``_EVIDENCE_FIELD_CASTERS`` that has evidence this message, the winning value
    (coerced to the field's type) that ``reconcile`` must use instead of the raw top-level claim
    field (R7).
    """
    provenance = dict(prior_provenance)
    conflicts = list(prior_conflicts)
    overrides: dict[str, Any] = {}

    by_field: dict[str, list[Evidence]] = {}
    for ev in claims.evidence:
        spec = _EVIDENCE_FIELD_SPECS.get(ev.field)
        working_field = spec[0] if spec is not None else ev.field
        by_field.setdefault(working_field, []).append(ev)

    for field_name, items in by_field.items():
        winner = _winning_evidence(items)
        provenance[field_name] = winner.evidence_id

        # Two or more sources disagreeing with EACH OTHER this message is always worth recording,
        # independent of whether the top-level claim field agrees with the winner.
        distinct_values = {repr(i.value) for i in items}
        if len(distinct_values) > 1:
            detail = ", ".join(
                f"{i.value!r} (conf {i.confidence:.2f} from {i.source_document})" for i in items
            )
            conflicts.append(
                f"{field_name}: conflicting values among sources this message -- {detail} "
                f"-- kept {winner.value!r} from {winner.source_document}"
            )

        spec = _EVIDENCE_FIELD_SPECS.get(winner.field)
        if spec is None or winner.value is None:
            continue
        working_field, claim_field, caster = spec
        try:
            winning_value = caster(winner.value)
        except (TypeError, ValueError, ArithmeticError):
            conflicts.append(
                f"{working_field}: winning evidence value {winner.value!r} from "
                f"{winner.source_document} could not be coerced; left unresolved"
            )
            continue
        overrides[working_field] = winning_value

        # R7: the reconciled value must never contradict the evidence recorded for it. If the
        # extractor's own top-level summary field disagrees, that is itself a conflict worth a
        # human being able to see -- but the evidence value, not the claim's, wins.
        claim_value = getattr(claims, claim_field, None)
        if claim_value is not None and claim_value != winning_value:
            conflicts.append(
                f"{working_field}: top-level claim {claim_value!r} disagrees with higher-confidence "
                f"evidence {winning_value!r} (conf {winner.confidence:.2f} from {winner.source_document}) "
                f"-- using the evidence value"
            )

    return provenance, conflicts, overrides


def _price_restated_without_change(claims: ExtractedClaims) -> bool:
    """True when a differing asking_price should be ignored: the message does not announce a price change
    and every piece of price evidence comes from the subject line (or there is none at all)."""
    if claims.is_price_change:
        return False
    price_evidence = [e for e in claims.evidence if e.field == "asking_price"]
    if not price_evidence:
        return True
    return all("subject" in (e.location or "").lower() for e in price_evidence)


def reconcile(
    existing: WorkingValues | None, claims: ExtractedClaims
) -> tuple[WorkingValues, list[DetectedChange]]:
    provenance, conflicts, evidence_overrides = _resolve_provenance_and_conflicts(
        claims,
        existing.provenance if existing else {},
        existing.conflicts if existing else [],
    )

    def _effective(field_name: str, claim_value: Any) -> Any:
        """R7: prefer this message's winning evidence value over the raw claim field, when there
        is evidence for the field at all."""
        return evidence_overrides[field_name] if field_name in evidence_overrides else claim_value

    new_price = _effective("asking_price", claims.asking_price)
    if existing is not None and new_price is not None and new_price != existing.asking_price:
        if _price_restated_without_change(claims):
            # A reply whose "Re: ... $1.55M" subject echoes the original listing is not a new price.
            conflicts.append(
                f"Ignored asking price ${new_price:,.0f} restated without a change announcement "
                f"(no body or attachment evidence); kept ${existing.asking_price:,.0f}"
            )
            new_price = None
    if new_price is not None:
        asking_price = new_price
    elif existing is not None:
        asking_price = existing.asking_price
    else:
        asking_price = None

    derived_gpr = _effective("gross_scheduled_income", _derive_gpr(claims))
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

    other_income_claim = _effective("other_income", claims.stated_other_income)
    other_income = (
        other_income_claim
        if other_income_claim is not None
        else (existing.other_income if existing else Decimal("0"))
    )
    stated_vacancy_pct_claim = _effective("stated_vacancy_pct", claims.stated_vacancy_pct)
    stated_vacancy_pct = (
        stated_vacancy_pct_claim
        if stated_vacancy_pct_claim is not None
        else (existing.stated_vacancy_pct if existing else Decimal("0"))
    )
    stated_expenses = _merge_expenses(existing.stated_expenses if existing else None, claims.stated_expenses)

    new_noi = _effective("stated_noi", claims.stated_noi)
    if new_noi is not None:
        stated_noi = new_noi
    elif existing is not None:
        stated_noi = existing.stated_noi
    else:
        stated_noi = None

    building_sqft_claim = _effective("building_sqft", claims.building_sqft)
    building_sqft = (
        building_sqft_claim
        if building_sqft_claim is not None
        else (existing.building_sqft if existing else None)
    )

    tenants: list[TenantClaim]
    if claims.tenants:
        tenants = list(claims.tenants)
    elif existing is not None:
        tenants = list(existing.tenants)
    else:
        tenants = []

    tenant_count_claim = _effective("tenant_count", claims.tenant_count)
    if tenant_count_claim is not None:
        tenant_count = tenant_count_claim
    elif claims.tenants:
        tenant_count = len(claims.tenants)
    elif existing is not None:
        tenant_count = existing.tenant_count
    else:
        tenant_count = None

    largest_tenant_pct_claim = _effective("largest_tenant_pct", claims.largest_tenant_pct)
    if largest_tenant_pct_claim is not None:
        largest_tenant_pct = largest_tenant_pct_claim
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

    occupancy_pct_claim = _effective("occupancy_pct", claims.occupancy_pct)
    occupancy_pct = (
        occupancy_pct_claim
        if occupancy_pct_claim is not None
        else (existing.occupancy_pct if existing else None)
    )
    property_type_claim = _effective("property_type", claims.property_type)
    property_type = (
        property_type_claim
        if property_type_claim is not None
        else (existing.property_type if existing else None)
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
        immediate_capex=existing.immediate_capex if existing else Decimal("0"),
        capex_items=list(existing.capex_items) if existing else [],
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
