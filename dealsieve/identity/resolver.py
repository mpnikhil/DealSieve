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
# R8: a score below 80 is not actionable; [80, 90) is surfaced for human review.
_FUZZY_HUMAN_FLOOR = 80


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
    normalized_address = normalize_address(claims.address_line, claims.city, claims.state, claims.postal_code)
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


def _add_candidate(
    candidates: dict[str, dict[str, Any]], opportunity_id: str, matched_on: str, confidence: float
) -> None:
    candidate = candidates.setdefault(
        opportunity_id,
        {"opportunity_id": opportunity_id, "matched_on": [], "confidence": confidence},
    )
    candidate["matched_on"].append(matched_on)
    candidate["confidence"] = max(candidate["confidence"], confidence)


def _candidate_details(repo: Repo, candidates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for opportunity_id, candidate in candidates.items():
        item = dict(candidate)
        item["matched_on"] = sorted(set(item["matched_on"]))
        opp = repo.get_opportunity(opportunity_id)
        if opp is not None:
            item["property_id"] = opp.property_id
            item["deal_number"] = opp.deal_number
        details.append(item)
    return details


def _ambiguous(repo: Repo, candidates: dict[str, dict[str, Any]]) -> ResolutionResult:
    details = _candidate_details(repo, candidates)
    return ResolutionResult(
        opportunity_id=None,
        property_id=None,
        created=False,
        confidence=max(float(item["confidence"]) for item in details),
        matched_on=sorted({key for item in details for key in item["matched_on"]}),
        ambiguous_candidates=details,
        needs_human=True,
    )


def _scored_properties(keys: IdentityKeys, repo: Repo) -> list[tuple[float, Any]]:
    if not keys.normalized_address:
        return []
    query_number = _leading_number(keys.normalized_address)
    scored: list[tuple[float, Any]] = []
    for prop in repo.list_properties():
        if not prop.normalized_address:
            continue
        # A different street number is a contradiction, not a fuzzy spelling variation.
        if _leading_number(prop.normalized_address) != query_number:
            continue
        score = fuzz.token_set_ratio(keys.normalized_address, prop.normalized_address)
        scored.append((score, prop))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def resolve(keys: IdentityKeys, repo: Repo) -> ResolutionResult:
    """Pure lookup. Never creates anything. created is always False here."""

    # R8: collect every deterministic identifier before choosing. Returning on the thread match
    # would silently merge a reply whose explicit address/APN actually identifies another deal.
    candidates: dict[str, dict[str, Any]] = {}
    if keys.thread_id:
        thread_match = repo.find_opportunity_id_by_thread_id(keys.thread_id)
        if thread_match is not None:
            _add_candidate(candidates, thread_match, "thread", 0.95)
        reply_match = repo.find_opportunity_id_by_message_id(keys.thread_id)
        if reply_match is not None:
            _add_candidate(candidates, reply_match, "thread", 0.95)

    exact = repo.find_opportunity_ids_by_keys(
        normalized_address=keys.normalized_address,
        apn=keys.apn,
        listing_url=keys.listing_url,
        broker_property_ref=keys.broker_property_ref,
    )
    for matched_on, opportunity_id in exact.items():
        _add_candidate(candidates, opportunity_id, matched_on, 1.0)

    # Attachment fingerprints are deterministic too. A mismatch with the thread/address is
    # evidence of a crossed thread or reused document and needs human review.
    for sha256 in keys.attachment_fingerprints:
        attachment_match = repo.find_opportunity_id_by_attachment_sha(sha256)
        if attachment_match is not None:
            _add_candidate(candidates, attachment_match, "attachment_sha", 0.9)

    if len(candidates) > 1:
        return _ambiguous(repo, candidates)

    if candidates:
        winner_id, winner = next(iter(candidates.items()))
        opp = repo.get_opportunity(winner_id)
        return ResolutionResult(
            opportunity_id=winner_id,
            property_id=opp.property_id if opp else None,
            created=False,
            confidence=winner["confidence"],
            matched_on=sorted(set(winner["matched_on"])),
            ambiguous_candidates=[],
            needs_human=False,
        )

    # Tier 4 (0.8): fuzzy address among all known properties.
    scored = _scored_properties(keys, repo)
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

        # R8: 80 <= score < 90 is an ambiguity, not a silent no-match. City disagreement also
        # prevents auto-merge and is therefore surfaced for review at these actionable scores.
        if best_score >= _FUZZY_HUMAN_FLOOR:
            fuzzy_candidates = []
            for score, prop in scored[:5]:
                if score < _FUZZY_HUMAN_FLOOR:
                    continue
                matched = repo.find_opportunity_ids_by_keys(normalized_address=prop.normalized_address)
                fuzzy_candidates.append(
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
                confidence=best_score / 100.0,
                matched_on=["fuzzy_address"],
                ambiguous_candidates=fuzzy_candidates,
                needs_human=True,
            )

    return _no_match()
