# Instructions for coding agents working in this repo

Read `docs/CONTRACTS.md` first. It defines module ownership, function signatures, and the rules below.

Non-negotiable rules (from the product plan):
1. Never let a model own authoritative financial arithmetic. Finance lives in `dealsieve/underwriting/` as pure, tested functions using `Decimal`.
2. Never weaken a policy threshold to make a fixture pass. Tune fixture inputs instead, and say so.
3. Events (`opportunity_events`) and underwriting runs are immutable. Append, never update or delete.
4. Preserve evidence provenance. Never silently resolve contradictory source data; record it in `WorkingValues.conflicts`.
5. No outbound broker message is ever sent without explicit human approval.
6. WATCH -> condition change -> REVIEW is the highest-priority end-to-end path. `tests/e2e/test_watch_to_review.py` is the spec.
7. Keep AWS-specific code behind interfaces. Local fallback for everything.
8. Never embed credentials. Read secrets from environment variables only.

Working conventions:
- Python 3.12, venv at `.venv` (`source .venv/bin/activate`). Run tests with `python -m pytest tests/<your-area> -q`.
- Only edit files in the directories you own (see CONTRACTS.md). If you need a change elsewhere, describe it in your final report instead of making it.
- Do not edit `pyproject.toml` or `config/investment_policy.yaml`. Report new dependencies in your final message.
- Do not run `git commit`. The orchestrator commits at milestones.
- Types come from `dealsieve.schemas`. Do not define parallel copies of these models.
