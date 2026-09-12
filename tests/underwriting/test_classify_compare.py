from __future__ import annotations

from decimal import Decimal

from dealsieve.schemas import (
    ConstraintKind,
    GateResult,
    OpportunityStatus,
    ViabilityFrontier,
)
from dealsieve.underwriting.classify import classify
from dealsieve.underwriting.compare import broker_vs_dealsieve
from dealsieve.underwriting.financing import compute_financing
from dealsieve.underwriting.normalize import normalize_economics


def _gate(*, kind: ConstraintKind, passed: bool) -> GateResult:
    return GateResult(
        gate="test",
        kind=kind,
        description="test gate",
        comparator=">=",
        threshold=Decimal(1),
        actual=Decimal(1) if passed else Decimal(0),
        passed=passed,
        price_dependent=kind == ConstraintKind.ECONOMIC,
    )


def _viability(distance: str | None) -> ViabilityFrontier:
    return ViabilityFrontier(
        current_price=Decimal("100"),
        max_viable_price=None if distance is None else Decimal("90"),
        distance_pct=None if distance is None else Decimal(distance),
    )


def test_classify_all_four_statuses(policy):
    assert (
        classify(
            [_gate(kind=ConstraintKind.STRUCTURAL, passed=False)],
            _viability(None),
            Decimal("100"),
            policy,
        )
        == OpportunityStatus.DEAD
    )
    assert (
        classify(
            [
                _gate(kind=ConstraintKind.STRUCTURAL, passed=True),
                _gate(kind=ConstraintKind.ECONOMIC, passed=True),
            ],
            _viability("0"),
            Decimal("100"),
            policy,
        )
        == OpportunityStatus.REVIEW
    )
    failing_economic = [
        _gate(kind=ConstraintKind.STRUCTURAL, passed=True),
        _gate(kind=ConstraintKind.ECONOMIC, passed=False),
    ]
    assert (
        classify(failing_economic, _viability("0.05"), Decimal("100"), policy)
        == OpportunityStatus.NEAR
    )
    assert (
        classify(failing_economic, _viability("0.050001"), Decimal("100"), policy)
        == OpportunityStatus.WATCH
    )


def test_comparison_rows_and_formatting(demo_values, policy):
    normalized = normalize_economics(demo_values, policy, demo_values.asking_price)
    financing = compute_financing(normalized.noi, demo_values.asking_price, policy)
    rows = broker_vs_dealsieve(demo_values, normalized, financing)
    assert [row.metric for row in rows] == [
        "NOI",
        "Cap rate",
        "Vacancy",
        "Management",
        "Property tax",
        "CapEx reserve",
        "DSCR",
        "Price / sf",
    ]
    by_metric = {row.metric: row for row in rows}
    assert by_metric["NOI"].broker == "$126,000"
    assert by_metric["NOI"].dealsieve == "$99,575"
    assert by_metric["Cap rate"].broker == "8.13%"
    assert by_metric["Cap rate"].dealsieve == "6.42%"
    assert by_metric["Management"].broker is None
    assert by_metric["CapEx reserve"].broker is None
    assert by_metric["DSCR"].broker is None
    assert by_metric["DSCR"].dealsieve == "1.02x"
    assert by_metric["Price / sf"].dealsieve == "$78"
