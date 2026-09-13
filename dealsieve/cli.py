"""`dealsieve` command-line entry point (argparse). Declared in pyproject as `dealsieve = "dealsieve.cli:main"`.

Subcommands: ingest <file.eml|.txt>, seed, reset, serve [--port], telegram-bot, status [deal#], retry.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ensure_repo_root_importable() -> None:
    """`scripts/` sits next to `dealsieve/`, not inside it; make sure it's importable regardless of cwd."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _cmd_ingest(args: argparse.Namespace) -> int:
    from dealsieve.ingestion.email import parse_eml
    from dealsieve.ingestion.text import from_text
    from dealsieve.models.backend import default_script_for
    from dealsieve.notifications import get_notifier
    from dealsieve.persistence import Repo
    from dealsieve.pipeline import process_inbound
    from dealsieve.policy import load_policy

    path = Path(args.file)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 1

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    policy = load_policy(args.policy) if args.policy else load_policy()
    notifier = get_notifier()

    if path.suffix.lower() == ".eml":
        message = parse_eml(path)
    else:
        message = from_text(path.read_text(encoding="utf-8"))

    script = default_script_for(path)
    outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier, script=script)
    print(outcome.model_dump_json(indent=2))
    return 0


def _cmd_seed(args: argparse.Namespace) -> int:
    _ensure_repo_root_importable()
    from scripts.seed_demo import main as seed_main

    return seed_main() or 0


def _cmd_reset(args: argparse.Namespace) -> int:
    _ensure_repo_root_importable()
    from scripts.reset_db import main as reset_main

    return reset_main() or 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("dealsieve.api.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_telegram_bot(args: argparse.Namespace) -> int:
    from dealsieve.ingestion.telegram import run_bot

    try:
        run_bot()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _fmt_money(value: object) -> str:
    try:
        return f"${value:,.0f}"  # type: ignore[str-format]
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: object) -> str:
    try:
        return f"{value * 100:.2f}%"  # type: ignore[operator]
    except TypeError:
        return str(value)


def _cmd_status(args: argparse.Namespace) -> int:
    from dealsieve.persistence import Repo
    from dealsieve.policy import load_policy

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    policy = load_policy(args.policy) if args.policy else load_policy()

    if args.deal is not None:
        detail = repo.opportunity_detail(str(args.deal))
        if detail is None:
            print(f"No opportunity for deal #{args.deal}")
            return 1
        opp = detail.opportunity
        print(f"Deal #{opp.deal_number}: {opp.display_name}")
        print(f"  status:  {opp.status.value}")
        if opp.current_asking_price is not None:
            print(f"  asking:  {_fmt_money(opp.current_asking_price)}")
        if opp.viability is not None:
            if opp.viability.max_viable_price is not None:
                print(f"  max viable: {_fmt_money(opp.viability.max_viable_price)}")
            if opp.viability.distance_pct is not None:
                print(f"  distance:   {_fmt_pct(opp.viability.distance_pct)}")
        if opp.reason_summary:
            print(f"  reason:  {opp.reason_summary}")
        print(f"  human attention required: {opp.human_attention_required}")
        return 0

    stats = repo.dashboard_stats(policy.policy_version)
    print(f"Encountered: {stats.encountered}")
    print(f"  DEAD:   {stats.dead}")
    print(f"  WATCH:  {stats.watch}")
    print(f"  NEAR:   {stats.near}")
    print(f"  REVIEW: {stats.review}")
    print(
        f"7-day: {stats.conditions_changed_7d} conditions changed, "
        f"{stats.threshold_crossings_7d} threshold crossings, "
        f"{stats.human_interruptions_7d} human interruptions"
    )
    return 0


def _cmd_followup(args: argparse.Namespace) -> int:
    from dealsieve.diligence import run_follow_ups
    from dealsieve.notifications import get_notifier
    from dealsieve.outbound import get_outbox
    from dealsieve.persistence import Repo
    from dealsieve.policy import load_policy

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    policy = load_policy(args.policy) if args.policy else load_policy()
    as_of = None
    if args.as_of:
        try:
            parsed = datetime.fromisoformat(args.as_of)
            as_of = datetime.combine(parsed.date(), time.max, tzinfo=UTC) if "T" not in args.as_of else parsed
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=UTC)
        except ValueError:
            print("error: --as-of must be YYYY-MM-DD or an ISO-8601 datetime", file=sys.stderr)
            return 2
    report = run_follow_ups(
        repo=repo,
        policy=policy,
        outbox=get_outbox(policy),
        notifier=get_notifier(),
        as_of=as_of,
    )
    print(json.dumps(report.model_dump(mode="json"), default=str, indent=2))
    return 0


