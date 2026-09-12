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

import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from rapidfuzz import fuzz

from dealsieve.schemas import ExtractedClaims, IdentityKeys, InboundMessage, ResolutionResult

if TYPE_CHECKING:
    from dealsieve.persistence import Repo

# Street-suffix abbreviations applied after uppercasing, whole-word only.
_SUFFIX_MAP = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "BOULEVARD": "BLVD",
    "ROAD": "RD",
    "DRIVE": "DR",
    "COURT": "CT",
    "LANE": "LN",
    "PARKWAY": "PKWY",
    "WAY": "WAY",
}

# Unit/suite markers to strip along with the token that follows them (e.g. "SUITE 200", "#200").
_UNIT_MARKER_RE = re.compile(r"#\s*\S+|\b(?:SUITE|STE|UNIT|APT|APARTMENT)\b\.?\s*\S*")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_NUMBER_RE = re.compile(r"^(\d+)")

# Fuzzy-address tier: >= this normalized score AND same city -> confident match (0.8).
_FUZZY_MATCH_THRESHOLD = 90
# Below this the candidate is not even worth surfacing as ambiguous.
_FUZZY_HUMAN_FLOOR = 50


def _clean_fragment(text: str) -> str:
    text = text.upper()
    text = _UNIT_MARKER_RE.sub(" ", text)
    text = _PUNCTUATION_RE.sub(" ", text)
    words = [_SUFFIX_MAP.get(w, w) for w in text.split()]
    return " ".join(words)


def normalize_address(
    address_line: str | None,
    city: str | None = None,
    state: str | None = None,
    postal_code: str | None = None,
) -> str | None:
    if not address_line or not address_line.strip():
        return None
    parts = [_clean_fragment(address_line)]
    for part in (city, state, postal_code):
        if part and part.strip():
            parts.append(_clean_fragment(part))
    normalized = _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()
    return normalized or None


def _leading_number(normalized_address: str | None) -> str | None:
    if not normalized_address:
        return None
    match = _LEADING_NUMBER_RE.match(normalized_address.strip())
    return match.group(1) if match else None


def _norm_city(city: str | None) -> str | None:
    if not city or not city.strip():
        return None
    return _WHITESPACE_RE.sub(" ", city.strip().upper())


def extract_identity_keys(message: InboundMessage, claims: ExtractedClaims) -> IdentityKeys:
    normalized_address = normalize_address(
        claims.address_line, claims.city, claims.state, claims.postal_code
    )
    listing_url = claims.listing_url
    if not listing_url and message.urls:
        listing_url = message.urls[0]
    return IdentityKeys(
        normalized_address=normalized_address,
        apn=claims.apn,
        broker_property_ref=claims.broker_property_ref,
        listing_url=listing_url,
        attachment_fingerprints=[a.sha256 for a in message.attachments],
        building_name=None,
        city=claims.city,
        building_sqft=claims.building_sqft,
        sender_email=message.sender,
        thread_id=message.thread_id,
    )


def _no_match() -> ResolutionResult:
    return ResolutionResult(
        opportunity_id=None,
        property_id=None,
        created=False,
        confidence=0.0,
        matched_on=[],
        ambiguous_candidates=[],
        needs_human=False,
    )


def resolve(keys: IdentityKeys, repo: Repo) -> ResolutionResult:
    """Pure lookup. Never creates anything. created is always False here."""

    # Tier 1 (0.95): message thread. A reply's thread_id is either the root of the References
    # chain or (when there is no chain) the raw In-Reply-To id, so a single value covers both
    # cases the contract calls out: thread-root lookup and direct in-reply-to message lookup.
    if keys.thread_id:
        opportunity_id = repo.find_opportunity_id_by_thread_id(keys.thread_id)
        if opportunity_id is None:
            opportunity_id = repo.find_opportunity_id_by_message_id(keys.thread_id)
        if opportunity_id is not None:
            opp = repo.get_opportunity(opportunity_id)
            return ResolutionResult(
                opportunity_id=opportunity_id,
                property_id=opp.property_id if opp else None,
                created=False,
                confidence=0.95,
                matched_on=["thread"],
                ambiguous_candidates=[],
                needs_human=False,
            )

    # Tier 2 (1.0): exact identity keys.
    exact = repo.find_opportunity_ids_by_keys(
        normalized_address=keys.normalized_address,
        apn=keys.apn,
        listing_url=keys.listing_url,
        broker_property_ref=keys.broker_property_ref,
    )
    if exact:
        counts = Counter(exact.values())
        winner_id, _ = counts.most_common(1)[0]
        matched_on = sorted(k for k, v in exact.items() if v == winner_id)
        opp = repo.get_opportunity(winner_id)
        return ResolutionResult(
            opportunity_id=winner_id,
            property_id=opp.property_id if opp else None,
            created=False,
            confidence=1.0,
            matched_on=matched_on,
            ambiguous_candidates=[],
            needs_human=False,
        )

    # Tier 3 (0.9): attachment fingerprint.
    for sha256 in keys.attachment_fingerprints:
        opportunity_id = repo.find_opportunity_id_by_attachment_sha(sha256)
        if opportunity_id is not None:
            opp = repo.get_opportunity(opportunity_id)
            return ResolutionResult(
                opportunity_id=opportunity_id,
                property_id=opp.property_id if opp else None,
                created=False,
                confidence=0.9,
                matched_on=["attachment_sha"],
                ambiguous_candidates=[],
                needs_human=False,
            )

    # Tier 4 (0.8): fuzzy address among all known properties.
    if keys.normalized_address:
        query_number = _leading_number(keys.normalized_address)
        scored: list[tuple[float, Any]] = []
        for prop in repo.list_properties():
            if not prop.normalized_address:
                continue
            # The street number is the single most discriminating token in a US address;
            # rapidfuzz's character-level ratio is not reliable at distinguishing "1234" from
            # "1240" when the rest of the string is identical, so gate on it explicitly.
            if _leading_number(prop.normalized_address) != query_number:
                continue
            score = fuzz.token_set_ratio(keys.normalized_address, prop.normalized_address)
            scored.append((score, prop))
        scored.sort(key=lambda item: item[0], reverse=True)

        if scored:
            best_score, best_prop = scored[0]
            same_city = keys.city is not None and _norm_city(keys.city) == _norm_city(best_prop.city)
            if best_score >= _FUZZY_MATCH_THRESHOLD and same_city:
                matched = repo.find_opportunity_ids_by_keys(normalized_address=best_prop.normalized_address)
                opportunity_id = matched.get("normalized_address")
                if opportunity_id is not None:
                    return ResolutionResult(
                        opportunity_id=opportunity_id,
                        property_id=best_prop.property_id,
                        created=False,
                        confidence=0.8,
                        matched_on=["fuzzy_address"],
                        ambiguous_candidates=[],
                        needs_human=False,
                    )

            scaled = best_score / 100.0
            if _FUZZY_HUMAN_FLOOR / 100.0 <= scaled < 0.8:
                candidates = []
                for score, prop in scored[:5]:
                    matched = repo.find_opportunity_ids_by_keys(normalized_address=prop.normalized_address)
                    candidates.append(
                        {
                            "property_id": prop.property_id,
                            "normalized_address": prop.normalized_address,
                            "opportunity_id": matched.get("normalized_address"),
                            "score": score / 100.0,
                        }
                    )
                return ResolutionResult(
                    opportunity_id=None,
                    property_id=None,
                    created=False,
                    confidence=scaled,
                    matched_on=["fuzzy_address"],
                    ambiguous_candidates=candidates,
                    needs_human=True,
                )

    return _no_match()
