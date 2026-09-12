"""`dealsieve` command-line entry point (argparse). Declared in pyproject as `dealsieve = "dealsieve.cli:main"`.

Subcommands: ingest <file.eml|.txt>, seed, reset, serve [--port], telegram-bot, status [deal#].
"""

from __future__ import annotations

import argparse
import sys
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
