from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dealsieve.evidence.reconcile import DetectedChange
from dealsieve.identity.resolver import normalize_address
from dealsieve.schemas import (
    Actor,
    Attachment,
    Channel,
    ComparisonRow,
    ConstraintKind,
    EventType,
    Evidence,
    ExpenseClaims,
    ExpenseLine,
    FinancingResult,
    GateResult,
    InboundMessage,
    NormalizedEconomics,
    Notification,
    NotificationAction,
    Opportunity,
    OpportunityEvent,
    OpportunityStatus,
    OutboundDraft,
    Property,
    SkepticConcern,
    SkepticReport,
    StressResult,
    TenantClaim,
    UnderwritingResult,
    ViabilityFrontier,
    ViabilityPath,
    WorkingValues,
)

# --------------------------------------------------------------------------- helpers


def _working_values() -> WorkingValues:
    return WorkingValues(
        asking_price=Decimal("1550000"),
        gross_scheduled_income=Decimal("180000"),
        other_income=Decimal("500.50"),
        stated_vacancy_pct=Decimal("0.05"),
        stated_expenses=ExpenseClaims(
            property_tax=Decimal("15500"),
            insurance=Decimal("8400"),
            repairs_maintenance=Decimal("12000"),
            utilities=Decimal("11000"),
            cam_other=Decimal("7100"),
            total=Decimal("54000"),
        ),
        stated_noi=Decimal("126000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.19"),
        occupancy_pct=Decimal("1"),
        tenants=[
            TenantClaim(name="Acme Machining", suite="A", sqft=2500, annual_rent=Decimal("34200")),
            TenantClaim(name="Delta Logistics", suite="B", sqft=1800, annual_rent=Decimal("25200")),
        ],
        property_type="industrial",
        provenance={"asking_price": "ev_abc123"},
        conflicts=["asking_price: conflicting values 1550000 vs 1500000"],
    )


