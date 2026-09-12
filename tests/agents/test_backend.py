"""Backend selection is driven entirely by environment variables."""

from __future__ import annotations

import pytest

from dealsieve.models.backend import backend_name, get_model, selected_backend
from dealsieve.models.cli_model import CLIModel
from dealsieve.models.scripted import ScriptedModel
from dealsieve.schemas import ModelPurpose

CLI_ENV = (
    "DEALSIEVE_MODEL_BACKEND",
    "DEALSIEVE_CLI_PROVIDER",
    "DEALSIEVE_CLI_MODEL",
    "DEALSIEVE_CLI_MODEL_SKEPTIC",
    "DEALSIEVE_ANTHROPIC_MODEL",
    "DEALSIEVE_BEDROCK_MODEL",
    "DEALSIEVE_OPENAI_MODEL",
    "DEALSIEVE_SCRIPT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in CLI_ENV:
        monkeypatch.delenv(name, raising=False)


def test_the_default_backend_is_the_claude_cli():
    assert selected_backend() == "cli"
    assert backend_name() == "cli:claude:sonnet"
    model = get_model()
    assert isinstance(model, CLIModel)
    assert model.provider == "claude" and model.model_id == "sonnet"


def test_cli_provider_and_model_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_CLI_PROVIDER", "agy")
    monkeypatch.setenv("DEALSIEVE_CLI_MODEL", "gemini-3.8-flash-low")
    assert backend_name() == "cli:agy:gemini-3.8-flash-low"
    assert get_model().describe() == "cli:agy:gemini-3.8-flash-low"


def test_a_purpose_can_override_the_model(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_CLI_MODEL", "sonnet")
    monkeypatch.setenv("DEALSIEVE_CLI_MODEL_SKEPTIC", "opus")
    assert get_model(ModelPurpose.ACQUISITION).model_id == "sonnet"
    assert get_model(ModelPurpose.SKEPTIC).model_id == "opus"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("anthropic", "anthropic:claude-sonnet-5"),
        ("bedrock", "bedrock:global.anthropic.claude-sonnet-4-6"),
        ("openai", "openai:gpt-4.1"),
        ("scripted", "scripted"),
    ],
)
def test_backend_name_for_each_backend(monkeypatch, backend, expected):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", backend)
    assert backend_name() == expected


def test_backend_name_reflects_a_model_override(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "bedrock")
    monkeypatch.setenv("DEALSIEVE_BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5")
    assert backend_name() == "bedrock:us.anthropic.claude-haiku-4-5"


def test_scripted_backend_builds_a_scripted_model(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    model = get_model(script="fixtures/scripted/01_initial_offer.json")
    assert isinstance(model, ScriptedModel)
    assert model.turns_remaining == 3


def test_scripted_backend_falls_back_to_the_script_env_var(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    monkeypatch.setenv("DEALSIEVE_SCRIPT", "fixtures/scripted/03_structural_single_tenant.json")
    assert isinstance(get_model(), ScriptedModel)


def test_scripted_backend_without_a_script_is_an_error(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    with pytest.raises(ValueError, match="requires a script"):
        get_model()


def test_an_unknown_backend_is_rejected(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "llamafile")
    with pytest.raises(ValueError, match="DEALSIEVE_MODEL_BACKEND"):
        backend_name()
