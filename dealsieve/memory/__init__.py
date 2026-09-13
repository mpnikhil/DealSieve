"""Decision memory: how this investor decides, and how each broker behaves.

``get_memory_store(repo)`` picks the backend (``DEALSIEVE_MEMORY=local|agentcore``, local by default).
Hooks in ``dealsieve.diligence`` call ``remember_decision`` / ``remember_broker_outcome`` on approve,
reject, answer and stall; ``recall_for_deal`` is how the read side (``Repo.opportunity_detail``, the
skeptic prompt, the credit rationale, the threshold alerts) looks memory up. Every record also appends
a ``MEMORY_RECORDED`` event to the opportunity, when there is one.
"""

from dealsieve.memory.decisions import (
    alert_kind_label,
    broker_namespace,
    deal_label,
    document_kind_label,
    investor_namespace,
    recall_for_deal,
    remember_alert_acknowledgement,
    remember_broker_outcome,
    remember_decision,
)
from dealsieve.memory.ranking import rank_events, score_event
from dealsieve.memory.store import (
    AgentCoreMemoryStore,
    LocalMemoryStore,
    MemoryStore,
    get_memory_store,
)

__all__ = [
    "AgentCoreMemoryStore",
    "LocalMemoryStore",
    "MemoryStore",
    "alert_kind_label",
    "broker_namespace",
    "deal_label",
    "document_kind_label",
    "get_memory_store",
    "investor_namespace",
    "rank_events",
    "recall_for_deal",
    "remember_alert_acknowledgement",
    "remember_broker_outcome",
    "remember_decision",
    "score_event",
]
