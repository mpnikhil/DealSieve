"""The one entry point every channel calls. Owned by W3.

process_inbound(message, ...) -> ProcessingOutcome

Behaviour contract:
1. Dedupe: if repo.message_exists(message.message_id) return an outcome with summary "duplicate".
2. Store message. Build a ProcessingSession(repo, policy, notifier, message, model).
3. Run the Strands Acquisition Agent (dealsieve.agents.acquisition) with the message as the prompt.
   The agent extracts claims and calls tools; tools do all deterministic work and append events.
4. Safety net (actor=SYSTEM): after the agent returns, if claims were recorded but no underwriting ran,
   run it; if the run crossed into REVIEW and no human notification was sent, send it. The demo must not
   depend on the model remembering a step.
5. Return ProcessingOutcome. Never raise on model failure: record a NOTE event and return summary.
"""

from __future__ import annotations

import logging
from typing import Any

from dealsieve.agents.acquisition import build_acquisition_agent, render_message_prompt
from dealsieve.agents.tools import (
    MAX_BROKER_QUESTIONS,
    ProcessingSession,
    perform_draft_broker_questions,
    perform_notify_human,
    perform_skeptic_review,
    perform_underwrite,
    suggested_questions,
)
from dealsieve.models import backend_name
from dealsieve.notifications import Notifier
from dealsieve.persistence import Repo
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import (
    Actor,
    EventType,
    InboundMessage,
    ProcessingOutcome,
)

logger = logging.getLogger(__name__)


def _agent_text(result: Any) -> str:
    """The agent's final one-line summary, if it produced one."""
    message = getattr(result, "message", None) or {}
    parts = [block["text"].strip() for block in message.get("content", []) if block.get("text")]
    return " ".join(part for part in parts if part).strip()


def _duplicate_outcome(message: InboundMessage, repo: Repo, backend: str) -> ProcessingOutcome:
    opportunity_id = repo.find_opportunity_id_by_message_id(message.message_id)
    status = None
    if opportunity_id:
        opp = repo.get_opportunity(opportunity_id)
        status = opp.status if opp else None
    return ProcessingOutcome(
        message_id=message.message_id,
        opportunity_id=opportunity_id,
        created_opportunity=False,
        status_before=status,
        status_after=status,
        summary=f"duplicate message {message.message_id}; already processed, nothing re-run",
        model_backend=backend,
    )


def _safety_net(session: ProcessingSession) -> list[str]:
    """Do, as the SYSTEM actor, whatever the model forgot. The demo cannot depend on a model."""
    actions: list[str] = []

    if session.claims_recorded and session.run_after is None:
        result = perform_underwrite(session, actor=Actor.SYSTEM)
        if "error" in result:
            session.errors.append(f"safety-net underwriting failed: {result['error']}")
        else:
            actions.append(f"underwriting run by safety net ({result['status']})")

    if session.threshold_crossed and session.skeptic_report is None:
        result = perform_skeptic_review(session, actor=Actor.SYSTEM)
        if "skipped" in result:
            session.errors.append(f"safety-net skeptic skipped: {result['skipped']}")
        else:
            actions.append("skeptic review run by safety net")

    if session.threshold_crossed and session.draft is None:
        questions = suggested_questions(session.skeptic_report)[:MAX_BROKER_QUESTIONS]
        if questions:
            result = perform_draft_broker_questions(session, questions, actor=Actor.SYSTEM)
            if "skipped" not in result:
                actions.append("broker questions drafted by safety net")

    if session.threshold_crossed and session.notification is None:
        result = perform_notify_human(
            session,
            "Crossed into REVIEW; notified by the pipeline safety net.",
            actor=Actor.SYSTEM,
        )
        if "skipped" in result:
            session.errors.append(f"safety-net notification skipped: {result['skipped']}")
        else:
            actions.append("human notified by safety net")

    return actions


def process_inbound(
    message: InboundMessage,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    notifier: Notifier,
    script: str | None = None,
) -> ProcessingOutcome:
    """script: path to a fixtures/scripted/*.json file when DEALSIEVE_MODEL_BACKEND=scripted."""
    backend = backend_name()

    if repo.message_exists(message.message_id):
        return _duplicate_outcome(message, repo, backend)

    repo.store_inbound_message(message)

    session = ProcessingSession(
        repo=repo,
        policy=policy,
        notifier=notifier,
        message=message,
        model_backend=backend,
        script=script,
    )

    summary = ""
    try:
        agent = build_acquisition_agent(session)
        result = agent(render_message_prompt(message))
        summary = _agent_text(result)
    except Exception as exc:  # the model layer must never take the pipeline down
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("acquisition agent failed for %s", message.message_id)
        session.errors.append(error)
        if session.opportunity_id is not None:
            try:
                session.append_event(
                    EventType.NOTE,
                    f"Acquisition agent failed: {error}",
                    {"error": error},
                    actor=Actor.SYSTEM,
                )
            except Exception:  # pragma: no cover - repo failure during error handling
                logger.exception("could not record the agent failure as an event")

    net_actions = _safety_net(session)

    opp = session.opportunity()
    status_after = opp.status if opp is not None else None
    if status_after is None and session.run_after is not None:
        status_after = session.run_after.status

    if not summary:
        if session.errors:
            summary = "; ".join(session.errors)
        elif status_after is not None:
            summary = f"Processed: {status_after.value}"
        else:
            summary = "Nothing recorded for this message"
    if session.errors and not summary.startswith(session.errors[0]):
        summary = f"{summary} [errors: {'; '.join(session.errors)}]"
    if net_actions:
        summary = f"{summary} [safety net: {'; '.join(net_actions)}]"

    return ProcessingOutcome(
        message_id=message.message_id,
        opportunity_id=session.opportunity_id,
        created_opportunity=session.created,
        status_before=session.status_before,
        status_after=status_after,
        run_id=session.run_after.run_id if session.run_after else None,
        events_created=list(session.events_created),
        notified_human=session.notification is not None,
        notification_id=session.notification.notification_id if session.notification else None,
        skeptic_report_id=session.skeptic_report.report_id if session.skeptic_report else None,
        draft_id=session.draft.draft_id if session.draft else None,
        summary=summary,
        model_backend=backend,
    )


__all__ = ["process_inbound"]
