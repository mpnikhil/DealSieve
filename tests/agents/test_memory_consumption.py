"""Phase 3B: the agents consult decision memory (they never write it).

Everything here runs against a real tmp-backed `Repo` and a real `LocalMemoryStore` seeded through
the public hooks (`remember_decision`, `remember_broker_outcome`) -- no fake store, so a change to
the recall ranking or to the remembered sentences shows up here rather than passing silently.

Each consumer is checked twice: once with something remembered, and once with an empty store, where
the skeptic prompt, the credit rationale and the alert body must be exactly what they were before
decision memory existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import dealsieve.agents.skeptic as skeptic_module
from dealsieve.agents.skeptic import (
    MEMORY_BLOCK_HEADING,
    MEMORY_BLOCK_INSTRUCTION,
    SkepticOutput,
    build_skeptic_prompt,
    run_skeptic,
)
from dealsieve.agents.tools import (
    MEMORY_PROMPT_LINES,
    ProcessingSession,
    credit_memory_note,
    perform_notify_human,
    perform_request_price_adjustment,
    recall_memories,
)
from dealsieve.memory import LocalMemoryStore, remember_broker_outcome, remember_decision
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.schemas import (
    DiligenceRequest,
    MemoryHit,
    Opportunity,
    OpportunityStatus,
    OutboundDraft,
    Property,
)

from .conftest import FakeRepo, make_run, make_working_values

BROKER = "maya.chen@brokerage.example"
PRINCIPAL = "human:local"

#: The three concerns the skeptic left open on this deal. They are the recall query, together with
#: the property type and city, so a past decision about the same topics is what comes back.
OPEN_TOPICS = (
    ("Roof age", "How old is the roof?"),
    ("Phase I environmental", "Has a Phase I ever been done?"),
    ("CAM reconciliation", "Can you send last year's CAM reconciliation?"),
)

FINAL_PROMPT_LINE = "Return your structured verdict now. Find what is missing."


# --------------------------------------------------------------------------- the world


@dataclass
class World:
    repo: Repo
    store: LocalMemoryStore
    session: ProcessingSession
    opportunity: Opportunity


def _property(repo: Repo, address: str) -> Property:
    return repo.upsert_property(
        Property(
            canonical_address=address,
            normalized_address=address.upper(),
            city="Sacramento",
            state="CA",
            property_type="industrial",
        )
    )


def _requests(repo: Repo, opportunity_id: str, *, status: str) -> list[DiligenceRequest]:
    stored = []
    for topic, question in OPEN_TOPICS:
        request = DiligenceRequest(
            opportunity_id=opportunity_id, topic=topic, question=question, status=status
        )
        repo.store_diligence_request(request)
        stored.append(request)
    return stored


@pytest.fixture
def world(repo: Repo, policy, recording_notifier, inbound_message) -> World:
    """This deal: in REVIEW, three questions out with the broker, nothing remembered yet."""
    prop = _property(repo, "8330 Power Inn Rd, Sacramento, CA")
    opp = repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name="8330 Power Inn Road, Sacramento, CA",
            broker_name="Maya Chen",
            broker_email=BROKER,
            status=OpportunityStatus.REVIEW,
            working_values=make_working_values(),
        )
    )
    _requests(repo, opp.opportunity_id, status="sent")

    session = ProcessingSession(
        repo=repo,
        policy=policy,
        notifier=recording_notifier,
        message=inbound_message,
        model_backend="scripted",
        memory=LocalMemoryStore(repo),
    )
    session.opportunity_id = opp.opportunity_id
    session.run_after = make_run(
        opp.opportunity_id, OpportunityStatus.REVIEW, cap=Decimal("0.083"), dscr=Decimal("1.43")
    )
    repo.store_underwriting_run(session.run_after)
    return World(repo=repo, store=LocalMemoryStore(repo), session=session, opportunity=opp)


def seed_memory(world: World) -> tuple[str, str]:
    """An earlier deal this investor already decided on, with the same broker on the other side.

    Returns the two sentences the hooks composed, so the assertions below are about what decision
    memory really wrote rather than about a copy of it kept in this file.
    """
    prop = _property(world.repo, "4120 Florin Rd, Sacramento, CA")
    past = world.repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name="4120 Florin Road, Sacramento, CA",
            broker_email=BROKER,
            status=OpportunityStatus.NEAR,
        )
    )
    requests = _requests(world.repo, past.opportunity_id, status="answered")
    draft = OutboundDraft(
        opportunity_id=past.opportunity_id,
        kind="credit_request",
        to_email=BROKER,
        subject="Diligence credit request: Florin Road",
        body="Based on our diligence, we would need a $42,000 credit. The roof is past its life.",
        request_ids=[request.request_id for request in requests],
        requires_approval=True,
        status="pending",
    )
    world.repo.store_draft(draft)

    decision = remember_decision(
        world.store,
        principal=PRINCIPAL,
        draft=draft,
        opportunity=past,
        outcome="approved",
    )
    broker = remember_broker_outcome(
        world.store,
        broker_email=BROKER,
        opportunity=past,
        kind="stalled",
        detail="went silent on 2 requests after 2 follow-ups (Phase I environmental, CAM reconciliation)",
    )
    return decision.text, broker.text


def memory_block(prompt: str) -> list[str]:
    """The recalled block of the skeptic prompt, heading through instruction."""
    lines = prompt.splitlines()
    start = lines.index(MEMORY_BLOCK_HEADING)
    end = lines.index(MEMORY_BLOCK_INSTRUCTION)
    return lines[start : end + 1]


# --------------------------------------------------------------------------- what is recalled


def test_the_seeded_world_recalls_both_the_decision_and_the_broker(world: World) -> None:
    """The premise of every test below: the deal's own topics bring back both namespaces."""
    decision, broker = seed_memory(world)

    hits = recall_memories(world.session)

    assert {hit.text for hit in hits} == {decision, broker}
    assert {hit.kind for hit in hits} == {"decision", "broker"}
    assert all(hit.score >= 0.6 for hit in hits), "both are strong matches; the alert line needs 0.6"


