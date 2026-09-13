from __future__ import annotations

from decimal import Decimal

import pytest

from dealsieve.evidence.reconcile import MissingInputs, reconcile
from dealsieve.schemas import (
    CapexItem,
    EventType,
    Evidence,
    ExpenseClaims,
    ExtractedClaims,
    TenantClaim,
    WorkingValues,
)


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


# --------------------------------------------------------------------------- R7: reconciled value must
# never contradict the winning evidence


def test_r7_higher_confidence_evidence_overrides_disagreeing_top_level_claim():
    """The exact scenario from the review: the extractor's own top-level asking_price (1,500,000)
    disagrees with a higher-confidence piece of evidence it recorded (1,600,000). The reconciled
    value must be the evidence's value, with the disagreement recorded as a conflict and
    provenance pointing at the winning evidence."""
    claims = ExtractedClaims(
        asking_price=Decimal("1500000"),
        stated_gross_income=Decimal("180000"),
        evidence=[
            Evidence(field="asking_price", value=1600000, source_document="OM.pdf", confidence=0.95),
        ],
    )
    working, _ = reconcile(None, claims)

    assert working.asking_price == Decimal("1600000")
    assert any("1500000" in c and "1600000" in c and "asking_price" in c for c in working.conflicts)
    assert working.provenance["asking_price"] == claims.evidence[0].evidence_id


def test_r7_evidence_value_is_coerced_to_field_type():
    claims = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("150000"),
        evidence=[Evidence(field="building_sqft", value="20000", source_document="OM.pdf", confidence=0.9)],
    )
    working, _ = reconcile(None, claims)
    assert working.building_sqft == 20000
    assert isinstance(working.building_sqft, int)


def test_r7_no_conflict_recorded_when_top_level_claim_agrees_with_winning_evidence():
    claims = ExtractedClaims(
        asking_price=Decimal("1550000"),
        stated_gross_income=Decimal("180000"),
        evidence=[Evidence(field="asking_price", value=1550000, source_document="email body", confidence=0.95)],
    )
    working, _ = reconcile(None, claims)
    assert working.asking_price == Decimal("1550000")
    assert working.conflicts == []


def test_r7_asking_price_changed_event_uses_evidence_corrected_value():
    """Change detection (and its payload) must reflect the corrected value, not the raw claim."""
    initial = ExtractedClaims(asking_price=Decimal("1550000"), stated_gross_income=Decimal("180000"))
    working1, _ = reconcile(None, initial)

    price_drop = ExtractedClaims(
        asking_price=Decimal("1300000"),  # what the extractor's summary field says
        is_price_change=True,
        evidence=[
            Evidence(field="asking_price", value=1250000, source_document="email body", confidence=0.97)
        ],  # what the source actually says, at higher confidence
    )
    working2, changes = reconcile(working1, price_drop)

    assert working2.asking_price == Decimal("1250000")
    price_changes = [c for c in changes if c.type == EventType.ASKING_PRICE_CHANGED]
    assert len(price_changes) == 1
    assert price_changes[0].payload["to"] == pytest.approx(1250000.0)
    assert any("asking_price" in c for c in working2.conflicts)


def test_r7_evidence_field_without_caster_is_left_to_existing_merge_logic():
    """Evidence tagged with a field name outside _EVIDENCE_FIELD_CASTERS (e.g. a rent-roll note)
    must not blow up reconcile and must not silently mutate an unrelated WorkingValues field."""
    claims = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("150000"),
        evidence=[Evidence(field="roof_age", value="2001", source_document="OM.pdf", confidence=0.8)],
    )
    working, _ = reconcile(None, claims)
    assert working.asking_price == Decimal("1000000")
    assert working.provenance["roof_age"] == claims.evidence[0].evidence_id
    assert working.conflicts == []


@pytest.mark.parametrize("evidence_field", ["stated_gross_income", "gross_scheduled_income"])
def test_r7_gross_income_evidence_alias_overrides_claim_and_records_conflict(evidence_field):
    claims = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("180000"),
        evidence=[
            Evidence(
                field=evidence_field,
                value="190000",
                source_document="OM.pdf",
                confidence=0.95,
            )
        ],
    )

    working, _ = reconcile(None, claims)

    assert working.gross_scheduled_income == Decimal("190000")
    assert working.provenance["gross_scheduled_income"] == claims.evidence[0].evidence_id
    assert any("180000" in conflict and "190000" in conflict for conflict in working.conflicts)


def test_reconcile_preserves_capex_added_by_document_analysis():
    initial = ExtractedClaims(
        asking_price=Decimal("1000000"),
        stated_gross_income=Decimal("180000"),
    )
    working, _ = reconcile(None, initial)
    capex = CapexItem(
        item="Roof replacement",
        low=Decimal("85000"),
        high=Decimal("95000"),
        urgency="immediate",
        source_document="inspection.pdf",
    )
    with_capex = working.model_copy(
        update={"immediate_capex": Decimal("90000"), "capex_items": [capex]}
    )

    updated, _ = reconcile(with_capex, ExtractedClaims(notes="No new economics"))

    assert updated.immediate_capex == Decimal("90000")
    assert updated.capex_items == [capex]


def _existing_at(price: str) -> WorkingValues:
    from decimal import Decimal as _D

    return WorkingValues(asking_price=_D(price), gross_scheduled_income=_D("180000"))


def test_reply_subject_price_does_not_change_the_asking_price():
    """A 'Re: ... $1.55M' subject echoing the original listing must not re-set the price."""
    from decimal import Decimal as _D

    from dealsieve.schemas import Evidence, ExtractedClaims

    claims = ExtractedClaims(
        asking_price=_D("1550000"),
        is_price_change=False,
        evidence=[Evidence(field="asking_price", value=1550000, source_document="msg-3", location="email subject", confidence=0.9)],
    )
    working, changes = reconcile(_existing_at("1250000"), claims)
    assert working.asking_price == _D("1250000")
    assert changes == []
    assert any("restated without a change announcement" in c for c in working.conflicts)


def test_new_price_in_body_or_attachment_still_counts_without_the_flag():
    from decimal import Decimal as _D

    from dealsieve.schemas import EventType, Evidence, ExtractedClaims

    claims = ExtractedClaims(
        asking_price=_D("1200000"),
        is_price_change=False,
        evidence=[Evidence(field="asking_price", value=1200000, source_document="Updated_OM.pdf", location="OM page 1", confidence=0.95)],
    )
    working, changes = reconcile(_existing_at("1250000"), claims)
    assert working.asking_price == _D("1200000")
    assert [c.type for c in changes] == [EventType.ASKING_PRICE_CHANGED]
