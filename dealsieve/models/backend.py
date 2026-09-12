"""Backend selection. W3 implements. Purposes may map to different models (e.g. cheaper for extraction)."""

from __future__ import annotations

import os

from strands.models import Model

from dealsieve.schemas import ModelPurpose

DEFAULT_BACKEND = "cli"
DEFAULT_CLI_PROVIDER = "claude"
DEFAULT_CLI_MODEL = "sonnet"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_BEDROCK_MODEL = "global.anthropic.claude-sonnet-4-6"
DEFAULT_OPENAI_MODEL = "gpt-4.1"

VALID_BACKENDS = ("cli", "anthropic", "bedrock", "openai", "scripted")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def selected_backend() -> str:
    """The backend named by DEALSIEVE_MODEL_BACKEND (default: cli)."""
    backend = (_env("DEALSIEVE_MODEL_BACKEND", DEFAULT_BACKEND) or DEFAULT_BACKEND).lower()
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"DEALSIEVE_MODEL_BACKEND={backend!r} is not one of {', '.join(VALID_BACKENDS)}"
        )
    return backend


def _model_for(prefix: str, purpose: ModelPurpose, default: str | None) -> str | None:
    """`DEALSIEVE_<PREFIX>_MODEL_<PURPOSE>` overrides `DEALSIEVE_<PREFIX>_MODEL`."""
    per_purpose = _env(f"DEALSIEVE_{prefix}_MODEL_{purpose.value.upper()}")
    if per_purpose:
        return per_purpose
    return _env(f"DEALSIEVE_{prefix}_MODEL", default)


def cli_provider() -> str:
    return (_env("DEALSIEVE_CLI_PROVIDER", DEFAULT_CLI_PROVIDER) or DEFAULT_CLI_PROVIDER).lower()


def backend_name() -> str:
    """e.g. 'cli:claude:sonnet', 'bedrock:global.anthropic.claude-sonnet-4-6', 'scripted'."""
    backend = selected_backend()
    purpose = ModelPurpose.ACQUISITION
    if backend == "cli":
        provider = cli_provider()
        model = _model_for("CLI", purpose, DEFAULT_CLI_MODEL) or "default"
        return f"cli:{provider}:{model}"
    if backend == "anthropic":
        return f"anthropic:{_model_for('ANTHROPIC', purpose, DEFAULT_ANTHROPIC_MODEL)}"
    if backend == "bedrock":
        return f"bedrock:{_model_for('BEDROCK', purpose, DEFAULT_BEDROCK_MODEL)}"
    if backend == "openai":
        return f"openai:{_model_for('OPENAI', purpose, DEFAULT_OPENAI_MODEL)}"
    return "scripted"


def get_model(purpose: ModelPurpose = ModelPurpose.ACQUISITION, *, script: str | None = None) -> Model:
    """Build the Strands model provider for one purpose.

    `script` is only meaningful for the scripted backend; other backends ignore it.
    """
    backend = selected_backend()

    if backend == "cli":
        from dealsieve.models.cli_model import CLIModel

        return CLIModel(
            provider=cli_provider(),
            model_id=_model_for("CLI", purpose, DEFAULT_CLI_MODEL),
        )

    if backend == "anthropic":
        from strands.models.anthropic import AnthropicModel

        return AnthropicModel(
            model_id=_model_for("ANTHROPIC", purpose, DEFAULT_ANTHROPIC_MODEL),
            max_tokens=int(_env("DEALSIEVE_ANTHROPIC_MAX_TOKENS", "8192") or 8192),
        )

    if backend == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=_model_for("BEDROCK", purpose, DEFAULT_BEDROCK_MODEL),
            region_name=_env("AWS_REGION", "us-west-2"),
        )

    if backend == "openai":
        from strands.models.openai import OpenAIModel

        return OpenAIModel(model_id=_model_for("OPENAI", purpose, DEFAULT_OPENAI_MODEL))

    from dealsieve.models.scripted import ScriptedModel

    resolved = script or _env("DEALSIEVE_SCRIPT")
    if not resolved:
        raise ValueError(
            "DEALSIEVE_MODEL_BACKEND=scripted requires a script path "
            "(pass script=... to process_inbound, or set DEALSIEVE_SCRIPT)"
        )
    return ScriptedModel(resolved)


__all__ = ["backend_name", "cli_provider", "get_model", "selected_backend"]
