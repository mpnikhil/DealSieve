"""The one entry point every channel calls. Owned by W3/W12.

`process_inbound(message, *, repo, policy, notifier, outbox=None, script=None) -> ProcessingOutcome`

1. **Claim the message (R4).** `repo.claim_message` atomically inserts it, or re-claims a row left
   `failed`/`processing` by an earlier crash, and says which of `claimed | completed | in_flight`
   happened. Only `completed` is a duplicate; `in_flight` says another worker holds the lease right
   now and gets its own summary. Message existence is not completion: a crash halfway through used
   to mark a deal permanently processed with no underwriting run.
2. Build a `ProcessingSession` and run the Strands acquisition agent over the rendered message. The
   agent extracts claims and calls tools; the tools do every deterministic thing and append events.
3. **Safety net (actor=SYSTEM).** Whatever the model forgot, do here: underwrite after any document
   analysis, run the skeptic and raise the diligence requests on a crossing, draft the credit
   request on a loss, interrupt the human on either. The demo must never depend on a model
   remembering a procedure.
4. **Isolation (R6).** Every safety-net action is wrapped independently. A failing skeptic must not
   cost the human their notification. Failures become NOTE events and text in the summary.
5. **Delivery is not assumed (G3).** `notified_human` is true only when a notifier actually returned.
   A message that ends with a stored-but-undelivered alert is marked *failed*, not completed, and a
   re-run resumes that delivery (the safety net's last step) instead of treating the dedupe row as
   proof the human was told.
6. Mark the message completed or failed, and return the outcome. `process_inbound` never raises --
   the claim, the backend name and the session construction are inside the guard too, so a bad
   `DEALSIEVE_OUTBOX` or a locked database returns an outcome and marks the row failed (L4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from dealsieve.agents.acquisition import build_acquisition_agent, render_message_prompt
from dealsieve.agents.tools import (
    ProcessingSession,
    diligence_items_from,
    perform_notify_human,
    perform_request_diligence,
    perform_request_price_adjustment,
    perform_skeptic_review,
    perform_underwrite,
    resume_undelivered_notifications,
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from dealsieve.outbound import Outbox

logger = logging.getLogger(__name__)


def _agent_text(result: Any) -> str:
    """The agent's final one-line summary, if it produced one."""
    message = getattr(result, "message", None) or {}
    parts = [block["text"].strip() for block in message.get("content", []) if block.get("text")]
    return " ".join(part for part in parts if part).strip()


def _resolve_outbox(outbox: Outbox | None) -> Outbox | None:
    """The caller's outbox, else the one the environment configures."""
    if outbox is not None:
        return outbox
    try:
        from dealsieve.outbound import get_outbox
    except ImportError:  # pragma: no cover - only before the outbound package lands
        logger.warning("dealsieve.outbound is unavailable; broker mail cannot be sent")
        return None
    return get_outbox()


#: What a claim attempt concluded. "claimed" is the only one that processes the message.
CLAIM_STATES = ("claimed", "completed", "in_flight")


def _claim(repo: Repo, message: InboundMessage) -> str:
    """R4. One of `CLAIM_STATES`.

    `Repo.claim_message` returns the tri-state directly (F14); a repository that still returns a
    bool is read the old way, where False can only mean "completed".
    """
    claim = getattr(repo, "claim_message", None)
    if claim is not None:
        outcome = claim(message)
        if isinstance(outcome, bool):
            return "claimed" if outcome else "completed"
        state = str(getattr(outcome, "value", outcome)).strip().lower()
        if state in CLAIM_STATES:
            return state
        return "claimed" if outcome else "completed"
    # Pre-R4 repositories have no processing state: fall back to plain existence.
    if repo.message_exists(message.message_id):
        return "completed"
    repo.store_inbound_message(message)
    return "claimed"


def _finish(repo: Repo, message_id: str, *, error: str | None) -> None:
    """Mark the message completed or failed. Never let bookkeeping take the pipeline down."""
    try:
        if error is None:
            mark = getattr(repo, "mark_message_completed", None)
            if mark is not None:
                mark(message_id)
        else:
            mark_failed = getattr(repo, "mark_message_failed", None)
            if mark_failed is not None:
                mark_failed(message_id, error)
    except Exception:  # pragma: no cover - defensive
        logger.exception("could not record the processing state of %s", message_id)