def test_recall_is_empty_when_nothing_has_been_remembered(world: World) -> None:
    assert recall_memories(world.session) == []


# --------------------------------------------------------------------------- the skeptic prompt


def test_the_skeptic_prompt_carries_what_the_investor_decided_before(world: World) -> None:
    decision, broker = seed_memory(world)

    block = memory_block(build_skeptic_prompt(world.session))

    assert block[0] == MEMORY_BLOCK_HEADING
    assert block[-1] == MEMORY_BLOCK_INSTRUCTION
    recalled = block[1:-1]
    assert sorted(recalled) == sorted([f"- {decision}", f"- {broker}"])
    assert all(line.startswith("- ") for line in recalled)
    assert len(recalled) <= MEMORY_PROMPT_LINES


def test_the_recalled_block_is_capped_at_five_lines(world: World) -> None:
    """`recall_for_deal` asks for five; the prompt must not grow past them however many exist."""
    seed_memory(world)
    for index in range(8):
        remember_broker_outcome(
            world.store,
            broker_email=BROKER,
            opportunity=world.opportunity,
            kind="stalled",
            detail=f"went silent on the Phase I environmental request {index} after 2 follow-ups",
        )

    recalled = memory_block(build_skeptic_prompt(world.session))[1:-1]

    assert len(recalled) == MEMORY_PROMPT_LINES


def test_run_skeptic_sends_the_recalled_block_to_the_model(world: World, monkeypatch) -> None:
    decision, _ = seed_memory(world)
    captured: dict[str, str] = {}

    class FakeAgent:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def __call__(self, prompt: str, structured_output_model=None):
            captured["prompt"] = prompt
            return SimpleNamespace(
                structured_output=SkepticOutput(
                    verdict="proceed_with_questions", summary="Gaps remain.", concerns=[]
                )
            )

    monkeypatch.setattr(skeptic_module, "get_model", lambda purpose, script=None: None)
    monkeypatch.setattr(skeptic_module, "Agent", FakeAgent)

    report = run_skeptic(world.session)

    assert report.verdict == "proceed_with_questions"
    assert MEMORY_BLOCK_HEADING in captured["prompt"]
    assert f"- {decision}" in captured["prompt"]


def test_an_empty_memory_leaves_the_skeptic_prompt_byte_identical(world: World) -> None:
    """No hits, no block: a deal with no history gets the pre-Phase-3 prompt, character for
    character."""
    with_empty_store = build_skeptic_prompt(world.session)

    world.session.memory = None
    without_memory_at_all = build_skeptic_prompt(world.session)

    assert with_empty_store == without_memory_at_all
    assert MEMORY_BLOCK_HEADING not in with_empty_store
    assert MEMORY_BLOCK_INSTRUCTION not in with_empty_store
    assert with_empty_store.splitlines()[-1] == FINAL_PROMPT_LINE