def _cmd_outbox(args: argparse.Namespace) -> int:
    from dealsieve.persistence import Repo

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    sent = repo.list_drafts(status="sent")
    if not sent:
        print("No sent broker messages.")
        return 0
    for draft in sent:
        timestamp = draft.sent_at.isoformat() if draft.sent_at else "unknown time"
        recipient = draft.to_email or "unknown recipient"
        print(f"{timestamp}  {draft.kind:<19}  {recipient}  {draft.subject}  [{draft.delivery_ref or '-'}]")
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    from dealsieve.memory import get_memory_store
    from dealsieve.persistence import Repo

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    store = get_memory_store(repo)

    namespace = args.namespace or ""
    if "/" not in namespace and namespace in {"investor", "broker"}:
        namespace = f"{namespace}/*"

    if args.q:
        namespaces = [namespace] if namespace else ["investor/*", "broker/*"]
        hits = store.recall(args.q, namespaces=namespaces, limit=args.limit)
        if not hits:
            print("No memories matched.")
            return 0
        for hit in hits:
            print(f"[{hit.score:.2f}] {hit.namespace}  {hit.created_at.isoformat()}")
            print(f"    {hit.text}")
        return 0

    events = store.list(namespace, limit=args.limit)
    if not events:
        print("Nothing remembered yet.")
        return 0
    for event in events:
        print(f"{event.created_at.isoformat()}  {event.namespace}  ({event.kind})")
        print(f"    {event.text}")
    return 0


def _cmd_retry(args: argparse.Namespace) -> int:
    """Run one bounded delivery-recovery sweep; failed inbound is report-only."""
    from dealsieve.diligence import sweep
    from dealsieve.notifications import get_notifier
    from dealsieve.outbound import get_outbox
    from dealsieve.persistence import Repo
    from dealsieve.policy import load_policy

    repo = Repo(args.db) if args.db else Repo()
    repo.init_schema()
    policy = load_policy(args.policy) if args.policy else load_policy()
    report = sweep(repo, policy, get_outbox(policy), get_notifier(), datetime.now(UTC))
    print(json.dumps(report.model_dump(mode="json"), default=str, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dealsieve", description="DealSieve: persistent acquisition agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Push a .eml or .txt file through the full pipeline.")
    p_ingest.add_argument("file", help="Path to a .eml or .txt file")
    p_ingest.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_ingest.add_argument("--policy", default=None, help="Override DEALSIEVE_POLICY_PATH")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_seed = sub.add_parser("seed", help="Reset the DB and load demo data (scripts/seed_demo.py).")
    p_seed.set_defaults(func=_cmd_seed)

    p_reset = sub.add_parser("reset", help="Delete and recreate the database (scripts/reset_db.py).")
    p_reset.set_defaults(func=_cmd_reset)

    p_serve = sub.add_parser("serve", help="Run the FastAPI dashboard/API server.")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--reload", action="store_true", help="Autoreload on code changes (dev only).")
    p_serve.set_defaults(func=_cmd_serve)

    p_bot = sub.add_parser("telegram-bot", help="Run the Telegram long-polling bot.")
    p_bot.set_defaults(func=_cmd_telegram_bot)

    p_status = sub.add_parser("status", help="Print dashboard stats, or one deal's detail.")
    p_status.add_argument("deal", nargs="?", type=int, default=None, help="Deal number, e.g. 101")
    p_status.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_status.add_argument("--policy", default=None, help="Override DEALSIEVE_POLICY_PATH")
    p_status.set_defaults(func=_cmd_status)

    p_followup = sub.add_parser("followup", help="Send due approved-thread follow-ups and escalate stalls.")
    p_followup.add_argument("--as-of", default=None, help="Run cadence as of YYYY-MM-DD or ISO-8601 datetime")
    p_followup.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_followup.add_argument("--policy", default=None, help="Override DEALSIEVE_POLICY_PATH")
    p_followup.set_defaults(func=_cmd_followup)

    p_outbox = sub.add_parser("outbox", help="List broker messages that have been sent.")
    p_outbox.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_outbox.set_defaults(func=_cmd_outbox)

    p_memory = sub.add_parser("memory", help="Show decision memory: what DealSieve remembers.")
    p_memory.add_argument("--namespace", default=None, help='e.g. "investor/human:local" or "broker"')
    p_memory.add_argument("--q", default=None, help="Search text; omit to just list recent memories")
    p_memory.add_argument("--limit", type=int, default=20)
    p_memory.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_memory.set_defaults(func=_cmd_memory)

    p_retry = sub.add_parser(
        "retry",
        help="Retry approved outbound drafts and undelivered notifications once.",
    )
    p_retry.add_argument("--db", default=None, help="Override DEALSIEVE_DB_PATH")
    p_retry.add_argument("--policy", default=None, help="Override DEALSIEVE_POLICY_PATH")
    p_retry.set_defaults(func=_cmd_retry)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
