"""LocalMemoryStore: record/recall ranking, thresholding, recency tie-break, and namespace scoping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dealsieve.memory import (
    LocalMemoryStore,
    broker_namespace,
    get_memory_store,
    investor_namespace,
)
from dealsieve.persistence import Repo
from dealsieve.schemas import EventType, MemoryEvent, Opportunity, Property


def _make_opp(repo: Repo, *, display_name: str = "Power Inn") -> Opportunity:
    prop = repo.upsert_property(
        Property(
            canonical_address=f"1 {display_name} St, Sacramento, CA",
            normalized_address=f"1 {display_name.upper()} ST SACRAMENTO CA",
            city="Sacramento",
            state="CA",
        )
    )
    return repo.create_opportunity(
        Opportunity(
            property_id=prop.property_id,
            display_name=display_name,
            broker_email="maya.chen@brokerage.example",
        )
    )


def test_get_memory_store_defaults_to_local(repo: Repo, monkeypatch) -> None:
    monkeypatch.delenv("DEALSIEVE_MEMORY", raising=False)
    store = get_memory_store(repo)
    assert isinstance(store, LocalMemoryStore)
    assert store.name == "local"


def test_record_fills_store_and_appends_memory_recorded_event(repo: Repo) -> None:
    opp = _make_opp(repo)
    store = LocalMemoryStore(repo)
    event = MemoryEvent(
        namespace=investor_namespace("human:local"),
        kind="decision",
        actor="human:local",
        opportunity_id=opp.opportunity_id,
        deal_number=opp.deal_number,
        text="Approved the information request on deal #1 (topics: Roof age).",
    )
    recorded = store.record(event)
    assert recorded.store == "local"

    events = repo.list_events(opp.opportunity_id)
    memory_recorded = [e for e in events if e.type == EventType.MEMORY_RECORDED]
    assert len(memory_recorded) == 1
    assert memory_recorded[0].summary == event.text


def test_record_without_opportunity_id_does_not_append_event(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    event = MemoryEvent(
        namespace=investor_namespace("human:local"),
        kind="note",
        actor="human:local",
        text="A memory not tied to any deal.",
    )
    recorded = store.record(event)
    assert recorded.store == "local"
    # No opportunity to append to; list_memory_events should still see it.
    assert recorded.memory_event_id in {e.memory_event_id for e in store.list(investor_namespace("human:local"))}


def test_recall_ranks_by_relevance_and_applies_threshold(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    namespace = investor_namespace("human:local")
    store.record(
        MemoryEvent(
            namespace=namespace,
            kind="decision",
            actor="human:local",
            text="Approved a $42,000 roof credit request on deal #113.",
        )
    )
    store.record(
        MemoryEvent(
            namespace=namespace,
            kind="decision",
            actor="human:local",
            text="Rejected a $60,000 credit request as aggressive on deal #99.",
        )
    )
    store.record(
        MemoryEvent(
            namespace=namespace,
            kind="note",
            actor="human:local",
            text="Completely unrelated grocery list about kittens and spreadsheets.",
        )
    )

    hits = store.recall("roof credit request", namespaces=[namespace], limit=5)
    assert hits, "expected at least one relevant hit"
    assert all(hit.score >= 0.35 for hit in hits)
    # The roof-specific memory should outrank the merely credit-shaped one.
    assert hits[0].text.startswith("Approved a $42,000 roof credit")
    texts = [hit.text for hit in hits]
    assert not any("kittens" in text for text in texts)


def test_recall_recency_tie_break(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    namespace = investor_namespace("human:local")
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = older + timedelta(days=5)
    store.record(
        MemoryEvent(
            namespace=namespace,
            kind="decision",
            actor="human:local",
            text="Approved the information request on deal #1 (topics: Roof age).",
            created_at=older,
        )
    )
    store.record(
        MemoryEvent(
            namespace=namespace,
            kind="decision",
            actor="human:local",
            text="Approved the information request on deal #1 (topics: Roof age).",
            created_at=newer,
        )
    )
    hits = store.recall("roof age information request", namespaces=[namespace], limit=5)
    assert len(hits) == 2
    assert hits[0].created_at == newer


def test_recall_scopes_to_requested_namespaces(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    investor_ns = investor_namespace("human:local")
    broker_ns = broker_namespace("Maya.Chen@Brokerage.example")
    store.record(
        MemoryEvent(
            namespace=investor_ns,
            kind="decision",
            actor="human:local",
            text="Approved the roof diligence request on deal #1.",
        )
    )
    store.record(
        MemoryEvent(
            namespace=broker_ns,
            kind="broker",
            actor="maya.chen@brokerage.example",
            text="maya.chen@brokerage.example answered 'Roof age' with a condition report 3 days after the request.",
        )
    )

    investor_hits = store.recall("roof", namespaces=[investor_ns], limit=5)
    assert all(hit.namespace == investor_ns for hit in investor_hits)

    both_hits = store.recall("roof", namespaces=[investor_namespace("*"), broker_ns], limit=5)
    namespaces_seen = {hit.namespace for hit in both_hits}
    assert investor_ns in namespaces_seen
    assert broker_ns in namespaces_seen


def test_broker_namespace_lowercases_email() -> None:
    assert broker_namespace("Maya.Chen@Brokerage.EXAMPLE") == "broker/maya.chen@brokerage.example"


def test_list_memory_events_orders_newest_first(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    namespace = investor_namespace("human:local")
    first = store.record(
        MemoryEvent(namespace=namespace, kind="note", actor="human:local", text="first", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    second = store.record(
        MemoryEvent(namespace=namespace, kind="note", actor="human:local", text="second", created_at=datetime(2026, 1, 2, tzinfo=UTC))
    )
    events = store.list(namespace, limit=10)
    assert [e.memory_event_id for e in events] == [second.memory_event_id, first.memory_event_id]


def test_list_empty_namespace_returns_everything(repo: Repo) -> None:
    store = LocalMemoryStore(repo)
    store.record(
        MemoryEvent(namespace=investor_namespace("human:local"), kind="note", actor="human:local", text="investor note")
    )
    store.record(
        MemoryEvent(namespace=broker_namespace("maya@brokerage.example"), kind="broker", actor="maya@brokerage.example", text="broker note")
    )
    events = store.list("", limit=10)
    assert len(events) == 2
