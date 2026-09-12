"""Decide whether an inbound message refers to an opportunity we already know.

Deterministic first, fuzzy second, never silently merge low confidence (W2 implements):
- normalize_address: uppercase, strip punctuation, expand/abbreviate suffixes (Street->ST, Avenue->AVE,
  Boulevard->BLVD, Road->RD, Drive->DR, Court->CT, Lane->LN, Way->WAY, Parkway->PKWY, Suite/Ste/# removed),
  collapse whitespace, include city/state/zip when present. Same property -> same key.
- Confidence tiers: exact normalized_address or APN or listing_url or broker_property_ref = 1.0;
  thread_id / in_reply_to match to a prior message of an opportunity = 0.95;
  attachment fingerprint match = 0.9; fuzzy address (rapidfuzz token_set_ratio >= 90) + same city = 0.8;
  below 0.8 -> candidates listed, needs_human=True, opportunity_id=None.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dealsieve.schemas import ExtractedClaims, IdentityKeys, InboundMessage, ResolutionResult

if TYPE_CHECKING:
    from dealsieve.persistence import Repo


def normalize_address(address_line: str | None, city: str | None = None, state: str | None = None, postal_code: str | None = None) -> str | None:
    raise NotImplementedError("W2: dealsieve.identity.resolver.normalize_address")


def extract_identity_keys(message: InboundMessage, claims: ExtractedClaims) -> IdentityKeys:
    raise NotImplementedError("W2: dealsieve.identity.resolver.extract_identity_keys")


def resolve(keys: IdentityKeys, repo: Repo) -> ResolutionResult:
    """Pure lookup. Never creates anything. created is always False here."""
    raise NotImplementedError("W2: dealsieve.identity.resolver.resolve")
