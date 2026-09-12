from __future__ import annotations

import json
from decimal import Decimal
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from dealsieve.schemas import ExpenseClaims, ExtractedClaims, OpportunityStatus, WorkingValues
from dealsieve.underwriting import run_underwriting

NAMES = [
    "01_initial_offer",
    "02_price_drop",
    "03_structural_single_tenant",
    "04_obvious_economic_failure",
]


def _load_claims(fixtures_dir: Path, name: str) -> ExtractedClaims:
    path = fixtures_dir / "expected" / f"claims_{name}.json"
    return ExtractedClaims.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _working_values(claims: ExtractedClaims) -> WorkingValues:
    rents = [tenant.annual_rent for tenant in claims.tenants if tenant.annual_rent is not None]
    gpr = sum(rents, Decimal(0)) if rents else claims.stated_gross_income
    if gpr is None or claims.asking_price is None:
        raise AssertionError("golden first-message claims must contain price and GPR")
    tenant_count = len(claims.tenants) if claims.tenants else claims.tenant_count
    largest_tenant_pct = max(rents) / gpr if rents else claims.largest_tenant_pct
    return WorkingValues(
        asking_price=claims.asking_price,
        gross_scheduled_income=gpr,
        other_income=claims.stated_other_income or Decimal(0),
        stated_vacancy_pct=claims.stated_vacancy_pct or Decimal(0),
        stated_expenses=claims.stated_expenses or ExpenseClaims(),
        stated_noi=claims.stated_noi,
        building_sqft=claims.building_sqft,
        tenant_count=tenant_count,
        largest_tenant_pct=largest_tenant_pct,
        occupancy_pct=claims.occupancy_pct,
        tenants=claims.tenants,
        property_type=claims.property_type,
    )


def _reconcile_by_hand(fixtures_dir: Path, name: str) -> WorkingValues:
    claims = _load_claims(fixtures_dir, name)
    if name != "02_price_drop":
        return _working_values(claims)

    baseline = _working_values(_load_claims(fixtures_dir, "01_initial_offer"))
    if claims.asking_price is None:
        raise AssertionError("price-drop claims must contain the new price")
    return baseline.model_copy(update={"asking_price": claims.asking_price})


@pytest.mark.parametrize("name", NAMES)
def test_engine_matches_golden_claim_ranges(fixtures_dir, policy, name):
    claims = _load_claims(fixtures_dir, name)
    assert claims.evidence
    values = _reconcile_by_hand(fixtures_dir, name)
    result = run_underwriting(values, policy, opportunity_id=f"fixture-{name}")
    expected = json.loads(
        (fixtures_dir / "expected" / f"{name}.json").read_text(encoding="utf-8")
    )

    assert result.status == OpportunityStatus(expected["status"])
    cap_low, cap_high = map(Decimal, map(str, expected["normalized_cap_rate"]))
    dscr_low, dscr_high = map(Decimal, map(str, expected["dscr"]))
    assert cap_low <= result.normalized.normalized_cap_rate <= cap_high
    assert dscr_low <= result.financing.dscr <= dscr_high
    if expected["max_viable_price"] is None:
        assert result.viability.max_viable_price is None
    else:
        price_low, price_high = map(Decimal, map(str, expected["max_viable_price"]))
        assert result.viability.max_viable_price is not None
        assert price_low <= result.viability.max_viable_price <= price_high
    assert result.viability.structural_failures == expected["structural_failures"]
    assert len(result.gates) == 6
    assert len(result.stress) == 3
    assert len(result.comparison) == 8
    assert result.failure_summary


def test_fixture_01_and_price_drop_hit_demo_targets(fixtures_dir, policy):
    first = run_underwriting(
        _reconcile_by_hand(fixtures_dir, "01_initial_offer"),
        policy,
        opportunity_id="fixture-01",
    )
    dropped = run_underwriting(
        _reconcile_by_hand(fixtures_dir, "02_price_drop"),
        policy,
        opportunity_id="fixture-02",
    )
    assert Decimal("0.063") <= first.normalized.normalized_cap_rate <= Decimal("0.069")
    assert Decimal("1.00") <= first.financing.dscr <= Decimal("1.25")
    assert all(gate.passed for gate in first.gates if not gate.price_dependent)
    assert first.status == OpportunityStatus.WATCH
    assert dropped.normalized.normalized_cap_rate >= Decimal("0.082")
    assert dropped.financing.dscr >= Decimal("1.40")
    assert dropped.status == OpportunityStatus.REVIEW


def test_fixture_03_is_unfixably_structural(fixtures_dir, policy):
    result = run_underwriting(
        _reconcile_by_hand(fixtures_dir, "03_structural_single_tenant"),
        policy,
        opportunity_id="fixture-03",
    )
    assert result.status == OpportunityStatus.DEAD
    assert set(result.viability.structural_failures) == {
        "tenant_count_min",
        "largest_tenant_pct_max",
    }
    assert result.viability.max_viable_price is None


def test_fixture_04_is_far_from_viable(fixtures_dir, policy):
    result = run_underwriting(
        _reconcile_by_hand(fixtures_dir, "04_obvious_economic_failure"),
        policy,
        opportunity_id="fixture-04",
    )
    assert result.status == OpportunityStatus.WATCH
    assert result.viability.distance_pct is not None
    assert result.viability.distance_pct > Decimal("0.05")


def test_email_fixtures_are_valid_mime(fixtures_dir):
    parsed = {}
    for path in sorted((fixtures_dir / "emails").glob("*.eml")):
        with path.open("rb") as stream:
            message = BytesParser(policy=email_policy.default).parse(stream)
        assert message["Message-ID"]
        parsed[path.name] = message

    first_attachments = list(parsed["01_initial_offer.eml"].iter_attachments())
    assert [(part.get_content_type(), part.get_filename()) for part in first_attachments] == [
        ("text/markdown", "Power_Inn_OM.md")
    ]
    assert not list(parsed["02_price_drop.eml"].iter_attachments())
    assert parsed["02_price_drop.eml"]["In-Reply-To"] == parsed["01_initial_offer.eml"][
        "Message-ID"
    ]
    assert parsed["02_price_drop.eml"]["References"] == parsed["01_initial_offer.eml"][
        "Message-ID"
    ]
