"""Deterministic replay model provider.

`ScriptedModel` replays a `fixtures/scripted/<name>.json` script instead of calling anything. It
exists so `tests/e2e` and the offline demo exercise the real Strands agent loop, the real tools and
the real deterministic finance with zero network and zero variance.

Script shape::

    {
      "turns": [
        {"tool_calls": [{"name": "record_claims",
                         "input_ref": "fixtures/expected/claims_01_initial_offer.json",
                         "input_arg": "claims"}]},
        {"tool_calls": [{"name": "underwrite", "input": {}}]},
        {"final_text": "Recorded, underwrote: WATCH. No human attention required."}
      ],
      "structured_outputs": {"SkepticOutput": {...}},
      "structured_output_refs": {"DocumentAnalysisOutput": "fixtures/expected/analysis_05_inspection_report.json"}
    }

* Each ``stream()`` call consumes the next turn, in order.
* ``input_ref`` is a path relative to the repository root; ``input_arg`` (optional) wraps the loaded
  JSON as a single named tool argument, which is what a tool taking a Pydantic model needs.
* Structured-output requests do **not** consume a turn: Strands registers a tool named after the
  Pydantic model, so when that name shows up in ``tool_specs`` (or in ``tool_choice``) the answer
  comes from ``structured_outputs``.
* ``structured_output_refs`` maps a model name to a JSON file (path relative to the repository root)
  holding that structured output, so a fixture can point straight at a golden file
  (``fixtures/expected/analysis_05_inspection_report.json``) instead of duplicating it inline. An
  inline ``structured_outputs`` entry for the same name wins.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel
from strands.models.model import BaseModelConfig, Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

REPO_ROOT = Path(__file__).resolve().parents[2]

EXHAUSTED_TEXT = "Scripted turns exhausted."


class ScriptError(ValueError):
    """The script file is missing or malformed."""


@dataclass
class ScriptedToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScriptedTurn:
    tool_calls: list[ScriptedToolCall] = field(default_factory=list)
    final_text: str = ""


def _load_input(raw: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    if "input_ref" in raw:
        ref = Path(raw["input_ref"])
        path = ref if ref.is_absolute() else repo_root / ref
        if not path.exists():
            raise ScriptError(f"input_ref does not exist: {path}")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        arg = raw.get("input_arg")
        if arg:
            return {arg: loaded}
        if not isinstance(loaded, dict):
            raise ScriptError(f"input_ref {path} must contain a JSON object (or set input_arg)")
        return loaded
    value = raw.get("input", {})
    if not isinstance(value, dict):
        raise ScriptError(f"tool call input must be an object, got {type(value).__name__}")
    return value


def load_script(path: str | Path, *, repo_root: Path | None = None) -> tuple[list[ScriptedTurn], dict[str, Any]]:
    """Parse a scripted-model JSON file into turns and structured outputs."""
    root = repo_root or REPO_ROOT
    script_path = Path(path)
    if not script_path.is_absolute():
        script_path = root / script_path
    if not script_path.exists():
        raise ScriptError(f"scripted model file not found: {script_path}")
    data = json.loads(script_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ScriptError(f"scripted model file must contain a JSON object: {script_path}")

    turns: list[ScriptedTurn] = []
    for index, raw_turn in enumerate(data.get("turns", [])):
        if not isinstance(raw_turn, dict):
            raise ScriptError(f"turns[{index}] must be an object in {script_path}")
        calls = [
            ScriptedToolCall(name=raw_call["name"], input=_load_input(raw_call, root))
            for raw_call in raw_turn.get("tool_calls", [])
        ]
        turns.append(ScriptedTurn(tool_calls=calls, final_text=str(raw_turn.get("final_text") or "")))

    structured = data.get("structured_outputs", {})
    if not isinstance(structured, dict):
        raise ScriptError(f"structured_outputs must be an object in {script_path}")

    refs = data.get("structured_output_refs", {})
    if not isinstance(refs, dict):
        raise ScriptError(f"structured_output_refs must be an object in {script_path}")
    resolved: dict[str, Any] = {}
    for name, ref in refs.items():
        ref_path = Path(ref)
        if not ref_path.is_absolute():
            ref_path = root / ref_path
        if not ref_path.exists():
            raise ScriptError(f"structured_output_refs[{name}] does not exist: {ref_path}")
        loaded = json.loads(ref_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ScriptError(f"structured_output_refs[{name}] must contain a JSON object: {ref_path}")
        resolved[str(name)] = loaded

    # An inline structured_outputs entry always wins over a reference to a file.
    return turns, {**resolved, **structured}


class ScriptedModel(Model):
    """Replays a fixture script as if it were a model. No network, no randomness."""

    class ScriptedModelConfig(BaseModelConfig, total=False):
        script: str | None

    def __init__(
        self,
        script: str | Path | None = None,
        *,
        repo_root: Path | None = None,
        **model_config: Any,
    ) -> None:
        self._repo_root = repo_root or REPO_ROOT
        self.config: dict[str, Any] = {"script": str(script) if script else None}
        self.config.update(model_config)
        if script:
            self._turns, self._structured_outputs = load_script(script, repo_root=self._repo_root)
        else:
            self._turns, self._structured_outputs = [], {}
        self._index = 0

    # -- config ------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        """Return the model configuration."""
        return self.config

    def update_config(self, **model_config: Any) -> None:
        """Update the model configuration with the provided arguments."""
        self.config.update(model_config)
        if "script" in model_config and model_config["script"]:
            self._turns, self._structured_outputs = load_script(
                model_config["script"], repo_root=self._repo_root
            )
            self._index = 0

    @property
    def turns_remaining(self) -> int:
        return max(0, len(self._turns) - self._index)

    def describe(self) -> str:
        return "scripted"

    # -- replay ------------------------------------------------------------

    def _structured_output_name(
        self, tool_specs: list[ToolSpec] | None, tool_choice: ToolChoice | None
    ) -> str | None:
        """The Pydantic model name Strands is asking for, if this is a structured-output request."""
        if tool_choice and "tool" in tool_choice:
            named = tool_choice["tool"].get("name")
            if named in self._structured_outputs:
                return str(named)
        for spec in tool_specs or []:
            if spec.get("name") in self._structured_outputs:
                return str(spec["name"])
        return None

    def _next_turn(self) -> ScriptedTurn:
        if self._index >= len(self._turns):
            logger.warning(
                "scripted model exhausted after %d turns (script=%s)", len(self._turns), self.config.get("script")
            )
            return ScriptedTurn(final_text=EXHAUSTED_TEXT)
        turn = self._turns[self._index]
        self._index += 1
        return turn

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        system_prompt_content: list[SystemContentBlock] | None = None,
        invocation_state: dict[str, Any] | None = None,
        cancel_signal: threading.Event | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Emit the next scripted turn as Strands stream events."""
        structured_name = self._structured_output_name(tool_specs, tool_choice)
        if structured_name is not None:
            turn = ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name=structured_name, input=self._structured_outputs[structured_name])
                ]
            )
        else:
            turn = self._next_turn()

        yield {"messageStart": {"role": "assistant"}}

        for call in turn.tool_calls:
            tool_use_id = f"scripted_{uuid.uuid4().hex[:16]}"
            yield {
                "contentBlockStart": {"start": {"toolUse": {"name": call.name, "toolUseId": tool_use_id}}}
            }
            yield {
                "contentBlockDelta": {
                    "delta": {"toolUse": {"input": json.dumps(call.input, default=str)}}
                }
            }
            yield {"contentBlockStop": {}}

        if turn.final_text:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn.final_text}}}
            yield {"contentBlockStop": {}}

        yield {"messageStop": {"stopReason": "tool_use" if turn.tool_calls else "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "metrics": {"latencyMs": 0},
            }
        }

    async def structured_output(
        self, output_model: type[T], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Return the scripted structured output for ``output_model``."""
        name = output_model.__name__
        if name not in self._structured_outputs:
            raise ScriptError(
                f"script {self.config.get('script')} has no structured_outputs entry for {name!r}"
            )
        yield {"output": output_model.model_validate(self._structured_outputs[name])}


__all__ = ["EXHAUSTED_TEXT", "ScriptError", "ScriptedModel", "ScriptedTurn", "load_script"]
