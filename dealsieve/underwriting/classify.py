"""Opportunity status classification."""

from __future__ import annotations

from decimal import Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import ConstraintKind, GateResult, OpportunityStatus, ViabilityFrontier


def classify(
    gates: list[GateResult],
    viability: ViabilityFrontier,
    price: Decimal,
    policy: InvestmentPolicy,
) -> OpportunityStatus:
    """Classify from hard gates and distance to the viability frontier."""
    del price
    if any(gate.kind == ConstraintKind.STRUCTURAL and not gate.passed for gate in gates):
        return OpportunityStatus.DEAD
    if all(gate.passed for gate in gates):
        return OpportunityStatus.REVIEW
    if (
        viability.distance_pct is not None
        and viability.distance_pct <= policy.classification.near_threshold_pct
    ):
        return OpportunityStatus.NEAR
    return OpportunityStatus.WATCH
