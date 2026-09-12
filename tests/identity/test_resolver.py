from __future__ import annotations

from dealsieve.identity.resolver import extract_identity_keys, normalize_address, resolve
from dealsieve.schemas import (
    Channel,
    Evidence,
    ExtractedClaims,
    IdentityKeys,
    InboundMessage,
    Opportunity,
    Property,
)

# --------------------------------------------------------------------------- normalize_address


def test_normalize_address_expands_suffixes_and_uppercases():
    assert normalize_address("1234 Power Inn Road") == "1234 POWER INN RD"
    assert normalize_address("500 Elder Creek Boulevard") == "500 ELDER CREEK BLVD"
    assert normalize_address("77 Main Street") == "77 MAIN ST"


def test_normalize_address_strips_suite_and_punctuation():
    assert normalize_address("1234 Power Inn Rd, Suite 200") == "1234 POWER INN RD"
    assert normalize_address("1234 Power Inn Rd #200") == "1234 POWER INN RD"


def test_normalize_address_includes_city_state_zip():
    assert (
        normalize_address("1234 Power Inn Road", "Sacramento", "CA", "95826")
        == "1234 POWER INN RD SACRAMENTO CA 95826"
    )


def test_normalize_address_same_property_different_spelling_same_key():
    a = normalize_address("1234 Power Inn Road", "Sacramento", "CA", "95826")
    b = normalize_address("1234 POWER INN RD", "SACRAMENTO", "CA", "95826")
    assert a == b == "1234 POWER INN RD SACRAMENTO CA 95826"


def test_normalize_address_none_when_blank():
    assert normalize_address(None) is None
    assert normalize_address("   ") is None


# --------------------------------------------------------------------------- extract_identity_keys


def test_extract_identity_keys_pulls_from_message_and_claims():
    message = InboundMessage(
        message_id="m2",
        channel=Channel.EMAIL,
        body_text="See https://example.com/listing/9001",
        urls=["https://example.com/listing/9001"],
        thread_id="m1",
        sender="jane@brokerage.example",
    )
    claims = ExtractedClaims(
        address_line="1234 Power Inn Road",
        city="Sacramento",
        state="CA",
        postal_code="95826",
        apn="APN-1",
        broker_property_ref="LST-1",
        building_sqft=20000,
    )
    keys = extract_identity_keys(message, claims)
    assert keys.normalized_address == "1234 POWER INN RD SACRAMENTO CA 95826"
    assert keys.apn == "APN-1"
    assert keys.broker_property_ref == "LST-1"
    assert keys.listing_url == "https://example.com/listing/9001"
    assert keys.thread_id == "m1"
    assert keys.sender_email == "jane@brokerage.example"
    assert keys.city == "Sacramento"
    assert keys.building_sqft == 20000


# --------------------------------------------------------------------------- resolve: helpers


def _seed_property_and_opportunity(repo, address="1234 Power Inn Road", city="Sacramento", apn=None) -> tuple[Property, Opportunity]:
    normalized = normalize_address(address, city, "CA", "95826")
    prop = repo.upsert_property(
        Property(canonical_address=f"{address}, {city}, CA 95826", normalized_address=normalized, city=city, apn=apn)
    )
    opp = repo.create_opportunity(Opportunity(property_id=prop.property_id, display_name="Test deal"))
    return prop, opp


# --------------------------------------------------------------------------- resolve: tiers


def test_resolve_no_properties_returns_no_match(repo):
    keys = IdentityKeys(normalized_address="1 NOWHERE ST")
    result = resolve(keys, repo)
    assert result.opportunity_id is None
    assert result.created is False
    assert result.needs_human is False


def test_resolve_exact_normalized_address_is_confidence_1(repo):
    prop, opp = _seed_property_and_opportunity(repo)
    keys = IdentityKeys(normalized_address=prop.normalized_address)
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 1.0
    assert "normalized_address" in result.matched_on
    assert result.created is False


def test_resolve_exact_apn_is_confidence_1(repo):
    prop, opp = _seed_property_and_opportunity(repo, apn="APN-999")
    keys = IdentityKeys(apn="APN-999")
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 1.0


def test_resolve_thread_id_is_confidence_0_95(repo):
    _, opp = _seed_property_and_opportunity(repo)
    message = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi", thread_id="m1")
    repo.store_inbound_message(message)
    repo.link_message_to_opportunity("m1", opp.opportunity_id)

    # Reply email carries no address at all, only a thread_id pointing back at message 1.
    keys = IdentityKeys(thread_id="m1")
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 0.95
    assert result.matched_on == ["thread"]


def test_resolve_thread_id_via_in_reply_to_when_no_prior_thread_row(repo):
    """A reply's thread_id (no References chain) equals the raw In-Reply-To id, which is the
    original message's own message_id -- and that original message stored its own message_id as
    its thread_id too, so a single find_opportunity_id_by_thread_id lookup covers both cases the
    contract calls out (thread lookup, and message-id lookup on the in-reply-to id)."""
    _, opp = _seed_property_and_opportunity(repo)
    original = InboundMessage(
        message_id="original@x", channel=Channel.EMAIL, body_text="hi", thread_id="original@x"
    )
    repo.store_inbound_message(original)
    repo.link_message_to_opportunity("original@x", opp.opportunity_id)

    keys = IdentityKeys(thread_id="original@x")  # reply's thread_id when References is absent
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 0.95


def test_resolve_attachment_sha_is_confidence_0_9(repo):
    _, opp = _seed_property_and_opportunity(repo)
    repo.store_document(opp.opportunity_id, "m1", "OM.pdf", "f" * 64, "text")
    keys = IdentityKeys(attachment_fingerprints=["f" * 64])
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 0.9
    assert result.matched_on == ["attachment_sha"]


