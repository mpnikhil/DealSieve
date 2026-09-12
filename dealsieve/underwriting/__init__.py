"""Deterministic finance. Owned by W1. Pure functions over Decimal. No model calls, ever.

Entry point: run_underwriting(values, policy, opportunity_id=...) -> UnderwritingResult
"""

from dealsieve.underwriting.engine import InvalidInputs, run_underwriting  # noqa: F401