# --------------------------------------------------------------------------- credit rationale


def _fell_below(world: World) -> None:
    world.session.threshold_lost = True


def test_the_credit_rationale_recalls_the_last_price_decision(world: World) -> None:
    seed_memory(world)
    _fell_below(world)

    result = perform_request_price_adjustment(
        world.session, None, "Diligence established $90,000 of immediate roof work."
    )

    assert result["memory"] == "You previously approved a $42,000 ask on a similar deal."
    assert result["rationale"] == (
        "Diligence established $90,000 of immediate roof work. "
        "You previously approved a $42,000 ask on a similar deal."
    )
    assert result["memory"] in result["note"]


def test_the_recalled_sentence_never_reaches_the_broker(world: World) -> None:
    """The rationale is pasted verbatim into the broker's email. What this investor paid last time
    is not something a broker may read, so it rides on the result, not on the draft."""
    seed_memory(world)
    _fell_below(world)

    result = perform_request_price_adjustment(
        world.session, None, "Diligence established $90,000 of immediate roof work."
    )
    draft = world.repo.get_draft(result["draft_id"])

    assert "You previously" not in draft.body
    assert "$42,000" not in draft.body
    assert "Diligence established $90,000 of immediate roof work." in draft.body


def test_memory_never_moves_the_deterministic_credit_amount(world: World) -> None:
    """Memory is allowed to explain the ask. It is not allowed to change it: the number still comes
    from the viability frontier, and only from there."""
    _fell_below(world)
    without_memory = perform_request_price_adjustment(world.session, None, "Roof work.")

    seed_memory(world)
    retry = ProcessingSession(
        repo=world.repo,
        policy=world.session.policy,
        notifier=world.session.notifier,
        message=world.session.message.model_copy(update={"message_id": "<second@brokerage.example>"}),
        model_backend="scripted",
        memory=world.store,
    )
    retry.opportunity_id = world.opportunity.opportunity_id
    retry.run_after = world.session.run_after
    retry.threshold_lost = True
    with_memory = perform_request_price_adjustment(retry, None, "Roof work.")

    assert with_memory["amount"] == without_memory["amount"] == with_memory["suggested"]
    assert without_memory["memory"] is None
    assert with_memory["memory"] == "You previously approved a $42,000 ask on a similar deal."


def test_an_empty_memory_leaves_the_credit_rationale_unchanged(world: World) -> None:
    _fell_below(world)

    result = perform_request_price_adjustment(world.session, None, "Roof work established by diligence.")

    assert result["memory"] is None
    assert result["rationale"] == "Roof work established by diligence."
    assert result["note"] == "Nothing is sent until a human approves this draft."


# ------------------------------------------------------ credit_memory_note, on its own


def _hit(text: str, score: float, *, kind: str = "decision", payload: dict | None = None) -> MemoryHit:
    return MemoryHit(
        memory_event_id="mem_1",
        namespace="investor/human:local",
        kind=kind,
        text=text,
        score=score,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        payload=payload or {},
    )


def test_credit_memory_note_reads_the_amount_out_of_the_payload() -> None:
    hit = _hit(
        "Approved a $42,000 credit request on deal #101.",
        0.81,
        payload={"outcome": "approved", "amount": "42000", "draft_kind": "credit_request"},
    )
    assert credit_memory_note([hit]) == "You previously approved a $42,000 ask on a similar deal."


def test_credit_memory_note_falls_back_to_the_remembered_sentence() -> None:
    """An AgentCore hit carries no payload, only the text the store wrote."""
    hit = _hit("Rejected a $60,000 credit request on deal #107.", 0.75)
    assert credit_memory_note([hit]) == "You previously rejected a $60,000 ask on a similar deal."


def test_credit_memory_note_carries_the_reason_a_rejection_was_given() -> None:
    hit = _hit(
        "Rejected a $60,000 credit request on deal #107. Reason: too aggressive for this basis.",
        0.75,
        payload={
            "outcome": "rejected",
            "amount": "60000",
            "draft_kind": "credit_request",
            "reason": "too aggressive for this basis",
        },
    )
    assert credit_memory_note([hit]) == (
        "You previously rejected a $60,000 ask on a similar deal: too aggressive for this basis."
    )


