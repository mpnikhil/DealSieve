"""Deterministic local ranking shared by ``LocalMemoryStore`` and ``AgentCoreMemoryStore``'s fallback.

rapidfuzz's ``token_set_ratio`` (0-100) is scaled to ``[0, 1]``; hits below ``MIN_SCORE`` are dropped.
Ties (equal score) break on recency, newest first, then on ``memory_event_id`` so ordering never
depends on incidental dict/list iteration order.
"""

from __future__ import annotations

from collections.abc import Iterable

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from dealsieve.schemas import MemoryEvent, MemoryHit

MIN_SCORE = 0.35


def _topics_text(payload: dict) -> str:
    topics = payload.get("topics")
    if isinstance(topics, list):
        return " ".join(str(topic) for topic in topics)
    if isinstance(topics, str):
        return topics
    return ""


def score_event(query: str, event: MemoryEvent) -> float:
    """rapidfuzz token_set_ratio between ``query`` and the event's text + payload topics, in [0, 1].

    ``default_process`` (case-fold, strip punctuation) so "roof" matches "Roof age" and a trailing
    period never costs a match.
    """
    haystack = f"{event.text} {_topics_text(event.payload)}".strip()
    if not query.strip() or not haystack:
        return 0.0
    return fuzz.token_set_ratio(query, haystack, processor=default_process) / 100.0


def rank_events(query: str, events: Iterable[MemoryEvent], *, limit: int = 5) -> list[MemoryHit]:
    """Score every event against ``query``, drop anything below ``MIN_SCORE``, and return the top
    ``limit`` as ``MemoryHit`` -- highest score first, ties broken by recency then id."""
    scored = [(score_event(query, event), event) for event in events]
    scored = [(score, event) for score, event in scored if score >= MIN_SCORE]
    scored.sort(key=lambda pair: (-pair[0], -pair[1].created_at.timestamp(), pair[1].memory_event_id))
    return [
        MemoryHit(
            memory_event_id=event.memory_event_id,
            namespace=event.namespace,
            kind=event.kind,
            text=event.text,
            score=round(score, 4),
            created_at=event.created_at,
            payload=event.payload,
        )
        for score, event in scored[:limit]
    ]


__all__ = ["MIN_SCORE", "rank_events", "score_event"]
