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

from dealsieve.notifications import Notifier
from dealsieve.persistence import Repo
from dealsieve.policy import InvestmentPolicy
from dealsieve.schemas import InboundMessage, ProcessingOutcome


def process_inbound(
    message: InboundMessage,
    *,
    repo: Repo,
    policy: InvestmentPolicy,
    notifier: Notifier,
    script: str | None = None,
) -> ProcessingOutcome:
    """script: path to a fixtures/scripted/*.json file when DEALSIEVE_MODEL_BACKEND=scripted."""
    raise NotImplementedError("W3: dealsieve.pipeline.process_inbound")
