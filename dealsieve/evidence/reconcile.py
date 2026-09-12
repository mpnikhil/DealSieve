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
from typing import Any

from dealsieve.schemas import EventType, ExtractedClaims, WorkingValues


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


def reconcile(existing: WorkingValues | None, claims: ExtractedClaims) -> tuple[WorkingValues, list[DetectedChange]]:
    raise NotImplementedError("W2: dealsieve.evidence.reconcile.reconcile")