def _underwriting_result(opportunity_id: str, *, created_at: datetime | None = None) -> UnderwritingResult:
    wv = _working_values()
    normalized = NormalizedEconomics(
        gross_potential_rent=Decimal("180000"),
        other_income=Decimal("500.50"),
        vacancy_loss=Decimal("9000"),
        effective_gross_income=Decimal("171500.50"),
        expenses=[
            ExpenseLine(name="property_tax", broker=Decimal("15500"), normalized=Decimal("19375"), basis="1.25% of price"),
            ExpenseLine(name="management", broker=None, normalized=Decimal("8575.025"), basis="5% of EGI"),
        ],
        total_expenses=Decimal("50000.025"),
        noi=Decimal("121500.475"),
        broker_noi=Decimal("126000"),
        broker_cap_rate=Decimal("0.0813"),
        normalized_cap_rate=Decimal("0.0784"),
        price_per_sqft=Decimal("77.5"),
        noi_per_sqft=Decimal("6.075"),
    )
    financing = FinancingResult(
        purchase_price=Decimal("1550000"),
        closing_costs=Decimal("31000"),
        total_acquisition_cost=Decimal("1581000"),
        equity_deployed=Decimal("421000"),
        loan_amount=Decimal("1160000"),
        ltv=Decimal("0.748387"),
        interest_rate=Decimal("0.07"),
        amortization_years=25,
        monthly_debt_service=Decimal("8199.32"),
        annual_debt_service=Decimal("98391.84"),
        dscr=Decimal("1.05"),
        cash_flow_after_debt=Decimal("23108.635"),
        cash_on_cash=Decimal("0.054889"),
        year1_principal_paydown=Decimal("15234.11"),
    )
    gates = [
        GateResult(
            gate="tenant_count_min",
            kind=ConstraintKind.STRUCTURAL,
            description="tenant count >= 5",
            comparator=">=",
            threshold=5,
            actual=8,
            passed=True,
            price_dependent=False,
        ),
        GateResult(
            gate="min_normalized_cap_rate",
            kind=ConstraintKind.ECONOMIC,
            description="normalized cap rate >= 8.00%",
            comparator=">=",
            threshold=Decimal("0.08"),
            actual=Decimal("0.0784"),
            passed=False,
            price_dependent=True,
        ),
    ]
    viability = ViabilityFrontier(
        current_price=Decimal("1550000"),
        max_viable_price=Decimal("1300000.50"),
        distance_pct=Decimal("0.16126"),
        binding_constraints=["min_normalized_cap_rate", "min_base_dscr"],
        paths=[
            ViabilityPath(
                variable="purchase_price",
                current_value=Decimal("1550000"),
                required_value=Decimal("1300000.50"),
                description="lower the purchase price",
            )
        ],
        structural_failures=[],
    )
    stress = [
        StressResult(
            scenario="vacancy_20pct", noi=Decimal("110000"), dscr=Decimal("0.95"),
            cash_flow_after_debt=Decimal("11608.16"), covers_debt=False,
        )
    ]
    comparison = [
        ComparisonRow(metric="NOI", broker="$126,000", dealsieve="$121,500", note=None),
    ]
    kwargs = dict(
        opportunity_id=opportunity_id,
        policy_version="policy_v1",
        trigger_event_id="evt_trigger1",
        inputs=wv,
        normalized=normalized,
        financing=financing,
        stress=stress,
        gates=gates,
        status=OpportunityStatus.WATCH,
        viability=viability,
        comparison=comparison,
        failure_summary="Fails on valuation: normalized cap 7.84% < 8.00%, DSCR 1.05x < 1.35x",
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    return UnderwritingResult(**kwargs)


def _property(repo, address="1234 Power Inn Road", city="Sacramento", state="CA", zip_="95826", apn=None) -> Property:
    normalized = normalize_address(address, city, state, zip_)
    prop = Property(
        canonical_address=f"{address}, {city}, {state} {zip_}",
        normalized_address=normalized,
        city=city,
        state=state,
        postal_code=zip_,
        apn=apn,
        building_sqft=20000,
        property_type="industrial",
    )
    return repo.upsert_property(prop)


def _opportunity(repo, prop: Property, display_name="8-unit small-bay industrial", status=OpportunityStatus.WATCH) -> Opportunity:
    opp = Opportunity(
        property_id=prop.property_id,
        display_name=display_name,
        status=status,
        current_asking_price=Decimal("1550000"),
        broker_email="jane@brokerage.example",
        broker_name="Jane Broker",
        broker_property_ref="LST-9001",
        listing_url="https://example.com/listing/9001",
    )
    return repo.create_opportunity(opp)


# --------------------------------------------------------------------------- round trips


def test_underwriting_run_round_trip_with_decimal_and_datetime(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    created_at = datetime(2026, 9, 12, 10, 30, 0, 123456, tzinfo=UTC)
    run = _underwriting_result(opp.opportunity_id, created_at=created_at)

    repo.store_underwriting_run(run)
    reloaded = repo.get_underwriting_run(run.run_id)

    assert reloaded == run
    assert reloaded.created_at == created_at
    assert isinstance(reloaded.financing.dscr, Decimal)
    assert reloaded.financing.dscr == Decimal("1.05")
    assert reloaded.normalized.total_expenses == Decimal("50000.025")


def test_inbound_message_round_trip_with_attachments(repo):
    msg = InboundMessage(
        message_id="om-2026-0912-power-inn@brokerage.example",
        channel=Channel.EMAIL,
        received_at=datetime(2026, 9, 12, 17, 0, 0, tzinfo=UTC),
        sender="jane@brokerage.example",
        sender_name="Jane Broker",
        subject="Off-market: 8-unit small-bay industrial",
        body_text="Asking $1,550,000, NOI $126,000.",
        attachments=[
            Attachment(
                filename="Power_Inn_OM.md",
                content_type="text/markdown",
                sha256="a" * 64,
                size_bytes=1234,
                text="# Power Inn OM",
            )
        ],
        urls=["https://example.com/listing/9001"],
        thread_id="om-2026-0912-power-inn@brokerage.example",
    )
    repo.store_inbound_message(msg)
    reloaded = repo.get_message(msg.message_id)
    assert reloaded == msg
    assert repo.message_exists(msg.message_id) is True
    assert repo.message_exists("nonexistent") is False


def test_store_inbound_message_is_idempotent_and_preserves_link(repo):
    msg = InboundMessage(message_id="m1", channel=Channel.MANUAL, body_text="hello")
    repo.store_inbound_message(msg)
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    repo.link_message_to_opportunity("m1", opp.opportunity_id)

    # Re-storing the same message must not wipe out the opportunity link.
    repo.store_inbound_message(msg)
    assert repo.find_opportunity_id_by_message_id("m1") == opp.opportunity_id


def test_evidence_round_trip(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    evidence = [
        Evidence(
            field="asking_price",
            value=1550000,
            source_document="om-2026-0912-power-inn@brokerage.example",
            location="email body",
            quote="asking $1,550,000",
            source_timestamp=datetime(2026, 9, 12, 17, 0, 0, tzinfo=UTC),
            confidence=0.95,
        ),
        Evidence(field="asking_price", value=1500000, source_document="Power_Inn_OM.md", confidence=0.6),
    ]
    repo.store_evidence(opp.opportunity_id, evidence)
    reloaded = repo.list_evidence(opp.opportunity_id)
    assert len(reloaded) == 2
    assert {e.evidence_id for e in reloaded} == {e.evidence_id for e in evidence}
    assert reloaded[0].source_timestamp == evidence[0].source_timestamp or reloaded[1].source_timestamp == evidence[0].source_timestamp


def test_skeptic_report_round_trip(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    report = SkepticReport(
        opportunity_id=opp.opportunity_id,
        run_id="run_1",
        verdict="proceed_with_questions",
        summary="Looks attractive but several gaps remain.",
        concerns=[
            SkepticConcern(
                topic="roof age",
                severity="medium",
                why_it_matters="Unknown capex exposure",
                evidence_status="missing",
                question_for_broker="What is the roof age?",
            )
        ],
    )
    repo.store_skeptic_report(report)
    reloaded = repo.list_skeptic_reports(opp.opportunity_id)
    assert reloaded == [report]


def test_outbound_draft_round_trip_and_update(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    draft = OutboundDraft(
        opportunity_id=opp.opportunity_id,
        to_email="jane@brokerage.example",
        subject="Re: Off-market: 8-unit small-bay industrial",
        body="A few questions...",
        questions=["What is the roof age?"],
    )
    repo.store_draft(draft)
    reloaded = repo.get_draft(draft.draft_id)
    assert reloaded == draft

    approved = draft.model_copy(update={"status": "approved", "decided_at": datetime.now(UTC)})
    repo.update_draft(approved)
    reloaded2 = repo.get_draft(draft.draft_id)
    assert reloaded2.status == "approved"
    assert reloaded2.decided_at is not None

    assert repo.list_drafts(status="approved", opportunity_id=opp.opportunity_id) == [reloaded2]
    assert repo.list_drafts(status="pending") == []


def test_notification_round_trip(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    notification = Notification(
        opportunity_id=opp.opportunity_id,
        kind="threshold_crossed",
        channel=Channel.TELEGRAM,
        title="DEAL #101 JUST BECAME INVESTABLE",
        body="Price $1,550,000 -> $1,250,000",
        actions=[NotificationAction(label="Review", action="review")],
        delivered=True,
        delivery_ref="tg_msg_1",
    )
    repo.store_notification(notification)
    reloaded = repo.list_notifications(opp.opportunity_id)
    assert reloaded == [notification]
    assert repo.list_notifications() == [notification]


# --------------------------------------------------------------------------- behavior


def test_create_opportunity_assigns_sequential_deal_numbers_starting_at_101(repo):
    prop = _property(repo)
    opp1 = _opportunity(repo, prop, display_name="Deal One")
    prop2 = _property(repo, address="500 Elder Creek Rd")
    opp2 = _opportunity(repo, prop2, display_name="Deal Two")
    assert opp1.deal_number == 101
    assert opp2.deal_number == 102
    assert repo.get_opportunity_by_deal_number(101).opportunity_id == opp1.opportunity_id
    assert repo.get_opportunity_by_deal_number(102).opportunity_id == opp2.opportunity_id


def test_save_opportunity_updates_derived_state_and_updated_at(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    original_updated_at = opp.updated_at

    updated = opp.model_copy(
        update={
            "status": OpportunityStatus.REVIEW,
            "previous_status": OpportunityStatus.WATCH,
            "working_values": _working_values(),
        }
    )
    saved = repo.save_opportunity(updated)

    assert saved.status == OpportunityStatus.REVIEW
    assert saved.working_values == _working_values()
    assert saved.updated_at >= original_updated_at

    reloaded = repo.get_opportunity(opp.opportunity_id)
    assert reloaded.status == OpportunityStatus.REVIEW
    assert reloaded.working_values == _working_values()


def test_list_opportunities_filters_by_status(repo):
    prop = _property(repo)
    watch_opp = _opportunity(repo, prop, display_name="Watch deal", status=OpportunityStatus.WATCH)
    prop2 = _property(repo, address="9 Other St")
    dead_opp = _opportunity(repo, prop2, display_name="Dead deal", status=OpportunityStatus.DEAD)

    watch_only = repo.list_opportunities(OpportunityStatus.WATCH)
    assert [o.opportunity_id for o in watch_only] == [watch_opp.opportunity_id]

    all_opps = repo.list_opportunities()
    assert {o.opportunity_id for o in all_opps} == {watch_opp.opportunity_id, dead_opp.opportunity_id}


def test_property_upsert_dedups_by_normalized_address(repo):
    first = _property(repo, apn=None)
    second = _property(repo, apn="APN-999")  # same address, different object/apn
    assert first.property_id == second.property_id
    assert repo.get_property(first.property_id).apn == "APN-999"
    assert len(repo.list_properties()) == 1


def test_append_event_assigns_monotonic_seq_and_history_is_immutable(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)

    e1 = repo.append_event(
        OpportunityEvent(opportunity_id=opp.opportunity_id, type=EventType.DEAL_DISCOVERED, summary="Discovered")
    )
    e2 = repo.append_event(
        OpportunityEvent(opportunity_id=opp.opportunity_id, type=EventType.CLAIMS_EXTRACTED, summary="Claims in")
    )
    e3 = repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=EventType.ASKING_PRICE_CHANGED,
            summary="Price changed",
            payload={"from": 1550000.0, "to": 1250000.0, "pct": -19.35},
            actor=Actor.SYSTEM,
        )
    )
    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)

    events = repo.list_events(opp.opportunity_id)
    assert [e.seq for e in events] == [1, 2, 3]
    assert events == [e1, e2, e3]

    # A second opportunity gets its own seq counter starting at 1.
    prop2 = _property(repo, address="42 Other Ave")
    opp2 = _opportunity(repo, prop2, display_name="Second deal")
    e_other = repo.append_event(
        OpportunityEvent(opportunity_id=opp2.opportunity_id, type=EventType.DEAL_DISCOVERED, summary="Discovered 2")
    )
    assert e_other.seq == 1


def test_underwriting_runs_are_appended_not_overwritten(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    run1 = _underwriting_result(opp.opportunity_id)
    run2 = _underwriting_result(opp.opportunity_id)
    repo.store_underwriting_run(run1)
    repo.store_underwriting_run(run2)

    runs = repo.list_underwriting_runs(opp.opportunity_id)
    assert len(runs) == 2
    assert repo.get_underwriting_run(run1.run_id) == run1
    assert repo.get_underwriting_run(run2.run_id) == run2


def test_watchlist_orders_review_near_watch_then_by_distance(repo):
    def make(status, distance, name):
        prop = _property(repo, address=f"{name} Ave")
        opp = _opportunity(repo, prop, display_name=name, status=status)
        viability = ViabilityFrontier(current_price=Decimal("1000000"), max_viable_price=Decimal("900000"), distance_pct=distance)
        saved = repo.save_opportunity(opp.model_copy(update={"viability": viability}))
        return saved

    make(OpportunityStatus.WATCH, Decimal("0.5"), "WatchFar")
    make(OpportunityStatus.WATCH, Decimal("0.1"), "WatchClose")
    make(OpportunityStatus.REVIEW, Decimal("-0.02"), "ReviewDeal")
    make(OpportunityStatus.NEAR, Decimal("0.03"), "NearDeal")
    make(OpportunityStatus.DEAD, None, "DeadDeal")  # excluded

    items = repo.watchlist()
    names = [i.display_name for i in items]
    assert names == ["ReviewDeal", "NearDeal", "WatchClose", "WatchFar"]
    assert "DeadDeal" not in names


def test_watchlist_puts_missing_distance_last_within_status(repo):
    prop1 = _property(repo, address="1 No Distance Ave")
    opp1 = _opportunity(repo, prop1, display_name="NoDistance", status=OpportunityStatus.WATCH)
    repo.save_opportunity(opp1.model_copy(update={"viability": ViabilityFrontier(current_price=Decimal("1"))}))

    prop2 = _property(repo, address="2 Has Distance Ave")
    opp2 = _opportunity(repo, prop2, display_name="HasDistance", status=OpportunityStatus.WATCH)
    repo.save_opportunity(
        opp2.model_copy(
            update={"viability": ViabilityFrontier(current_price=Decimal("1"), distance_pct=Decimal("0.3"))}
        )
    )

    items = repo.watchlist()
    assert [i.display_name for i in items] == ["HasDistance", "NoDistance"]


def test_dashboard_stats_counts_statuses_and_7day_events(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop, status=OpportunityStatus.WATCH)
    prop2 = _property(repo, address="2 Dead Ave")
    _opportunity(repo, prop2, status=OpportunityStatus.DEAD)
    prop3 = _property(repo, address="3 Review Ave")
    opp3 = _opportunity(repo, prop3, status=OpportunityStatus.REVIEW)

    repo.append_event(
        OpportunityEvent(opportunity_id=opp.opportunity_id, type=EventType.ASKING_PRICE_CHANGED, summary="x")
    )
    repo.append_event(
        OpportunityEvent(
            opportunity_id=opp3.opportunity_id,
            type=EventType.STATUS_CHANGED,
            summary="->REVIEW",
            payload={"from": "WATCH", "to": "REVIEW"},
        )
    )
    repo.append_event(
        OpportunityEvent(opportunity_id=opp3.opportunity_id, type=EventType.HUMAN_NOTIFIED, summary="notified")
    )
    # An old event (outside the 7-day window) must not be counted.
    old_event = OpportunityEvent(
        opportunity_id=opp.opportunity_id,
        type=EventType.NOI_CHANGED,
        summary="old",
        occurred_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    repo.append_event(old_event)

    stats = repo.dashboard_stats("policy_v1")
    assert stats.encountered == 3
    assert stats.dead == 1
    assert stats.watch == 1
    assert stats.review == 1
    assert stats.near == 0
    assert stats.conditions_changed_7d == 1
    assert stats.threshold_crossings_7d == 1
    assert stats.human_interruptions_7d == 1
    assert stats.policy_version == "policy_v1"


def test_opportunity_detail_accepts_id_or_deal_number(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    run = _underwriting_result(opp.opportunity_id)
    repo.store_underwriting_run(run)
    saved = repo.save_opportunity(opp.model_copy(update={"latest_run_id": run.run_id}))
    repo.append_event(
        OpportunityEvent(opportunity_id=opp.opportunity_id, type=EventType.DEAL_DISCOVERED, summary="Discovered")
    )

    by_id = repo.opportunity_detail(opp.opportunity_id)
    by_deal_number = repo.opportunity_detail(str(saved.deal_number))

    assert by_id is not None and by_deal_number is not None
    assert by_id.opportunity.opportunity_id == by_deal_number.opportunity.opportunity_id == opp.opportunity_id
    assert by_id.property.property_id == prop.property_id
    assert by_id.latest_run == run
    assert len(by_id.runs) == 1
    assert len(by_id.events) == 1

    assert repo.opportunity_detail("does-not-exist") is None
    assert repo.opportunity_detail("999999") is None


def test_record_policy_version_is_idempotent(repo):
    repo.record_policy_version("policy_v1", "Base policy", "name: base\n")
    repo.record_policy_version("policy_v1", "Base policy", "name: base\n")  # must not raise


def test_store_document_and_find_by_attachment_sha(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    repo.store_document(opp.opportunity_id, "msg1", "Power_Inn_OM.md", "deadbeef" * 8, "# OM text")
    assert repo.find_opportunity_id_by_attachment_sha("deadbeef" * 8) == opp.opportunity_id
    assert repo.find_opportunity_id_by_attachment_sha("nonexistent") is None


# --------------------------------------------------------------------------- identity lookups


def test_find_opportunity_id_by_message_id_and_thread_id(repo):
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    msg = InboundMessage(message_id="m1", channel=Channel.EMAIL, body_text="hi", thread_id="m1")
    repo.store_inbound_message(msg)
    assert repo.find_opportunity_id_by_message_id("m1") is None
    repo.link_message_to_opportunity("m1", opp.opportunity_id)
    assert repo.find_opportunity_id_by_message_id("m1") == opp.opportunity_id
    assert repo.find_opportunity_id_by_thread_id("m1") == opp.opportunity_id
    assert repo.find_opportunity_id_by_thread_id("does-not-exist") is None


def test_find_opportunity_ids_by_keys(repo):
    prop = _property(repo, apn="APN-123")
    opp = _opportunity(repo, prop)

    found = repo.find_opportunity_ids_by_keys(
        normalized_address=prop.normalized_address,
        apn="APN-123",
        listing_url="https://example.com/listing/9001",
        broker_property_ref="LST-9001",
    )
    assert found == {
        "normalized_address": opp.opportunity_id,
        "apn": opp.opportunity_id,
        "listing_url": opp.opportunity_id,
        "broker_property_ref": opp.opportunity_id,
    }

    empty = repo.find_opportunity_ids_by_keys(normalized_address="9999 NOWHERE ST")
    assert empty == {}


def test_reconcile_changes_can_be_turned_into_events(repo):
    """Smoke-test that DetectedChange objects from evidence.reconcile map cleanly onto events."""
    prop = _property(repo)
    opp = _opportunity(repo, prop)
    change = DetectedChange(
        type=EventType.ASKING_PRICE_CHANGED,
        summary="Asking price $1,550,000 -> $1,250,000 (-19.4%)",
        payload={"from": 1550000.0, "to": 1250000.0, "pct": -19.4},
    )
    event = repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=change.type,
            summary=change.summary,
            payload=change.payload,
        )
    )
    reloaded = repo.list_events(opp.opportunity_id)[0]
    assert reloaded == event
    assert reloaded.payload["pct"] == pytest.approx(-19.4)
