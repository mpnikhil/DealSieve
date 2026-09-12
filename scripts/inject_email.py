#!/usr/bin/env python3
"""Push one .eml file through the full DealSieve pipeline and print a readable outcome.

Usage: python scripts/inject_email.py path/to/file.eml   (equivalent to `dealsieve ingest`)
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from dealsieve.ingestion.email import parse_eml
from dealsieve.notifications import get_notifier
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import load_policy


def _fmt_money(value: Decimal | None) -> str:
    return f"${value:,.0f}" if value is not None else "—"


def _fmt_pct(value: Decimal | None) -> str:
    return f"{value * 100:.2f}%" if value is not None else "—"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: inject_email.py <file.eml>", file=sys.stderr)
        return 2

    path = Path(argv[0])
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1

    repo = Repo()
    repo.init_schema()
    policy = load_policy()
    notifier = get_notifier()

    message = parse_eml(path)
    outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier)

    print(f"Message:        {path.name}")
    print(f"Opportunity:    {outcome.opportunity_id or '(none)'}")

    opp = repo.get_opportunity(outcome.opportunity_id) if outcome.opportunity_id else None
    if opp is not None and opp.deal_number is not None:
        print(f"Deal #:         {opp.deal_number}")

    before = outcome.status_before.value if outcome.status_before else "(new)"
    after = outcome.status_after.value if outcome.status_after else "(unknown)"
    print(f"Status:         {before} -> {after}")

    run = repo.get_underwriting_run(outcome.run_id) if outcome.run_id else None
    if run is not None:
        print(f"Normalized cap: {_fmt_pct(run.normalized.normalized_cap_rate)}")
        print(f"DSCR:           {run.financing.dscr:.2f}x")
        print(f"Max viable:     {_fmt_money(run.viability.max_viable_price)}")
        print(f"Distance:       {_fmt_pct(run.viability.distance_pct)}")

    print(f"Human notified: {'yes' if outcome.notified_human else 'no'}")

    if outcome.notified_human and outcome.notification_id and outcome.opportunity_id:
        notifications = repo.list_notifications(opportunity_id=outcome.opportunity_id)
        match = next((n for n in notifications if n.notification_id == outcome.notification_id), None)
        if match is not None:
            print()
            print("--- alert ---")
            print(match.title)
            print()
            print(match.body)
            print("-------------")

    print()
    print(f"Summary: {outcome.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
