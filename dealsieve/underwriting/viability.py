"""Purchase-price viability frontier solver."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    ConstraintKind,
    GateResult,
    ViabilityFrontier,
    ViabilityPath,
    WorkingValues,
)
from dealsieve.underwriting.financing import compute_financing
from dealsieve.underwriting.gates import evaluate_gates
from dealsieve.underwriting.normalize import normalize_economics

MONEY = Decimal("0.01")
RATE = Decimal("0.000001")


def _gates_at(
    values: WorkingValues,
    policy: InvestmentPolicy,
    price: Decimal,
) -> list[GateResult]:
    normalized = normalize_economics(values, policy, price)
    financing = compute_financing(normalized.noi, price, policy)
    return evaluate_gates(values, normalized, financing, policy)


def _economic_passes(values: WorkingValues, policy: InvestmentPolicy, price: Decimal) -> bool:
    return all(
        gate.passed
        for gate in _gates_at(values, policy, price)
        if gate.kind == ConstraintKind.ECONOMIC
    )


def solve_max_viable_price(
    values: WorkingValues,
    policy: InvestmentPolicy,
) -> ViabilityFrontier:
    """Find the highest purchase price that passes every economic hard gate."""
    current_price = values.asking_price
    current_gates = _gates_at(values, policy, current_price)
    structural_failures = [
        gate.gate
        for gate in current_gates
        if gate.kind == ConstraintKind.STRUCTURAL and not gate.passed
    ]
    if structural_failures:
        return ViabilityFrontier(
            current_price=current_price.quantize(MONEY, rounding=ROUND_HALF_UP),
            max_viable_price=None,
            distance_pct=None,
            structural_failures=structural_failures,
        )

    lower = MONEY
    upper = policy.purchase.absolute_max
    if not _economic_passes(values, policy, lower):
        return ViabilityFrontier(
            current_price=current_price.quantize(MONEY, rounding=ROUND_HALF_UP),
            max_viable_price=None,
            distance_pct=None,
        )

    if _economic_passes(values, policy, upper):
        lower = upper
    else:
        while upper - lower >= Decimal("1"):
            midpoint = (lower + upper) / Decimal(2)
            if _economic_passes(values, policy, midpoint):
                lower = midpoint
            else:
                upper = midpoint

    max_viable_price = lower.quantize(MONEY, rounding=ROUND_DOWN)
    while max_viable_price > MONEY and not _economic_passes(values, policy, max_viable_price):
        max_viable_price -= MONEY

    probe_gates = _gates_at(values, policy, max_viable_price + Decimal("1000"))
    binding_constraints = [
        gate.gate
        for gate in probe_gates
        if gate.kind == ConstraintKind.ECONOMIC and not gate.passed
    ]
    raw_distance = (current_price - max_viable_price) / current_price
    distance = max(Decimal(0), raw_distance).quantize(RATE, rounding=ROUND_HALF_UP)
    return ViabilityFrontier(
        current_price=current_price.quantize(MONEY, rounding=ROUND_HALF_UP),
        max_viable_price=max_viable_price,
        distance_pct=distance,
        binding_constraints=binding_constraints,
        paths=[
            ViabilityPath(
                variable="purchase_price",
                current_value=current_price.quantize(MONEY, rounding=ROUND_HALF_UP),
                required_value=max_viable_price,
                description="Reduce purchase price to the maximum that passes every economic gate",
            )
        ],
        structural_failures=[],
    )