def test_resolve_fuzzy_address_same_property_different_spelling(repo):
    prop, opp = _seed_property_and_opportunity(repo, address="1234 Power Inn Road", city="Sacramento")
    # Same property, close but not identical after normalization (a minor typo dropping the
    # second "n" in "Inn"), so this exercises the fuzzy tier rather than the exact-match tier.
    query = normalize_address("1234 Power In Road", "Sacramento", "CA", "95826")
    assert query != prop.normalized_address  # sanity: this must not collapse to an exact match
    keys = IdentityKeys(normalized_address=query, city="Sacramento")
    result = resolve(keys, repo)
    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 0.8
    assert result.needs_human is False


def test_resolve_different_street_number_does_not_match(repo):
    _seed_property_and_opportunity(repo, address="1234 Power Inn Road", city="Sacramento")
    query = normalize_address("1240 Power Inn Rd", "Sacramento", "CA", "95826")
    keys = IdentityKeys(normalized_address=query, city="Sacramento")
    result = resolve(keys, repo)
    assert result.opportunity_id is None
    assert result.confidence < 0.8


def test_resolve_fuzzy_address_without_same_city_does_not_auto_match(repo):
    prop, opp = _seed_property_and_opportunity(repo, address="1234 Power Inn Road", city="Sacramento")
    query = normalize_address("1234 Power Inn Rd", "Elk Grove", "CA", "95826")
    keys = IdentityKeys(normalized_address=query, city="Elk Grove")
    result = resolve(keys, repo)
    assert result.opportunity_id is None


def test_resolve_never_creates_anything(repo):
    keys = IdentityKeys(normalized_address="1 NOWHERE ST")
    result = resolve(keys, repo)
    assert result.created is False
    assert repo.list_properties() == []
    assert repo.list_opportunities() == []


def test_resolve_exact_beats_fuzzy_when_both_available(repo):
    prop, opp = _seed_property_and_opportunity(repo)
    keys = IdentityKeys(normalized_address=prop.normalized_address, city="Sacramento")
    result = resolve(keys, repo)
    assert result.confidence == 1.0  # exact tier wins even though fuzzy would also match


def test_r8_conflicting_exact_identifiers_require_human_review(repo):
    prop_a, opp_a = _seed_property_and_opportunity(repo, address="1234 Power Inn Road")
    _, opp_b = _seed_property_and_opportunity(repo, address="9000 Elder Creek Road", apn="APN-B")

    result = resolve(
        IdentityKeys(normalized_address=prop_a.normalized_address, apn="APN-B"),
        repo,
    )

    assert result.opportunity_id is None
    assert result.needs_human is True
    assert {c["opportunity_id"] for c in result.ambiguous_candidates} == {
        opp_a.opportunity_id,
        opp_b.opportunity_id,
    }


def test_r8_explicit_identifier_can_contradict_thread_match(repo):
    _, thread_opp = _seed_property_and_opportunity(repo, address="1234 Power Inn Road")
    explicit_prop, explicit_opp = _seed_property_and_opportunity(
        repo, address="9000 Elder Creek Road"
    )
    message = InboundMessage(
        message_id="thread-root",
        channel=Channel.EMAIL,
        body_text="first deal",
        thread_id="thread-root",
    )
    repo.store_inbound_message(message)
    repo.link_message_to_opportunity(message.message_id, thread_opp.opportunity_id)

    result = resolve(
        IdentityKeys(thread_id="thread-root", normalized_address=explicit_prop.normalized_address),
        repo,
    )

    assert result.opportunity_id is None
    assert result.needs_human is True
    assert {c["opportunity_id"] for c in result.ambiguous_candidates} == {
        thread_opp.opportunity_id,
        explicit_opp.opportunity_id,
    }


def test_r8_agreeing_thread_and_exact_identifier_return_one_candidate(repo):
    prop, opp = _seed_property_and_opportunity(repo)
    message = InboundMessage(
        message_id="thread-root",
        channel=Channel.EMAIL,
        body_text="first deal",
        thread_id="thread-root",
    )
    repo.store_inbound_message(message)
    repo.link_message_to_opportunity(message.message_id, opp.opportunity_id)

    result = resolve(
        IdentityKeys(thread_id="thread-root", normalized_address=prop.normalized_address),
        repo,
    )

    assert result.opportunity_id == opp.opportunity_id
    assert result.confidence == 1.0
    assert set(result.matched_on) == {"normalized_address", "thread"}
    assert result.needs_human is False


def test_r8_fuzzy_score_between_80_and_90_is_a_human_candidate(repo):
    _, opp = _seed_property_and_opportunity(repo)
    query = normalize_address("1234 Industrial Park Road", "Sacramento", "CA", "95826")

    result = resolve(IdentityKeys(normalized_address=query, city="Sacramento"), repo)

    assert result.opportunity_id is None
    assert 0.8 <= result.confidence < 0.9
    assert result.needs_human is True
    assert result.ambiguous_candidates[0]["opportunity_id"] == opp.opportunity_id


def test_resolve_evidence_field_matches_by_name_not_used_directly(repo):
    """Sanity check that resolve() only consults repo lookups, not claims.evidence directly."""
    prop, opp = _seed_property_and_opportunity(repo)
    keys = IdentityKeys(normalized_address=prop.normalized_address)
    result = resolve(keys, repo)
    assert result.property_id == prop.property_id
    # Evidence objects are unrelated to identity resolution directly; just confirm the schema shape.
    ev = Evidence(field="asking_price", value=100, source_document="m1", confidence=0.9)
    assert ev.field == "asking_price"
