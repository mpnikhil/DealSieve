"""End-to-end against a real coding-agent CLI. Excluded by default (`-m 'not live'`).

Run with a real `claude` on PATH once W1 and W2 have landed:

    python -m pytest tests/agents/test_live_cli.py -m live -q

This is the proof that the same agent, tools and pipeline that pass offline also work when a real
model is doing the extraction.
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.live


@pytest.mark.live
def test_fixture_01_through_the_real_claude_cli_lands_on_watch(tmp_path, policy):
    if shutil.which("claude") is None:
        pytest.skip("the claude CLI is not on PATH")

    import os

    from dealsieve.ingestion import parse_eml
    from dealsieve.notifications import RecordingNotifier
    from dealsieve.persistence import Repo
    from dealsieve.pipeline import process_inbound

    os.environ["DEALSIEVE_MODEL_BACKEND"] = "cli"
    os.environ["DEALSIEVE_CLI_PROVIDER"] = "claude"
    os.environ.setdefault("DEALSIEVE_CLI_MODEL", "sonnet")

    repo = Repo(tmp_path / "live.db")
    repo.init_schema()
    notifier = RecordingNotifier()

    message = parse_eml(ROOT / "fixtures" / "emails" / "01_initial_offer.eml")
    outcome = process_inbound(message, repo=repo, policy=policy, notifier=notifier, script=None)

    assert outcome.model_backend.startswith("cli:claude")
    assert outcome.opportunity_id is not None, outcome.summary
    assert outcome.created_opportunity is True
    assert outcome.status_after.value == "WATCH", outcome.summary
    assert outcome.notified_human is False and notifier.sent == []

    opp = repo.get_opportunity(outcome.opportunity_id)
    assert opp.current_asking_price == Decimal("1550000"), "the model must not invent the price"
    assert opp.viability is not None and opp.viability.max_viable_price is not None

    run = repo.get_underwriting_run(outcome.run_id)
    assert run.normalized.normalized_cap_rate < policy.underwriting.min_normalized_cap_rate
    assert all(g.passed for g in run.gates if g.kind.value == "structural")

    evidence = repo.list_evidence(outcome.opportunity_id)
    assert evidence, "a real extraction must record provenance"
    sources = {e.source_document for e in evidence}
    assert any("Power_Inn_OM" in s for s in sources) or message.message_id in sources


@pytest.mark.live
def test_fixture_05_through_real_codex_with_images_reviews_photos(tmp_path, policy):
    if shutil.which("codex") is None:
        pytest.skip("the codex CLI is not on PATH")

    import os

    from dealsieve.diligence import approve_and_send
    from dealsieve.ingestion import parse_eml
    from dealsieve.notifications import RecordingNotifier
    from dealsieve.outbound import RecordingOutbox
    from dealsieve.persistence import Repo
    from dealsieve.pipeline import process_inbound

    os.environ["DEALSIEVE_MODEL_BACKEND"] = "cli"
    os.environ["DEALSIEVE_CLI_PROVIDER"] = "codex"

    repo = Repo(tmp_path / "live.db")
    repo.init_schema()
    notifier = RecordingNotifier()
    outbox = RecordingOutbox()

    # Pre-seed through scripted backend so deal is in REVIEW with open diligence requests
    for stem in ("01_initial_offer", "02_price_drop"):
        msg = parse_eml(ROOT / "fixtures" / "emails" / f"{stem}.eml")
        process_inbound(
            msg,
            repo=repo,
            policy=policy,
            notifier=notifier,
            outbox=outbox,
            script=str(ROOT / "fixtures" / "scripted" / f"{stem}.json"),
        )

    opps = repo.list_opportunities()
    opp = opps[0]
    drafts = repo.list_drafts(opp.opportunity_id)
    pending = next(d for d in drafts if d.status == "pending")
    approve_and_send(pending.draft_id, repo=repo, policy=policy, outbox=outbox)

    msg5 = parse_eml(ROOT / "fixtures" / "emails" / "05_inspection_report.eml")
    outcome = process_inbound(msg5, repo=repo, policy=policy, notifier=notifier, outbox=outbox, script=None)

    assert outcome.opportunity_id == opp.opportunity_id
    analyses = repo.list_document_analyses(opp.opportunity_id)
    assert len(analyses) == 1
    assert analyses[0].images_reviewed == 3
    assert any("roof" in i.item.lower() for i in analyses[0].capex_items)

