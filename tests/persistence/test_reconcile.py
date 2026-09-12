from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.evidence.reconcile import MissingInputs, reconcile
from dealsieve.schemas import EventType, Evidence, ExpenseClaims, ExtractedClaims, TenantClaim


def test_first_claims_establish_working_values_from_gross_income():
    claims = ExtractedClaims(
        address_line="1234 Power Inn Road",
        city="Sacramento",
        asking_price=Decimal("1550000"),
        stated_gross_income=Decimal("180000"),
        stated_noi=Decimal("126000"),
        stated_expenses=ExpenseClaims(total=Decimal("54000")),
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
        evidence=[
            Evidence(field="asking_price", value=1550000, source_document="msg1", confidence=0.95),
        ],
    )
    working, changes = reconcile(None, claims)

    assert working.asking_price == Decimal("1550000")
    assert working.gross_scheduled_income == Decimal("180000")
    assert working.stated_noi == Decimal("126000")
    assert working.tenant_count == 8
    assert working.largest_tenant_pct == Decimal("0.19")
    assert working.provenance["asking_price"] == claims.evidence[0].evidence_id
    assert changes == []  # nothing to compare against yet


def test_first_claims_derive_gpr_from_tenant_rents_when_no_stated_income():
    tenants = [
        TenantClaim(name="A", annual_rent=Decimal("34200")),
        TenantClaim(name="B", annual_rent=Decimal("25200")),
        TenantClaim(name="C", annual_rent=Decimal("20600")),
    ]
    claims = ExtractedClaims(asking_price=Decimal("1000000"), tenants=tenants)
    working, _ = reconcile(None, claims)

    assert working.gross_scheduled_income == Decimal("80000")
    assert working.tenant_count == 3
    assert working.largest_tenant_pct == Decimal("34200") / Decimal("80000")


def test_first_claims_derive_gpr_from_noi_plus_expenses_when_no_other_source():
    claims = ExtractedClaims(
        asking_price=Decimal("950000"),
        stated_noi=Decimal("92000"),
        stated_expenses=ExpenseClaims(total=Decimal("28000")),
    )
    working, _ = reconcile(None, claims)
    assert working.gross_scheduled_income == Decimal("120000")


def test_first_claims_missing_inputs_raises_with_missing_field_names():
    with pytest.raises(MissingInputs) as excinfo:
        reconcile(None, ExtractedClaims(asking_price=Decimal("1000000")))
    assert excinfo.value.missing == ["gross_scheduled_income"]

    with pytest.raises(MissingInputs) as excinfo2:
        reconcile(None, ExtractedClaims(stated_gross_income=Decimal("100000")))
    assert excinfo2.value.missing == ["asking_price"]

    with pytest.raises(MissingInputs) as excinfo3:
        reconcile(None, ExtractedClaims())
    assert set(excinfo3.value.missing) == {"asking_price", "gross_scheduled_income"}


def test_price_change_emits_asking_price_changed_with_correct_pct_and_summary():
    initial = ExtractedClaims(asking_price=Decimal("1550000"), stated_gross_income=Decimal("180000"))
    working1, _ = reconcile(None, initial)

    price_drop = ExtractedClaims(asking_price=Decimal("1250000"), is_price_change=True)
    working2, changes = reconcile(working1, price_drop)

    assert working2.asking_price == Decimal("1250000")
    assert working2.gross_scheduled_income == Decimal("180000")  # unchanged field keeps prior value

    price_changes = [c for c in changes if c.type == EventType.ASKING_PRICE_CHANGED]
    assert len(price_changes) == 1
    change = price_changes[0]
    assert change.summary == "Asking price $1,550,000 -> $1,250,000 (-19.4%)"
    assert change.payload["from"] == pytest.approx(1550000.0)
    assert change.payload["to"] == pytest.approx(1250000.0)
    assert change.payload["pct"] == pytest.approx(-19.35, abs=0.01)


