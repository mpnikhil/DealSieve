"""Backend selection. W3 implements. Purposes may map to different models (e.g. cheaper for extraction)."""

from __future__ import annotations

from strands.models import Model

from dealsieve.schemas import ModelPurpose


def backend_name() -> str:
    """e.g. 'cli:claude:sonnet', 'bedrock:global.anthropic.claude-sonnet-4-6', 'scripted'."""
    raise NotImplementedError("W3: dealsieve.models.backend.backend_name")


def get_model(purpose: ModelPurpose = ModelPurpose.ACQUISITION, *, script: str | None = None) -> Model:
    raise NotImplementedError("W3: dealsieve.models.backend.get_model")