def _not_claimed_outcome(message: InboundMessage, repo: Repo, backend: str, state: str) -> ProcessingOutcome:
    """The outcome for a message this worker must not process.

    Two different facts used to share one summary (F14): a message that is *finished*, and one
    another worker is *in the middle of*. Only the first means "nothing more will happen".
    """
    opportunity_id = repo.find_opportunity_id_by_message_id(message.message_id)
    status = None
    if opportunity_id:
        opp = repo.get_opportunity(opportunity_id)
        status = opp.status if opp else None
    if state == "in_flight":
        summary = f"message {message.message_id} is being processed by another worker; nothing re-run"
    else:
        summary = f"duplicate message {message.message_id}; already processed, nothing re-run"
    return ProcessingOutcome(
        message_id=message.message_id,
        opportunity_id=opportunity_id,
        created_opportunity=False,
        status_before=status,
        status_after=status,
        summary=summary,
        model_backend=backend,
    )


def _startup_failure(message: InboundMessage, repo: Repo, backend: str, error: str) -> ProcessingOutcome:
    """L4: the pipeline never raises, not even before the session exists (F23).

    `backend_name()`, the claim and the session construction (which resolves the outbox, which
    loads the policy) can all raise. When a row exists for the message it is marked failed, so the
    message stays retryable rather than being stranded in `processing` with nobody owning it.
    """
    logger.exception("process_inbound could not start for %s", message.message_id)
    try:
        exists = repo.message_exists(message.message_id)
    except Exception:  # pragma: no cover - a repo that cannot be read cannot be marked either
        logger.exception("could not check whether %s is stored", message.message_id)
        exists = False
    if exists:
        _finish(repo, message.message_id, error=error)
    return ProcessingOutcome(
        message_id=message.message_id,
        opportunity_id=None,
        created_opportunity=False,
        status_before=None,
        status_after=None,
        summary=error,
        model_backend=backend,
    )


# --------------------------------------------------------------------------- safety net