def test_no_price_change_when_price_repeated():
    initial = ExtractedClaims(asking_price=Decimal("1550000"), stated_gross_income=Decimal("180000"))
    working1, _ = reconcile(None, initial)
    working2, changes = reconcile(working1, ExtractedClaims(asking_price=Decimal("1550000")))
    assert changes == []
    assert working2.asking_price == Decimal("1550000")


def test_noi_change_over_1pct_emits_noi_changed_but_small_change_does_not():
    initial = ExtractedClaims(
        asking_price=Decimal("1000000"), stated_gross_income=Decimal("150000"), stated_noi=Decimal("100000")
    )
    working1, _ = reconcile(None, initial)

    # 0.5% change: below threshold, no event.
    working2, changes_small = reconcile(working1, ExtractedClaims(stated_noi=Decimal("100500")))
    assert not any(c.type == EventType.NOI_CHANGED for c in changes_small)

    # 5% change from the ORIGINAL noi: above threshold.
    working3, changes_big = reconcile(working1, ExtractedClaims(stated_noi=Decimal("105000")))
    noi_changes = [c for c in changes_big if c.type == EventType.NOI_CHANGED]
    assert len(noi_changes) == 1
    assert noi_changes[0].payload == {"from": 100000.0, "to": 105000.0}


def test_new_tenants_emit_rent_roll_updated():
    initial = ExtractedClaims(asking_price=Decimal("1000000"), stated_gross_income=Decimal("150000"))
    working1, _ = reconcile(None, initial)

    new_tenants = [TenantClaim(name="New Tenant", annual_rent=Decimal("50000"))]
    working2, changes = reconcile(working1, ExtractedClaims(tenants=new_tenants))

    rent_roll_changes = [c for c in changes if c.type == EventType.RENT_ROLL_UPDATED]
    assert len(rent_roll_changes) == 1
    assert working2.tenants == new_tenants
    # Tenant rent replaced the stated gross income as the GPR source now that tenants are known.
    assert working2.gross_scheduled_income == Decimal("50000")


def test_absent_fields_keep_prior_working_value():
    initial = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("150000"),
        building_sqft=20000,
        property_type="industrial",
        occupancy_pct=Decimal("0.95"),
    )
    working1, _ = reconcile(None, initial)

    working2, _ = reconcile(working1, ExtractedClaims(asking_price=Decimal("1000000")))
    assert working2.building_sqft == 20000
    assert working2.property_type == "industrial"
    assert working2.occupancy_pct == Decimal("0.95")


def test_intra_message_contradiction_recorded_and_higher_confidence_wins():
    claims = ExtractedClaims(
        asking_price=Decimal("1550000"),
        stated_gross_income=Decimal("180000"),
        evidence=[
            Evidence(field="asking_price", value=1550000, source_document="email body", confidence=0.95),
            Evidence(field="asking_price", value=1500000, source_document="Power_Inn_OM.md", confidence=0.6),
        ],
    )
    working, _ = reconcile(None, claims)

    assert len(working.conflicts) == 1
    assert "asking_price" in working.conflicts[0]
    winning_evidence_id = next(e.evidence_id for e in claims.evidence if e.confidence == 0.95)
    assert working.provenance["asking_price"] == winning_evidence_id


def test_expense_subfields_merge_field_by_field():
    initial = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("150000"),
        stated_expenses=ExpenseClaims(property_tax=Decimal("15000"), insurance=Decimal("8000")),
    )
    working1, _ = reconcile(None, initial)

    working2, _ = reconcile(
        working1,
        ExtractedClaims(stated_expenses=ExpenseClaims(utilities=Decimal("11000"))),
    )
    assert working2.stated_expenses.property_tax == Decimal("15000")  # kept
    assert working2.stated_expenses.insurance == Decimal("8000")  # kept
    assert working2.stated_expenses.utilities == Decimal("11000")  # newly added
