"""Immutable investment policy.

Agents may read the policy. Agents may never write it. The version is a content
hash so every underwriting run can prove which rules it was evaluated under.
"""

from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_POLICY_PATH = Path(os.environ.get("DEALSIEVE_POLICY_PATH", "config/investment_policy.yaml"))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Capital(_Frozen):
    acquisition_equity: Decimal
    reserve_min: Decimal
    reserve_target: Decimal


class Purchase(_Frozen):
    preferred_min: Decimal
    preferred_max: Decimal
    absolute_max: Decimal


class SuiteRange(_Frozen):
    min: int
    max: int


class PropertyRules(_Frozen):
    target_type: str
    tenant_count_min: int
    largest_tenant_pct_max: Decimal
    preferred_suite_sqft: SuiteRange


class Underwriting(_Frozen):
    min_normalized_cap_rate: Decimal
    min_base_dscr: Decimal


class Financing(_Frozen):
    assumed_interest_rate: Decimal
    amortization_years: int
    max_ltv: Decimal
    closing_cost_pct: Decimal


class Normalization(_Frozen):
    management_fee_pct: Decimal
    normalized_vacancy_pct: Decimal
    require_property_tax_reset: bool
    property_tax_rate_pct: Decimal
    require_capex_reserve: bool
    capex_reserve_per_sqft: Decimal
    min_insurance_per_sqft: Decimal
    min_repairs_pct_of_egi: Decimal


class Stress(_Frozen):
    vacancy_pct: Decimal
    rent_haircut_pct: Decimal
    min_stress_dscr: Decimal


class Classification(_Frozen):
    near_threshold_pct: Decimal


class Outreach(_Frozen):
    auto_send_information_requests: bool
    auto_follow_up_approved_threads: bool
    follow_up_after_days: int
    max_follow_ups: int
    always_require_approval: tuple[str, ...]
    from_name: str
    from_email: str
    signature: str


class CapexPolicy(_Frozen):
    count_as_immediate: tuple[str, ...]
    use_midpoint: bool


class InvestmentPolicy(_Frozen):
    version: int
    name: str
    capital: Capital
    purchase: Purchase
    property: PropertyRules
    underwriting: Underwriting
    financing: Financing
    normalization: Normalization
    stress: Stress
    classification: Classification
    outreach: Outreach
    capex: CapexPolicy
    policy_version: str
    """Content hash identifier, e.g. "v1-3fa9c2d1e0b7". Set by load_policy()."""
    source_path: str
    raw_yaml: str = ""
    """The exact text the version hash was computed from, captured at load time (never re-read from disk)."""


def _decimalize(obj: Any) -> Any:
    """YAML floats become Decimal via str() so 0.08 stays exactly 0.08."""
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimalize(v) for v in obj]
    return obj


def policy_version(raw_text: str, version: int) -> str:
    digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:12]
    return f"v{version}-{digest}"


@lru_cache(maxsize=8)
def load_policy(path: str | os.PathLike[str] | None = None) -> InvestmentPolicy:
    p = Path(path) if path else DEFAULT_POLICY_PATH
    raw_text = p.read_text(encoding="utf-8")
    data = _decimalize(yaml.safe_load(raw_text))
    data["policy_version"] = policy_version(raw_text, int(data["version"]))
    data["source_path"] = str(p)
    data["raw_yaml"] = raw_text
    return InvestmentPolicy.model_validate(data)
