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
    if getattr(viability, "no_viable_price", False):
        # Nothing structural failed, but no purchase price passes the economic gates (NOI <= 0, for
        # instance). It is not DEAD -- the economics can still change -- and it is not NEAR, because
        # there is no frontier to be near. It is watched, with no price path (S7/F18).
        return OpportunityStatus.WATCH
    if (
        viability.distance_pct is not None
        and viability.distance_pct <= policy.classification.near_threshold_pct
    ):
        return OpportunityStatus.NEAR
    return OpportunityStatus.WATCH
