#!/usr/bin/env python3
"""Reset the database and load demo data.

1. Fixtures 03 (structural, DEAD) and 04 (obvious economic failure, WATCH) through the real pipeline with
   the scripted model backend -- these exercise ingestion, identity, reconciliation and underwriting
   end to end.
2. ~10 synthetic Sacramento-area opportunities created directly with Repo + run_underwriting: mostly
   DEAD/WATCH, exactly one NEAR, zero REVIEW. The live demo (fixture 01 then 02, run interactively or via
   `dealsieve ingest`) supplies the only REVIEW -- this script never seeds one. Each synthetic opportunity
   gets DEAL_DISCOVERED, UNDERWRITING_COMPLETED and STATUS_CHANGED events, backdated over the last week, so
   the timeline and 7-day stats look real.

Usage: python scripts/seed_demo.py   (or: dealsieve seed)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from dealsieve.identity.resolver import normalize_address
from dealsieve.notifications.console import ConsoleNotifier
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import InvestmentPolicy, load_policy
from dealsieve.schemas import (
    Actor,
    EventType,
    Opportunity,
    OpportunityEvent,
    OpportunityStatus,
    Property,
    WorkingValues,
    now_utc,
)
from dealsieve.underwriting.engine import run_underwriting

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


@dataclass(frozen=True)
class SyntheticDeal:
    display_name: str
    address_line: str
    city: str
    state: str
    postal_code: str
    gross_scheduled_income: Decimal
    asking_price: Decimal
    building_sqft: int
    tenant_count: int
    largest_tenant_pct: Decimal
    stated_noi: Decimal
    days_ago: int
    property_type: str = "small_bay_industrial"


# Numbers checked against the real underwriting engine at config/investment_policy.yaml:
# 4 DEAD (structural), 5 WATCH, 1 NEAR, 0 REVIEW.
SYNTHETIC_DEALS: list[SyntheticDeal] = [
    SyntheticDeal(
        display_name="Single-tenant distribution building, Fruitridge Road, Sacramento",
        address_line="3400 Fruitridge Road",
        city="Sacramento",
        state="CA",
        postal_code="95820",
        gross_scheduled_income=Decimal("140000"),
        asking_price=Decimal("900000"),
        building_sqft=10000,
        tenant_count=1,
        largest_tenant_pct=Decimal("1.00"),
        stated_noi=Decimal("118000"),
        days_ago=6,
    ),
    SyntheticDeal(
        display_name="3-tenant industrial building, Elder Creek Road, Sacramento",
        address_line="5250 Elder Creek Road",
        city="Sacramento",
        state="CA",
        postal_code="95824",
        gross_scheduled_income=Decimal("160000"),
        asking_price=Decimal("1100000"),
        building_sqft=12000,
        tenant_count=3,
        largest_tenant_pct=Decimal("0.55"),
        stated_noi=Decimal("132000"),
        days_ago=6,
    ),
    SyntheticDeal(
        display_name="6-unit small-bay industrial, Kiefer Boulevard, Sacramento",
        address_line="8801 Kiefer Boulevard",
        city="Sacramento",
        state="CA",
        postal_code="95826",
        gross_scheduled_income=Decimal("150000"),
        asking_price=Decimal("1900000"),
        building_sqft=15000,
        tenant_count=6,
        largest_tenant_pct=Decimal("0.20"),
        stated_noi=Decimal("108000"),
        days_ago=5,
    ),
    SyntheticDeal(
        display_name="7-unit small-bay industrial, Longview Drive, Rancho Cordova",
        address_line="3100 Longview Drive",
        city="Rancho Cordova",
        state="CA",
        postal_code="95742",
        gross_scheduled_income=Decimal("170000"),
        asking_price=Decimal("2000000"),
        building_sqft=16000,
        tenant_count=7,
        largest_tenant_pct=Decimal("0.18"),
        stated_noi=Decimal("128000"),
        days_ago=5,
    ),
    SyntheticDeal(
        display_name="8-unit small-bay industrial, National Drive, Sacramento",
        address_line="1801 National Drive",
        city="Sacramento",
        state="CA",
        postal_code="95834",
        gross_scheduled_income=Decimal("200000"),
        asking_price=Decimal("1900000"),
        building_sqft=18000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.15"),
        stated_noi=Decimal("155000"),
        days_ago=4,
    ),
    SyntheticDeal(
        display_name="2-tenant flex building, Harbor Boulevard, West Sacramento",
        address_line="1450 Harbor Boulevard",
        city="West Sacramento",
        state="CA",
        postal_code="95691",
        gross_scheduled_income=Decimal("120000"),
        asking_price=Decimal("800000"),
        building_sqft=9000,
        tenant_count=2,
        largest_tenant_pct=Decimal("0.60"),
        stated_noi=Decimal("98000"),
        days_ago=4,
    ),
    SyntheticDeal(
        display_name="9-unit small-bay industrial, Micron Avenue, Sacramento",
        address_line="9600 Micron Avenue",
        city="Sacramento",
        state="CA",
        postal_code="95827",
        gross_scheduled_income=Decimal("210000"),
        asking_price=Decimal("1950000"),
        building_sqft=19000,
        tenant_count=9,
        largest_tenant_pct=Decimal("0.12"),
        stated_noi=Decimal("162000"),
        days_ago=3,
    ),
    SyntheticDeal(
        display_name="8-unit small-bay industrial, Duckhorn Drive, Sacramento",
        address_line="4750 Duckhorn Drive",
        city="Sacramento",
        state="CA",
        postal_code="95834",
        gross_scheduled_income=Decimal("230000"),
        asking_price=Decimal("1650000"),
        building_sqft=20000,
        tenant_count=8,
        largest_tenant_pct=Decimal("0.16"),
        stated_noi=Decimal("182000"),
        days_ago=2,
    ),
    SyntheticDeal(
        display_name="4-tenant industrial building, Bell Avenue, Sacramento",
        address_line="2200 Bell Avenue",
        city="Sacramento",
        state="CA",
        postal_code="95838",
        gross_scheduled_income=Decimal("130000"),
        asking_price=Decimal("950000"),
        building_sqft=11000,
        tenant_count=4,
        largest_tenant_pct=Decimal("0.30"),
        stated_noi=Decimal("105000"),
        days_ago=2,
    ),
    SyntheticDeal(
        display_name="6-unit small-bay industrial, Mericrest Way, Sacramento",
        address_line="6100 Mericrest Way",
        city="Sacramento",
        state="CA",
        postal_code="95828",
        gross_scheduled_income=Decimal("110000"),
        asking_price=Decimal("1200000"),
        building_sqft=10500,
        tenant_count=6,
        largest_tenant_pct=Decimal("0.22"),
        stated_noi=Decimal("82000"),
        days_ago=1,
    ),
]


def _run_fixture(repo: Repo, policy: InvestmentPolicy, name: str) -> None:
    """Run one .eml fixture through the real pipeline with the scripted model backend."""
    from dealsieve.ingestion.email import parse_eml

    eml_path = FIXTURES / "emails" / f"{name}.eml"
    script_path = FIXTURES / "scripted" / f"{name}.json"
    if not eml_path.is_file() or not script_path.is_file():
        print(f"  skipping {name}: fixture not present yet ({eml_path.name} / {script_path.name})")
        return

    try:
        message = parse_eml(eml_path)
        outcome = process_inbound(
            message, repo=repo, policy=policy, notifier=ConsoleNotifier(), script=str(script_path)
        )
    except NotImplementedError as exc:
        print(f"  skipping {name}: {exc}")
        return
    print(f"  {name}: {outcome.status_before} -> {outcome.status_after}  ({outcome.summary})")


def _seed_synthetic(repo: Repo, policy: InvestmentPolicy, deal: SyntheticDeal) -> None:
    normalized = (
        normalize_address(deal.address_line, deal.city, deal.state, deal.postal_code) or deal.address_line
    )

    prop = repo.upsert_property(
        Property(
            canonical_address=f"{deal.address_line}, {deal.city}, {deal.state} {deal.postal_code}",
            normalized_address=normalized,
            city=deal.city,
            state=deal.state,
            postal_code=deal.postal_code,
            building_sqft=deal.building_sqft,
            property_type=deal.property_type,
        )
    )

    working_values = WorkingValues(
        asking_price=deal.asking_price,
        gross_scheduled_income=deal.gross_scheduled_income,
        stated_noi=deal.stated_noi,
        building_sqft=deal.building_sqft,
        tenant_count=deal.tenant_count,
        largest_tenant_pct=deal.largest_tenant_pct,
        property_type=deal.property_type,
    )

    opp = repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name=deal.display_name,
            status=OpportunityStatus.NEW,
            current_asking_price=deal.asking_price,
            working_values=working_values,
        )
    )

    occurred_at = now_utc() - timedelta(days=deal.days_ago, hours=deal.tenant_count % 5, minutes=10)

    discovered = repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=EventType.DEAL_DISCOVERED,
            actor=Actor.AGENT,
            occurred_at=occurred_at,
            summary=f"Discovered {deal.display_name}.",
            payload={"address": deal.address_line, "city": deal.city},
        )
    )

    run = run_underwriting(
        working_values, policy, opportunity_id=opp.opportunity_id, trigger_event_id=discovered.event_id
    )
    repo.store_underwriting_run(run)
    repo.record_policy_version(
        policy.policy_version, policy.name, Path(policy.source_path).read_text(encoding="utf-8")
    )

    max_viable = run.viability.max_viable_price
    repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=EventType.UNDERWRITING_COMPLETED,
            actor=Actor.SYSTEM,
            occurred_at=occurred_at + timedelta(minutes=1),
            summary=run.failure_summary,
            payload={
                "run_id": run.run_id,
                "status": run.status.value,
                "normalized_cap": float(run.normalized.normalized_cap_rate),
                "dscr": float(run.financing.dscr),
                "max_viable_price": float(max_viable) if max_viable is not None else None,
                "failure_summary": run.failure_summary,
            },
        )
    )

    repo.append_event(
        OpportunityEvent(
            opportunity_id=opp.opportunity_id,
            type=EventType.STATUS_CHANGED,
            actor=Actor.SYSTEM,
            occurred_at=occurred_at + timedelta(minutes=2),
            summary=f"Status changed {OpportunityStatus.NEW.value} -> {run.status.value}.",
            payload={"from": OpportunityStatus.NEW.value, "to": run.status.value},
        )
    )

    updated = opp.model_copy(
        update={
            "status": run.status,
            "previous_status": OpportunityStatus.NEW,
            "working_values": working_values,
            "latest_run_id": run.run_id,
            "viability": run.viability,
            "reason_summary": run.failure_summary,
            "human_attention_required": False,
        }
    )
    repo.save_opportunity(updated)
    print(f"  #{updated.deal_number}: {deal.display_name} -> {run.status.value}")


def main() -> int:
    from scripts.reset_db import main as reset_main

    print("Resetting database...")
    reset_main()

    db_path = os.environ.get("DEALSIEVE_DB_PATH", "data/dealsieve.db")
    repo = Repo(db_path)
    repo.init_schema()
    policy = load_policy()

    print("Loading fixtures 03 and 04 through the full pipeline (scripted backend)...")
    previous_backend = os.environ.get("DEALSIEVE_MODEL_BACKEND")
    os.environ["DEALSIEVE_MODEL_BACKEND"] = "scripted"
    try:
        _run_fixture(repo, policy, "03_structural_single_tenant")
        _run_fixture(repo, policy, "04_obvious_economic_failure")
    finally:
        if previous_backend is None:
            os.environ.pop("DEALSIEVE_MODEL_BACKEND", None)
        else:
            os.environ["DEALSIEVE_MODEL_BACKEND"] = previous_backend

    print(f"Seeding {len(SYNTHETIC_DEALS)} synthetic Sacramento-area opportunities...")
    for deal in SYNTHETIC_DEALS:
        _seed_synthetic(repo, policy, deal)

    stats = repo.dashboard_stats(policy.policy_version)
    print()
    print(
        f"Done. encountered={stats.encountered} dead={stats.dead} watch={stats.watch} "
        f"near={stats.near} review={stats.review}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
