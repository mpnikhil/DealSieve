"""Decision-memory backends.

``LocalMemoryStore`` is the SQLite-backed default (table ``memory_events``, via ``Repo``); it is the
only store used offline and in tests. ``AgentCoreMemoryStore`` talks to Amazon Bedrock AgentCore
Memory (``bedrock_agentcore.memory.MemoryClient``) but always writes through to the same local table,
so the dashboard and tests never depend on AWS, and degrades to the local store (with a logged
warning) whenever an AWS call fails.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, runtime_checkable

from dealsieve.memory.ranking import rank_events
from dealsieve.persistence import Repo
from dealsieve.schemas import (
    Actor,
    EventType,
    MemoryEvent,
    MemoryHit,
    OpportunityEvent,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class MemoryStore(Protocol):
    name: str  # "local" | "agentcore"

    def record(self, event: MemoryEvent) -> MemoryEvent:
        """Persist ``event`` (assigning ``store``/``external_id``) and return the stored copy."""
        ...

    def recall(self, query: str, *, namespaces: list[str], limit: int = 5) -> list[MemoryHit]:
        """Ranked hits across ``namespaces`` for ``query``, best first."""
        ...

    def list(self, namespace: str, limit: int = 50) -> list[MemoryEvent]:
        """Most recent events in ``namespace``, newest first. An empty ``namespace`` means "all"."""
        ...


def _append_memory_recorded_event(repo: Repo, event: MemoryEvent) -> None:
    """Every record also appends a MEMORY_RECORDED event to the opportunity, when there is one."""
    if event.opportunity_id is None:
        return
    repo.append_event(
        OpportunityEvent(
            opportunity_id=event.opportunity_id,
            type=EventType.MEMORY_RECORDED,
            actor=Actor.SYSTEM,
            summary=event.text,
            payload={
                "memory_event_id": event.memory_event_id,
                "namespace": event.namespace,
                "kind": event.kind,
                "store": event.store,
            },
        )
    )


class LocalMemoryStore:
    """SQLite-backed memory store. The write-through target for every other backend."""

    name = "local"

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    def record(self, event: MemoryEvent) -> MemoryEvent:
        final = event if event.store is not None else event.model_copy(update={"store": "local"})
        self.repo.store_memory_event(final)
        _append_memory_recorded_event(self.repo, final)
        return final

    def recall(self, query: str, *, namespaces: list[str], limit: int = 5) -> list[MemoryHit]:
        # Over-fetch before ranking: search_memory_events returns recency-ordered rows, not
        # relevance-ordered ones, so the top `limit` by insertion time would not be the top `limit`
        # by rapidfuzz score.
        candidates = self.repo.search_memory_events(namespaces, limit=max(limit * 10, 100))
        return rank_events(query, candidates, limit=limit)

    def list(self, namespace: str, limit: int = 50) -> list[MemoryEvent]:
        return self.repo.list_memory_events(namespace, limit=limit)


def _actor_id_for(namespace: str) -> str:
    return namespace.replace("/", "_")


class AgentCoreMemoryStore:
    """Amazon Bedrock AgentCore Memory, with unconditional write-through to a local mirror.

    Every AWS call is wrapped: a failure logs a warning and degrades to the local store rather than
    raising, so a missing region, missing credentials, or a transient service error never breaks the
    diligence loop or the dashboard.
    """

    name = "agentcore"

    def __init__(
        self,
        repo: Repo,
        *,
        client: Any | None = None,
        memory_id: str | None = None,
        region_name: str | None = None,
    ) -> None:
        self.repo = repo
        self._local = LocalMemoryStore(repo)
        self._client = client
        self._region_name = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._memory_id = memory_id or os.environ.get("AGENTCORE_MEMORY_ID") or None
        self._strategies_cache: dict[str, bool] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            from bedrock_agentcore.memory import MemoryClient

            self._client = MemoryClient(region_name=self._region_name)
        return self._client

    def _memory_id_for(self) -> str | None:
        if self._memory_id is not None:
            return self._memory_id
        try:
            client = self._get_client()
            response = client.create_or_get_memory(name="dealsieve_decisions", strategies=[])
            self._memory_id = response.get("id") or response.get("memoryId")
        except Exception as exc:  # noqa: BLE001 -- any AWS/SDK failure degrades to local.
            logger.warning("AgentCore create_or_get_memory failed; degrading to local memory: %s", exc)
            return None
        return self._memory_id

    def _has_strategies(self, memory_id: str) -> bool:
        if memory_id in self._strategies_cache:
            return self._strategies_cache[memory_id]
        has_strategies = False
        try:
            client = self._get_client()
            has_strategies = bool(client.get_memory_strategies(memory_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentCore get_memory_strategies failed; assuming none: %s", exc)
        self._strategies_cache[memory_id] = has_strategies
        return has_strategies

    def record(self, event: MemoryEvent) -> MemoryEvent:
        store_name = "local"
        external_id: str | None = None
        memory_id = self._memory_id_for()
        if memory_id is not None:
            try:
                client = self._get_client()
                response = client.create_event(
                    memory_id=memory_id,
                    actor_id=_actor_id_for(event.namespace),
                    session_id=event.opportunity_id or "global",
                    messages=[(event.text, "ASSISTANT")],
                )
                external_id = response.get("eventId") or response.get("memoryEventId") or response.get("id")
                store_name = "agentcore"
            except Exception as exc:  # noqa: BLE001
                logger.warning("AgentCore create_event failed; degrading to local memory: %s", exc)
        final = event.model_copy(update={"store": store_name, "external_id": external_id})
        # Write-through always: the local table is the read-model every other part of DealSieve
        # (dashboard, tests, the "no strategies" recall fallback below) relies on.
        return self._local.record(final)

    def _retrieve(self, memory_id: str, query: str, namespace: str, limit: int) -> list[MemoryHit] | None:
        try:
            client = self._get_client()
            if namespace.endswith("/*"):
                records = client.retrieve_memories(
                    memory_id=memory_id, namespace_path=f"/{namespace[:-2]}/", query=query, top_k=limit
                )
            else:
                records = client.retrieve_memories(
                    memory_id=memory_id, namespace=f"/{namespace}/", query=query, top_k=limit
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentCore retrieve_memories failed for %s: %s", namespace, exc)
            return None
        return [self._hit_from_record(record, namespace) for record in records]

    def _hit_from_record(self, record: dict[str, Any], namespace: str) -> MemoryHit:
        from dealsieve.schemas import new_id, now_utc

        content = record.get("content")
        text = content.get("text") if isinstance(content, dict) else record.get("text")
        score = record.get("score", record.get("relevanceScore", 0.0))
        try:
            score = max(0.0, min(1.0, float(score)))
        except (TypeError, ValueError):
            score = 0.0
        kind = "decision" if namespace.startswith("investor/") else "broker" if namespace.startswith("broker/") else "note"
        return MemoryHit(
            memory_event_id=record.get("memoryRecordId") or record.get("id") or new_id("mem"),
            namespace=namespace,
            kind=kind,
            text=text or "",
            score=score,
            created_at=now_utc(),
            payload={},
        )

    def _list_events_fallback(self, memory_id: str, namespace: str) -> list[MemoryEvent] | None:
        """``list_events`` needs a concrete (actor, session) pair; a wildcard namespace has no single
        actor to list, so only a concrete namespace can use this path (the caller falls back to the
        local mirror otherwise)."""
        if namespace.endswith("/*"):
            return None
        try:
            client = self._get_client()
            raw_events = client.list_events(
                memory_id=memory_id, actor_id=_actor_id_for(namespace), session_id="global"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AgentCore list_events failed for %s: %s", namespace, exc)
            return None
        events: list[MemoryEvent] = []
        for raw in raw_events:
            local_match = self._match_local_by_external_id(raw.get("eventId") or raw.get("memoryEventId"))
            if local_match is not None:
                events.append(local_match)
        return events

    def _match_local_by_external_id(self, external_id: str | None) -> MemoryEvent | None:
        if external_id is None:
            return None
        for event in self.repo.search_memory_events(["investor/*", "broker/*"], limit=500):
            if event.external_id == external_id:
                return event
        return None

    def recall(self, query: str, *, namespaces: list[str], limit: int = 5) -> list[MemoryHit]:
        memory_id = self._memory_id_for()
        if memory_id is None:
            return self._local.recall(query, namespaces=namespaces, limit=limit)

        hits: list[MemoryHit] = []
        for namespace in namespaces:
            if self._has_strategies(memory_id):
                retrieved = self._retrieve(memory_id, query, namespace, limit)
                if retrieved:
                    hits.extend(retrieved)
                    continue
            events = self._list_events_fallback(memory_id, namespace)
            if events:
                hits.extend(rank_events(query, events, limit=limit))

        if hits:
            hits.sort(key=lambda hit: (-hit.score, hit.memory_event_id))
            return hits[:limit]
        # Nothing came back from AWS (no strategies, empty history, or every call degraded): the
        # local mirror has everything this process has ever recorded via record()'s write-through.
        return self._local.recall(query, namespaces=namespaces, limit=limit)

    def list(self, namespace: str, limit: int = 50) -> list[MemoryEvent]:
        # AWS has no "list this whole namespace" call (list_events needs one actor+session); the
        # local mirror is the read-model of record for listing.
        return self._local.list(namespace, limit=limit)


def get_memory_store(repo: Repo) -> MemoryStore:
    """Choose by DEALSIEVE_MEMORY: local (default) | agentcore."""
    backend = os.environ.get("DEALSIEVE_MEMORY", "local").strip().lower()
    if backend == "agentcore":
        return AgentCoreMemoryStore(repo)
    return LocalMemoryStore(repo)


__all__ = ["AgentCoreMemoryStore", "LocalMemoryStore", "MemoryStore", "get_memory_store"]
