"""AWS Bedrock AgentCore entrypoint. All AWS-specific code is isolated to this file.

Local check:
    python -m dealsieve.agentcore_app
    curl -X POST localhost:8080/invocations -d '{"type":"status"}'

Payload shapes (see docs/CONTRACTS.md):
    {"type": "email", "eml_base64": "..."}
    {"type": "text", "text": "...", "sender": "..."}
    {"type": "status"}
    {"type": "deal", "id": "101"}

Returns the same `ProcessingOutcome` / `OpportunityDetail` JSON the FastAPI surface returns, via
`process_inbound` -- there is no separate code path for AgentCore.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from dealsieve.ingestion.email import parse_eml
from dealsieve.ingestion.text import from_text
from dealsieve.notifications import get_notifier
from dealsieve.persistence import Repo
from dealsieve.pipeline import process_inbound
from dealsieve.policy import InvestmentPolicy, load_policy
from dealsieve.schemas import Channel

app = BedrockAgentCoreApp()

_repo: Repo | None = None
_policy: InvestmentPolicy | None = None


def _get_repo() -> Repo:
    global _repo
    if _repo is None:
        db_path = os.environ.get("DEALSIEVE_DB_PATH", "data/dealsieve.db")
        _repo = Repo(db_path)
        _repo.init_schema()
    return _repo


def _get_policy() -> InvestmentPolicy:
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


def _handle_email(payload: dict[str, Any]) -> dict[str, Any]:
    eml_base64 = payload.get("eml_base64")
    if not eml_base64:
        return {"error": "payload.type == 'email' requires 'eml_base64'"}
    raw = base64.b64decode(eml_base64)
    message = parse_eml(raw)
    outcome = process_inbound(message, repo=_get_repo(), policy=_get_policy(), notifier=get_notifier())
    return outcome.model_dump(mode="json")


def _handle_text(payload: dict[str, Any]) -> dict[str, Any]:
    text = payload.get("text")
    if not text:
        return {"error": "payload.type == 'text' requires 'text'"}
    message = from_text(text, channel=Channel.TELEGRAM, sender=payload.get("sender"))
    outcome = process_inbound(message, repo=_get_repo(), policy=_get_policy(), notifier=get_notifier())
    return outcome.model_dump(mode="json")


def _handle_status(payload: dict[str, Any]) -> dict[str, Any]:
    repo = _get_repo()
    policy = _get_policy()
    stats = repo.dashboard_stats(policy.policy_version)
    return stats.model_dump(mode="json")


def _handle_deal(payload: dict[str, Any]) -> dict[str, Any]:
    deal_id = payload.get("id")
    if not deal_id:
        return {"error": "payload.type == 'deal' requires 'id'"}
    detail = _get_repo().opportunity_detail(str(deal_id))
    if detail is None:
        return {"error": f"no opportunity matching {deal_id!r}"}
    return detail.model_dump(mode="json")


_HANDLERS = {
    "email": _handle_email,
    "text": _handle_text,
    "status": _handle_status,
    "deal": _handle_deal,
}


@app.entrypoint
def handler(payload: dict[str, Any]) -> dict[str, Any]:
    payload_type = payload.get("type")
    fn = _HANDLERS.get(payload_type)
    if fn is None:
        return {"error": f"unknown payload type {payload_type!r}; expected one of {sorted(_HANDLERS)}"}
    try:
        return fn(payload)
    except Exception as exc:  # AgentCore expects a JSON-serializable response, never a raw traceback
        return {"error": str(exc)}


if __name__ == "__main__":
    app.run()