def _isolated(session: ProcessingSession, label: str, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """R6: run one safety-net action so that its failure costs only that action.

    A skeptic agent that times out must not stop the human being told the deal became investable.
    The failure is recorded twice -- as a NOTE event on the opportunity, and in the outcome summary --
    so it is visible in the deal's history and to whoever called the pipeline.
    """
    try:
        return call()
    except Exception as exc:
        error = f"safety-net {label} failed: {type(exc).__name__}: {exc}"
        logger.exception("safety net: %s failed for %s", label, session.message.message_id)
        session.errors.append(error)
        if session.opportunity_id is not None:
            try:
                session.append_event(
                    EventType.NOTE, error, {"step": label, "error": error}, actor=Actor.SYSTEM
                )
            except Exception:  # pragma: no cover - repo failure during error handling
                logger.exception("could not record the safety-net failure as an event")
        return {"error": error}


def _safety_net(session: ProcessingSession) -> list[str]:
    """Do, as the SYSTEM actor, whatever the model forgot. The demo cannot depend on a model."""
    actions: list[str] = []

    # 1. Underwrite. Also covers "a document was analyzed but never re-underwritten", which is the
    #    step that turns $90k of roof work into a status change.
    if session.claims_recorded and session.run_after is None:
        result = _isolated(session, "underwriting", lambda: perform_underwrite(session, actor=Actor.SYSTEM))
        if "error" in result:
            session.errors.append(f"safety-net underwriting did not run: {result['error']}")
        else:
            actions.append(f"underwriting run by safety net ({result['status']})")

    # 2. A crossing owes the human a skeptic review and the diligence chase.
    if session.threshold_crossed and session.skeptic_report is None:
        result = _isolated(
            session, "skeptic review", lambda: perform_skeptic_review(session, actor=Actor.SYSTEM)
        )
        if "skipped" in result:
            session.errors.append(f"safety-net skeptic skipped: {result['skipped']}")
        elif "error" not in result:
            actions.append("skeptic review run by safety net")

    # F21/L2: any skeptic report in this message with chaseable concerns owes the broker a
    # question -- not only one produced by a crossing. A second message on a deal that is
    # *already* in REVIEW raises a report full of missing evidence too, and nothing else chases it.
    if session.skeptic_report is not None and not session.diligence_requests:
        items = diligence_items_from(session.skeptic_report)
        if items:
            result = _isolated(
                session,
                "diligence request",
                lambda: perform_request_diligence(session, items, actor=Actor.SYSTEM),
            )
            if "skipped" not in result and "error" not in result:
                actions.append(f"{len(result['request_ids'])} diligence request(s) raised by safety net")

    # 3. A loss with a frontier owes the human the credit that would fix it.
    if session.credit_draft is None and session.run_after is not None and session.threshold_lost:
        if session.run_after.viability.max_viable_price is not None:
            result = _isolated(
                session,
                "credit request",
                lambda: perform_request_price_adjustment(
                    session,
                    None,
                    "Diligence established immediate capital work; this is the credit that restores the deal.",
                    actor=Actor.SYSTEM,
                ),
            )
            if "skipped" not in result and "error" not in result:
                actions.append(f"credit request drafted by safety net (${result['amount']})")

    # 4. Either kind of decision change owes the human one interruption. A delivery that already
    #    failed in this message is NOT retried here: the message is marked failed instead, so the
    #    retry happens on a re-run rather than hammering a notifier that is currently down.
    if (
        (session.threshold_crossed or session.threshold_lost)
        and session.notification is None
        and session.notification_error is None
    ):
        result = _isolated(
            session,
            "notification",
            lambda: perform_notify_human(
                session,
                "Decision changed; notified by the pipeline safety net.",
                actor=Actor.SYSTEM,
            ),
        )
        if "skipped" in result:
            session.errors.append(f"safety-net notification skipped: {result['skipped']}")
        elif "error" not in result:
            actions.append("human notified by safety net")

    # 5. An alert recorded on an earlier run but never delivered is finished here (R3/G3). The
    #    dedupe row means "we tried", not "the human knows".
    if session.opportunity_id is not None and session.notification_error is None:
        result = _isolated(session, "notification resume", lambda: resume_undelivered_notifications(session))
        if result.get("resumed"):
            actions.append(f"{result['resumed']} undelivered notification(s) delivered by safety net")

    return actions


# --------------------------------------------------------------------------- entry point


def process_inbound(
    message: InboundMessage,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    notifier: Notifier,
    outbox: Outbox | None = None,
    script: str | None = None,
) -> ProcessingOutcome:
    """Process one inbound message end to end. Never raises.

    `outbox` defaults to `dealsieve.outbound.get_outbox()`; `script` is the path to a
    `fixtures/scripted/*.json` file and is only meaningful when `DEALSIEVE_MODEL_BACKEND=scripted`.
    """
    backend = "unknown"
    try:
        backend = backend_name()
        state = _claim(repo, message)
        if state != "claimed":
            return _not_claimed_outcome(message, repo, backend, state)
        session = ProcessingSession(
            repo=repo,
            policy=policy,
            notifier=notifier,
            message=message,
            model_backend=backend,
            script=script,
            outbox=_resolve_outbox(outbox),
        )
    except Exception as exc:  # F23: the claim, the backend and the outbox can all raise
        return _startup_failure(message, repo, backend, f"{type(exc).__name__}: {exc}")

    summary = ""
    fatal: str | None = None
    try:
        agent = build_acquisition_agent(session)
        result = agent(render_message_prompt(message))
        summary = _agent_text(result)
    except Exception as exc:  # the model layer must never take the pipeline down
        fatal = f"{type(exc).__name__}: {exc}"
        logger.exception("acquisition agent failed for %s", message.message_id)
        session.errors.append(fatal)
        if session.opportunity_id is not None:
            try:
                session.append_event(
                    EventType.NOTE,
                    f"Acquisition agent failed: {fatal}",
                    {"error": fatal},
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

    # Completed means "dealt with; never run this again". A message that produced no underwriting
    # run has not been dealt with -- leave it failed so a retry, or a fixed model, can pick it up.
    # So has one whose alert never reached the human: the decision change is recorded but nobody
    # knows about it, and a re-run resumes that delivery (G3).
    alert = session.notification or session.undelivered_notification
    delivered = session.notification is not None and session.notification.delivered
    undelivered = session.notification_error is not None or (
        session.notification is not None and not session.notification.delivered
    )
    if session.run_after is None:
        failure: str | None = fatal or summary
    elif undelivered:
        failure = session.notification_error or "the human was not notified: delivery never completed"
    else:
        failure = None
    _finish(repo, message.message_id, error=failure)

    return ProcessingOutcome(
        message_id=message.message_id,
        opportunity_id=session.opportunity_id,
        created_opportunity=session.created,
        status_before=session.status_before,
        status_after=status_after,
        run_id=session.run_after.run_id if session.run_after else None,
        events_created=list(session.events_created),
        notified_human=delivered,
        notification_id=alert.notification_id if alert else None,
        skeptic_report_id=session.skeptic_report.report_id if session.skeptic_report else None,
        draft_id=session.draft.draft_id if session.draft else None,
        summary=summary,
        model_backend=backend,
    )


__all__ = ["process_inbound"]
