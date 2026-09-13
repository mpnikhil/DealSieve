"""AgentCoreMemoryStore against a fake bedrock_agentcore MemoryClient.

Asserts the exact create_event/retrieve_memories arguments the contract specifies, the unconditional
write-through to the local table, and that every AWS call is wrapped so a raise degrades to the local
store instead of propagating. The one @pytest.mark.live test talks to real AWS and skips without
credentials (excluded by default via the repo-wide `-m 'not live'` addopts).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from dealsieve.memory import AgentCoreMemoryStore, LocalMemoryStore, investor_namespace
from dealsieve.persistence import Repo
from dealsieve.schemas import EventType, MemoryEvent, Opportunity, Property


def _make_opp(repo: Repo) -> Opportunity:
    prop = repo.upsert_property(
        Property(canonical_address="1 Test St", normalized_address="1 TEST ST", city="Sacramento", state="CA")
    )
    return repo.create_opportunity(Opportunity(property_id=prop.property_id, display_name="Test Property"))


class FakeMemoryClient:
    """Records every call; behaviour is configured per-test via the *_return/*_raises attributes."""

    def __init__(self) -> None:
        self.create_or_get_memory_calls: list[dict[str, Any]] = []
        self.get_memory_strategies_calls: list[str] = []
        self.create_event_calls: list[dict[str, Any]] = []
        self.retrieve_memories_calls: list[dict[str, Any]] = []
        self.list_events_calls: list[dict[str, Any]] = []

        self.memory_id = "mem-123"
        self.strategies: list[dict[str, Any]] = []
        self.create_event_raises: Exception | None = None
        self.create_event_return: dict[str, Any] | None = None
        self.retrieve_memories_return: list[dict[str, Any]] = []
        self.retrieve_memories_raises: Exception | None = None
        self.list_events_return: list[dict[str, Any]] = []
        self.list_events_raises: Exception | None = None

    def create_or_get_memory(self, *, name: str, strategies: list[Any]) -> dict[str, Any]:
        self.create_or_get_memory_calls.append({"name": name, "strategies": strategies})
        return {"id": self.memory_id}

    def get_memory_strategies(self, memory_id: str) -> list[dict[str, Any]]:
        self.get_memory_strategies_calls.append(memory_id)
        return self.strategies

    def create_event(self, *, memory_id, actor_id, session_id, messages) -> dict[str, Any]:
        self.create_event_calls.append(
            {"memory_id": memory_id, "actor_id": actor_id, "session_id": session_id, "messages": messages}
        )
        if self.create_event_raises is not None:
            raise self.create_event_raises
        return self.create_event_return or {"eventId": "evt-1"}

    def retrieve_memories(self, *, memory_id, query, top_k, namespace=None, namespace_path=None) -> list[dict[str, Any]]:
        self.retrieve_memories_calls.append(
            {"memory_id": memory_id, "query": query, "top_k": top_k, "namespace": namespace, "namespace_path": namespace_path}
        )
        if self.retrieve_memories_raises is not None:
            raise self.retrieve_memories_raises
        return self.retrieve_memories_return

    def list_events(self, *, memory_id, actor_id, session_id) -> list[dict[str, Any]]:
        self.list_events_calls.append({"memory_id": memory_id, "actor_id": actor_id, "session_id": session_id})
        if self.list_events_raises is not None:
            raise self.list_events_raises
        return self.list_events_return


# --------------------------------------------------------------------------- record


def test_record_calls_create_event_with_contract_arguments_and_writes_through(repo: Repo) -> None:
    opp = _make_opp(repo)
    fake = FakeMemoryClient()
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)
    event = MemoryEvent(
        namespace=investor_namespace("human:local"),
        kind="decision",
        actor="human:local",
        opportunity_id=opp.opportunity_id,
        deal_number=opp.deal_number,
        text="Approved the information request on deal #1.",
    )

    recorded = store.record(event)

    assert fake.create_event_calls == [
        {
            "memory_id": "mem-123",
            "actor_id": "investor_human:local",
            "session_id": opp.opportunity_id,
            "messages": [(event.text, "ASSISTANT")],
        }
    ]
    assert recorded.store == "agentcore"
    assert recorded.external_id == "evt-1"

    # Write-through: the local table (what the dashboard and every other test reads) has it too.
    local_texts = [e.text for e in LocalMemoryStore(repo).list(investor_namespace("human:local"))]
    assert event.text in local_texts

    # Every record also appends MEMORY_RECORDED to the opportunity.
    recorded_events = [e for e in repo.list_events(opp.opportunity_id) if e.type == EventType.MEMORY_RECORDED]
    assert len(recorded_events) == 1


def test_record_uses_global_session_when_no_opportunity(repo: Repo) -> None:
    fake = FakeMemoryClient()
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)
    event = MemoryEvent(namespace=investor_namespace("human:local"), kind="note", actor="human:local", text="hi")
    store.record(event)
    assert fake.create_event_calls[0]["session_id"] == "global"


def test_record_degrades_to_local_when_create_event_raises(repo: Repo) -> None:
    fake = FakeMemoryClient()
    fake.create_event_raises = RuntimeError("boto3 exploded")
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)
    event = MemoryEvent(namespace=investor_namespace("human:local"), kind="note", actor="human:local", text="hi")

    recorded = store.record(event)

    assert recorded.store == "local"
    assert recorded.external_id is None
    # Still landed in the local table despite the AWS failure.
    assert event.text in [e.text for e in LocalMemoryStore(repo).list(investor_namespace("human:local"))]


def test_degrades_to_local_when_create_or_get_memory_raises(repo: Repo) -> None:
    class ExplodingClient(FakeMemoryClient):
        def create_or_get_memory(self, *, name: str, strategies: list[Any]) -> dict[str, Any]:
            raise RuntimeError("no AWS credentials")

    store = AgentCoreMemoryStore(repo, client=ExplodingClient())  # no memory_id -> must resolve one
    event = MemoryEvent(namespace=investor_namespace("human:local"), kind="note", actor="human:local", text="hi")

    recorded = store.record(event)

    assert recorded.store == "local"


# --------------------------------------------------------------------------- recall


def test_recall_uses_retrieve_memories_when_strategies_exist(repo: Repo) -> None:
    fake = FakeMemoryClient()
    fake.strategies = [{"strategyId": "s1"}]
    fake.retrieve_memories_return = [
        {"content": {"text": "Approved a $42,000 credit on a similar deal."}, "score": 0.91, "memoryRecordId": "rec-1"}
    ]
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)

    hits = store.recall("roof credit", namespaces=["investor/human:local"], limit=5)

    assert fake.retrieve_memories_calls == [
        {
            "memory_id": "mem-123",
            "query": "roof credit",
            "top_k": 5,
            "namespace": "/investor/human:local/",
            "namespace_path": None,
        }
    ]
    assert len(hits) == 1
    assert hits[0].text == "Approved a $42,000 credit on a similar deal."
    assert hits[0].score == pytest.approx(0.91)
    assert hits[0].namespace == "investor/human:local"


def test_recall_uses_namespace_path_for_wildcard_namespace(repo: Repo) -> None:
    fake = FakeMemoryClient()
    fake.strategies = [{"strategyId": "s1"}]
    fake.retrieve_memories_return = []
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)

    store.recall("roof", namespaces=["investor/*"], limit=5)

    assert fake.retrieve_memories_calls[0]["namespace"] is None
    assert fake.retrieve_memories_calls[0]["namespace_path"] == "/investor/"


def test_recall_falls_back_to_list_events_when_no_strategies(repo: Repo) -> None:
    fake = FakeMemoryClient()
    fake.strategies = []  # no long-term strategies configured
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)

    namespace = investor_namespace("human:local")
    event = MemoryEvent(
        namespace=namespace,
        kind="decision",
        actor="human:local",
        text="Approved the roof diligence request.",
    )
    recorded = store.record(event)  # writes through locally and gets external_id "evt-1"
    fake.list_events_return = [{"eventId": recorded.external_id}]

    hits = store.recall("roof", namespaces=[namespace], limit=5)

    assert fake.retrieve_memories_calls == []
    assert fake.list_events_calls == [{"memory_id": "mem-123", "actor_id": "investor_human:local", "session_id": "global"}]
    assert len(hits) == 1
    assert hits[0].text == "Approved the roof diligence request."


def test_recall_degrades_to_local_when_every_aws_call_raises(repo: Repo) -> None:
    fake = FakeMemoryClient()
    fake.strategies = [{"strategyId": "s1"}]
    fake.retrieve_memories_raises = RuntimeError("service unavailable")
    fake.list_events_raises = RuntimeError("service unavailable")
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)

    namespace = investor_namespace("human:local")
    store.record(
        MemoryEvent(namespace=namespace, kind="decision", actor="human:local", text="Approved the roof request.")
    )
    fake.create_event_raises = None  # record() above must have succeeded to seed the local mirror

    hits = store.recall("roof", namespaces=[namespace], limit=5)

    assert len(hits) == 1
    assert hits[0].text == "Approved the roof request."


def test_list_delegates_to_local_mirror(repo: Repo) -> None:
    fake = FakeMemoryClient()
    store = AgentCoreMemoryStore(repo, client=fake, memory_id=fake.memory_id)
    namespace = investor_namespace("human:local")
    store.record(MemoryEvent(namespace=namespace, kind="note", actor="human:local", text="hello"))

    events = store.list(namespace, limit=10)

    assert [e.text for e in events] == ["hello"]


# --------------------------------------------------------------------------- live


@pytest.mark.live
def test_agentcore_memory_store_against_real_bedrock(tmp_path) -> None:
    boto3 = pytest.importorskip("boto3")
    session = boto3.Session()
    if session.get_credentials() is None:
        pytest.skip("no AWS credentials available")

    repo = Repo(tmp_path / "live_memory.db")
    repo.init_schema()
    store = AgentCoreMemoryStore(repo)
    event = MemoryEvent(
        namespace=investor_namespace("human:live-test"),
        kind="note",
        actor="human:live-test",
        text=f"Live AgentCore smoke test at {datetime.now(UTC).isoformat()}.",
    )

    recorded = store.record(event)

    assert recorded.store in {"agentcore", "local"}