@pytest.mark.parametrize(
    "hit",
    [
        pytest.param(
            _hit("Approved a $42,000 credit request.", 0.55, payload={"outcome": "approved"}),
            id="too weak a match",
        ),
        pytest.param(
            _hit(f"{BROKER} answered the roof question with a $42,000 quote.", 0.9, kind="broker"),
            id="not a decision",
        ),
        pytest.param(
            _hit("Approved the information request on deal #107.", 0.9, payload={"outcome": "approved"}),
            id="not about money",
        ),
        pytest.param(
            _hit("Approved a credit request on deal #107.", 0.9, payload={"outcome": "approved"}),
            id="no amount to cite",
        ),
    ],
)
def test_credit_memory_note_stays_silent(hit: MemoryHit) -> None:
    """It cites what was remembered or it says nothing. It never infers an amount."""
    assert credit_memory_note([hit]) is None


# --------------------------------------------------------------------------- alerts


def test_the_threshold_alert_ends_with_what_the_investor_did_before(world: World) -> None:
    decision, broker = seed_memory(world)
    world.session.threshold_crossed = True

    result = perform_notify_human(world.session)
    body = world.session.notification.body

    assert result["delivered"] is True
    assert body.splitlines()[-1].startswith("You previously: ")
    assert body.splitlines()[-1].removeprefix("You previously: ") in {decision, broker}


def test_the_fell_below_alert_ends_with_what_the_investor_did_before(world: World) -> None:
    seed_memory(world)
    world.session.threshold_lost = True

    perform_notify_human(world.session)
    body = world.session.notification.body

    assert body.splitlines()[-1].startswith("You previously: ")


def test_an_empty_memory_leaves_the_alert_unchanged(world: World) -> None:
    world.session.threshold_crossed = True

    perform_notify_human(world.session)
    body = world.session.notification.body

    assert "You previously" not in body
    assert body.splitlines()[-1] != ""


# --------------------------------------------------------------------------- degrading


def test_recall_returns_nothing_when_the_repository_has_no_memory_table(
    fake_repo: FakeRepo, policy, recording_notifier, inbound_message
) -> None:
    """A repository double that predates `memory_events` must cost the pipeline nothing. The
    existing agent suites run the whole pipeline against exactly this repo."""

    class RepoWithProperties(FakeRepo):
        def get_property(self, property_id: str) -> Property | None:
            return self.properties.get(property_id)

    repo = RepoWithProperties()
    prop = repo.upsert_property(
        Property(
            canonical_address="8330 Power Inn Rd",
            normalized_address="8330 POWER INN RD",
            city="Sacramento",
            property_type="industrial",
        )
    )
    opp = repo.create_opportunity(
        Opportunity(property_id=prop.property_id, display_name="Power Inn", broker_email=BROKER)
    )
    session = ProcessingSession(
        repo=repo,
        policy=policy,
        notifier=recording_notifier,
        message=inbound_message,
        model_backend="scripted",
        memory=LocalMemoryStore(repo),
    )
    session.opportunity_id = opp.opportunity_id

    assert not hasattr(repo, "search_memory_events")
    assert recall_memories(session) == []


def test_recall_returns_nothing_without_a_store(world: World) -> None:
    world.session.memory = None
    assert recall_memories(world.session) == []


# --------------------------------------------------------------------------- the wiring


def _capture_session(monkeypatch, seen: dict) -> None:
    class FakeAgent:
        def __init__(self, session: ProcessingSession) -> None:
            self.session = session

        def __call__(self, prompt: str):
            seen["memory"] = self.session.memory
            return SimpleNamespace(message={"content": [{"text": "done"}]})

    monkeypatch.setattr("dealsieve.pipeline.build_acquisition_agent", lambda session: FakeAgent(session))


def test_process_inbound_gives_the_session_the_configured_store(
    fake_repo: FakeRepo, policy, recording_notifier, inbound_message, monkeypatch
) -> None:
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    seen: dict = {}
    _capture_session(monkeypatch, seen)

    process_inbound(inbound_message, repo=fake_repo, policy=policy, notifier=recording_notifier)

    assert isinstance(seen["memory"], LocalMemoryStore), "DEALSIEVE_MEMORY defaults to the local store"


def test_process_inbound_prefers_the_store_it_was_handed(
    fake_repo: FakeRepo, policy, recording_notifier, inbound_message, monkeypatch, repo: Repo
) -> None:
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    seen: dict = {}
    _capture_session(monkeypatch, seen)
    store = LocalMemoryStore(repo)

    process_inbound(
        inbound_message, repo=fake_repo, policy=policy, notifier=recording_notifier, memory=store
    )

    assert seen["memory"] is store
