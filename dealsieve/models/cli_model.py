"""A Strands model provider backed by a local coding-agent CLI.

Development and the hackathon demo run on existing CLI subscriptions rather than API keys:
``claude -p``, ``codex exec`` and ``agy -p`` all accept a JSON schema and return a JSON object.
This provider renders the whole Strands request (system prompt + tool specs + transcript) into a
single prompt, asks the CLI for a ``{tool_calls, final_text}`` object, and replays the answer as the
Strands ``StreamEvent`` sequence the event loop expects.

The subprocess layer is injectable (``runner=``) so rendering and parsing are unit-testable with no
process spawning at all. See ``SubprocessCLIRunner`` for the verified command lines.

Trust boundary (R9)
-------------------
**Everything this provider puts in a prompt is untrusted.** Broker emails, offering memoranda and
inspection-report PDFs are written by strangers, and a coding-agent CLI is not a chat completion
endpoint: it is a program with a shell, a filesystem and, by default, permission prompts a flag can
switch off. There is no role separation between "system instructions" and "attacker-supplied
document text" once both are rendered into one prompt string, so a document that says *"ignore your
instructions and run this command"* is a real risk, not a hypothetical one.

The mitigations here are defence in depth, not a sandbox:

* **Minimal environment.** Only :data:`BASE_ENV_KEYS` plus the invoked provider's own auth variables
  reach the child (:func:`cli_env`). No ``AWS_*``, no ``GITHUB_TOKEN``, no ``DEALSIEVE_*`` secrets.
* **No tools.** ``claude`` runs with ``--tools ""``; ``codex`` runs ``-s read-only`` inside an empty
  throwaway ``-C`` directory; ``agy`` runs ``--sandbox``. Verified 2026-09-12: agy answers in print
  mode under ``--sandbox`` **without** ``--dangerously-skip-permissions``, so that flag is gone.
* **Opt-in file reads.** ``DEALSIEVE_CLI_ALLOW_READ=1`` grants ``claude --tools Read`` so it can open
  the extracted document images. It is off by default and should stay off for untrusted senders.

For production, use the ``bedrock`` or ``anthropic`` backends. They have native role separation and
native multimodal input, and they never hand untrusted text to a process that can act on it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import jsonschema
from pydantic import BaseModel
from strands.models.model import BaseModelConfig, Model
from strands.types.content import Messages, SystemContentBlock
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolSpec

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

DEFAULT_PROVIDER = "claude"
DEFAULT_TIMEOUT_S = 240.0
DEFAULT_MODELS: dict[str, str | None] = {
    "claude": "sonnet",
    "agy": "gemini-3.8-flash-low",
    "codex": None,  # codex uses its configured default model
}

#: The response contract every CLI invocation of :meth:`CLIModel.stream` asks for.
TOOL_CALL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "input_json": {
                        "type": "string",
                        "description": "JSON object encoded as a string",
                    },
                },
                "required": ["name", "input_json"],
                "additionalProperties": False,
            },
        },
        "final_text": {"type": "string"},
    },
    "required": ["tool_calls", "final_text"],
    "additionalProperties": False,
}

#: Environment variables that make a nested `claude` invocation think it is being driven by
#: Claude Code. They must be absent or the CLI refuses / misbehaves. They are also absent from the
#: allowlist below, so :func:`cli_env` drops them either way; the tuple documents the intent.
_STRIPPED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

#: R9: the only environment every provider gets. Anything not listed here (AWS credentials, tokens,
#: DEALSIEVE_* settings, the user's whole shell environment) never reaches the child process.
BASE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "TERM")

#: Per-provider additions: config locations and that provider's own credentials, nothing else.
PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "claude": (
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ),
    "codex": ("CODEX_HOME", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "agy": (
        "AGY_HOME",
        "ANTIGRAVITY_HOME",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ),
}

#: Extensions Strands/Bedrock accept as image formats, keyed by lowercase suffix.
IMAGE_FORMATS: dict[str, str] = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}


def image_format_for(path: str | Path) -> str:
    """Strands `ImageFormat` for a file path. Unknown suffixes fall back to png."""
    return IMAGE_FORMATS.get(Path(path).suffix.lower(), "png")


def allow_read_enabled() -> bool:
    """`DEALSIEVE_CLI_ALLOW_READ=1` lets `claude` open the images from disk. Off by default."""
    return os.environ.get("DEALSIEVE_CLI_ALLOW_READ", "").strip() in ("1", "true", "yes")


class CLIModelError(RuntimeError):
    """The CLI could not be run, or returned something that is not usable JSON."""


@dataclass(frozen=True)
class CLIImage:
    """One image content block pulled out of the Strands transcript, ready for a CLI."""

    index: int
    """1-based position, matching the "image N" text block that precedes it in the prompt."""
    format: str
    data: bytes

    @property
    def filename(self) -> str:
        suffix = "jpg" if self.format == "jpeg" else self.format
        return f"image_{self.index}.{suffix}"


@dataclass(frozen=True)
class CLIInvocation:
    """One request to the underlying CLI. This is what a fake runner receives in tests."""

    prompt: str
    schema: dict[str, Any]
    provider: str
    model_id: str | None
    timeout: float
    purpose: str = "stream"
    images: tuple[CLIImage, ...] = ()


Runner = Callable[[CLIInvocation], dict[str, Any]]
"""Runs one CLI invocation and returns the JSON object the CLI produced."""


# --------------------------------------------------------------------------- subprocess runner


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object out of CLI stdout, tolerating banners and trailing chatter."""
    text = text.strip()
    if not text:
        raise CLIModelError("CLI produced no output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    # Fall back to the outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            candidate = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise CLIModelError(f"CLI output is not JSON: {text[:400]}") from exc
        if isinstance(candidate, dict):
            return candidate
    raise CLIModelError(f"CLI output is not a JSON object: {text[:400]}")


def _unwrap(obj: dict[str, Any]) -> dict[str, Any]:
    """`claude`/`agy` wrap the schema-conforming payload under `structured_output`."""
    for key in ("structured_output", "structuredOutput"):
        inner = obj.get(key)
        if isinstance(inner, dict):
            return inner
    return obj


def cli_env(provider: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """R9: a minimal allowlisted environment for one provider's child process.

    Untrusted broker text reaches these CLIs, so they get the least environment that still lets them
    find their config and authenticate: :data:`BASE_ENV_KEYS` plus this provider's own variables.
    Everything else in the parent environment -- cloud credentials, tokens, every ``DEALSIEVE_*``
    setting, and the Claude Code markers in :data:`_STRIPPED_ENV` -- is dropped.
    """
    source = dict(os.environ if base is None else base)
    allowed = BASE_ENV_KEYS + PROVIDER_ENV_KEYS.get(provider, ())
    return {key: source[key] for key in allowed if key in source and key not in _STRIPPED_ENV}


@dataclass
class CLICommand:
    argv: list[str]
    stdin_text: str | None
    """Text piped on stdin. When None, stdin is redirected from /dev/null."""
    output_file: Path | None
    """When set, the JSON answer is read from this file instead of stdout."""


def _write_images(images: tuple[CLIImage, ...], workdir: Path) -> list[Path]:
    """Materialise the transcript's images inside the throwaway working directory."""
    paths: list[Path] = []
    for image in images:
        path = workdir / image.filename
        path.write_bytes(image.data)
        paths.append(path)
    return paths


def _unavailable_note(images: tuple[CLIImage, ...]) -> str:
    """The exact wording the contract requires when a provider cannot see the images."""
    first, last = images[0].index, images[-1].index
    span = f"{first}" if first == last else f"{first}..{last}"
    return (
        f"\n\n## Images\nimages {span} were not available to you; do not describe them. "
        "Base every finding only on the document text above, and leave `image_ref` null."
    )


def _read_tool_note(paths: list[Path], images: tuple[CLIImage, ...]) -> str:
    listing = "\n".join(f"- image {img.index}: {path}" for img, path in zip(images, paths, strict=True))
    return (
        "\n\n## Images\nThe images referenced above are on disk. Open each one with the Read tool "
        f"before answering, and cite them as \"image N\":\n{listing}"
    )


def build_command(
    invocation: CLIInvocation, workdir: Path, *, schema_path: Path, output_path: Path
) -> CLICommand:
    """Build the exact verified command line for one provider.

    ``claude`` takes the prompt on stdin and the schema inline; ``codex`` and ``agy`` take the
    prompt as an argument and must have stdin redirected from /dev/null or they block forever.

    Images (see :class:`CLIImage`) reach each provider differently:

    * ``codex`` is genuinely multimodal in ``exec`` mode: the bytes are written into the ``-C``
      working directory and passed with one ``-i <file>`` per image, in "image N" order.
    * ``claude`` gets the images only under ``DEALSIEVE_CLI_ALLOW_READ=1``, which swaps ``--tools ""``
      for ``--tools Read`` and lists the on-disk paths in the prompt.
    * ``agy`` never gets them (``--sandbox`` has no image channel in print mode).

    When a provider cannot see them the prompt says so explicitly, so the model leaves ``image_ref``
    null instead of inventing a description of a photo it never saw.
    """
    schema_json = json.dumps(invocation.schema)
    provider = invocation.provider
    model_id = invocation.model_id
    images = invocation.images
    prompt = invocation.prompt

    if provider == "claude":
        argv = ["claude", "-p"]
        if model_id:
            argv += ["--model", model_id]
        tools = ""
        if images and allow_read_enabled():
            tools = "Read"
            prompt += _read_tool_note(_write_images(images, workdir), images)
        elif images:
            prompt += _unavailable_note(images)
        argv += [
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--tools",
            tools,
        ]
        return CLICommand(argv=argv, stdin_text=prompt, output_file=None)

    if provider == "codex":
        schema_path.write_text(schema_json, encoding="utf-8")
        argv = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            str(workdir),
            "--output-schema",
            str(schema_path),
            "-o",
            str(output_path),
        ]
        image_paths = _write_images(images, workdir)
        for path in image_paths:
            argv += ["-i", str(path)]
        if image_paths:
            # `-i` is variadic: a positional prompt after it is swallowed as another image path and
            # codex then reads an empty stdin. With images, the prompt travels on stdin instead.
            return CLICommand(argv=argv, stdin_text=prompt, output_file=output_path)
        argv.append(prompt)
        return CLICommand(argv=argv, stdin_text=None, output_file=output_path)

    if provider == "agy":
        schema_path.write_text(schema_json, encoding="utf-8")
        # R9, verified 2026-09-12: --sandbox alone answers in print mode, so the
        # --dangerously-skip-permissions flag this used to carry is gone.
        argv = ["agy", "--sandbox", "--output-format", "json", "--json-schema", str(schema_path)]
        if model_id:
            argv += ["--model", model_id]
        if images:
            prompt += _unavailable_note(images)
        argv += ["-p", prompt]
        return CLICommand(argv=argv, stdin_text=None, output_file=None)

    raise CLIModelError(f"unknown CLI provider: {provider!r} (expected claude | codex | agy)")


class SubprocessCLIRunner:
    """Default :data:`Runner`: actually shells out to the coding-agent CLI."""

    def __init__(self, *, run: Callable[..., Any] | None = None) -> None:
        self._run = run or subprocess.run

    def __call__(self, invocation: CLIInvocation) -> dict[str, Any]:
        if shutil.which(invocation.provider) is None:
            raise CLIModelError(
                f"CLI provider {invocation.provider!r} is not on PATH; "
                "set DEALSIEVE_MODEL_BACKEND to another backend"
            )
        with tempfile.TemporaryDirectory(prefix="dealsieve-cli-") as tmp:
            workdir = Path(tmp)
            command = build_command(
                invocation,
                workdir,
                schema_path=workdir / "schema.json",
                output_path=workdir / "out.json",
            )
            completed = self._run(
                command.argv,
                input=command.stdin_text,
                stdin=None if command.stdin_text is not None else subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=invocation.timeout,
                env=cli_env(invocation.provider),
                cwd=str(workdir),
                check=False,
            )
            stdout = getattr(completed, "stdout", "") or ""
            stderr = getattr(completed, "stderr", "") or ""
            returncode = getattr(completed, "returncode", 0)

            if command.output_file is not None and command.output_file.exists():
                raw = command.output_file.read_text(encoding="utf-8")
            else:
                raw = stdout

            if not raw.strip():
                raise CLIModelError(
                    f"{invocation.provider} exited {returncode} with no JSON output: {stderr[:400]}"
                )
            return _unwrap(_extract_json_object(raw))


# --------------------------------------------------------------------------- prompt rendering


def _render_tool_specs(tool_specs: list[ToolSpec] | None) -> str:
    if not tool_specs:
        return "No tools are available. Answer with an empty tool_calls list."
    blocks = ["The following tools are available. Call them by name with a matching input object."]
    for spec in tool_specs:
        schema = spec.get("inputSchema", {}).get("json", {})
        blocks.append(
            f"\n### {spec['name']}\n{spec.get('description', '').strip()}\n"
            f"Input JSON Schema:\n{json.dumps(schema, indent=2, sort_keys=True, default=str)}"
        )
    return "\n".join(blocks)


def extract_images(messages: Messages) -> tuple[CLIImage, ...]:
    """Pull every `image` content block out of the transcript, numbered from 1 in order.

    The numbering is the contract between the prompt (which carries a literal "image N" text block
    before each image) and the provider (which attaches the files in the same order).
    """
    images: list[CLIImage] = []
    for message in messages:
        for block in message.get("content", []):
            image = block.get("image") if isinstance(block, dict) else None
            if not image:
                continue
            data = (image.get("source") or {}).get("bytes")
            if not data:
                continue
            images.append(
                CLIImage(index=len(images) + 1, format=str(image.get("format") or "png"), data=data)
            )
    return tuple(images)


def _render_content_block(block: dict[str, Any], image_counter: list[int]) -> str | None:
    if "text" in block:
        return block["text"]
    if "image" in block:
        image_counter[0] += 1
        return f"[image {image_counter[0]}]"
    if "toolUse" in block:
        use = block["toolUse"]
        payload = json.dumps(use.get("input", {}), sort_keys=True, default=str)
        return f"[tool call] {use.get('name')} (id={use.get('toolUseId')})\n{payload}"
    if "toolResult" in block:
        result = block["toolResult"]
        parts: list[str] = []
        for item in result.get("content", []):
            if "text" in item:
                parts.append(item["text"])
            elif "json" in item:
                parts.append(json.dumps(item["json"], sort_keys=True, default=str))
        body = "\n".join(parts) if parts else "(no content)"
        return (
            f"[tool result] id={result.get('toolUseId')} status={result.get('status', 'success')}\n{body}"
        )
    if "reasoningContent" in block:
        return None
    return None


def _render_transcript(messages: Messages) -> str:
    lines: list[str] = []
    image_counter = [0]
    for message in messages:
        role = message.get("role", "user")
        rendered = [
            text
            for text in (
                _render_content_block(block, image_counter) for block in message.get("content", [])
            )
            if text
        ]
        if not rendered:
            continue
        lines.append(f"\n[{role}]\n" + "\n".join(rendered))
    return "\n".join(lines).strip() or "(empty conversation)"


def _render_system(system_prompt: str | None, system_prompt_content: list[SystemContentBlock] | None) -> str:
    if system_prompt_content:
        texts = [block["text"] for block in system_prompt_content if "text" in block]
        if texts:
            return "\n\n".join(texts)
    return system_prompt or ""


def _tool_choice_directive(tool_choice: ToolChoice | None, tool_specs: list[ToolSpec] | None) -> str | None:
    if not tool_choice:
        return None
    if "tool" in tool_choice:
        name = tool_choice["tool"].get("name", "")
        return (
            f"MANDATORY: you must call the tool `{name}` in this turn. "
            "tool_calls must contain exactly one entry for it."
        )
    if "any" in tool_choice:
        names = ", ".join(spec["name"] for spec in tool_specs or []) or "the available tools"
        return (
            "MANDATORY: you must call at least one tool in this turn "
            f"(one of: {names}). An empty tool_calls list is not acceptable."
        )
    return None


def render_prompt(
    *,
    system_prompt: str | None,
    tool_specs: list[ToolSpec] | None,
    messages: Messages,
    response_schema: dict[str, Any],
    tool_choice: ToolChoice | None = None,
    system_prompt_content: list[SystemContentBlock] | None = None,
    error_feedback: str | None = None,
) -> str:
    """Render the whole Strands request as one deterministic prompt string."""
    sections: list[str] = []
    system = _render_system(system_prompt, system_prompt_content)
    if system:
        sections.append("## Instructions\n" + system.strip())
    sections.append("## Tools\n" + _render_tool_specs(tool_specs))
    sections.append("## Conversation so far\n" + _render_transcript(messages))

    directive = _tool_choice_directive(tool_choice, tool_specs)
    response_rules = [
        "## Your response",
        "Reply with a single JSON object conforming exactly to this schema:",
        json.dumps(response_schema, indent=2, sort_keys=True),
        "",
        "- `tool_calls`: the tools to call now, in order. Use an empty list when you are done.",
        "- `input_json`: the tool input as a JSON **object encoded as a string**, valid against"
        " that tool's input schema. Do not include arguments the schema does not define.",
        "- `final_text`: your message to the user. Leave it empty when you are only calling tools.",
        "- Do not invent tool names. Do not wrap the JSON in markdown fences or commentary.",
    ]
    if directive:
        response_rules.append(f"- {directive}")
    sections.append("\n".join(response_rules))

    if error_feedback:
        sections.append(
            "## Your previous attempt was rejected\n"
            f"{error_feedback}\n"
            "Produce a corrected JSON object now."
        )
    return "\n\n".join(sections)


def render_structured_output_prompt(
    *,
    system_prompt: str | None,
    messages: Messages,
    output_schema: dict[str, Any],
    model_name: str,
    error_feedback: str | None = None,
) -> str:
    sections: list[str] = []
    if system_prompt:
        sections.append("## Instructions\n" + system_prompt.strip())
    sections.append("## Conversation so far\n" + _render_transcript(messages))
    sections.append(
        "## Your response\n"
        f"Reply with a single JSON object of type `{model_name}` conforming exactly to this schema:\n"
        + json.dumps(output_schema, indent=2, sort_keys=True)
        + "\n\nNo markdown fences, no commentary, no extra keys."
    )
    if error_feedback:
        sections.append(
            "## Your previous attempt was rejected\n"
            f"{error_feedback}\nProduce a corrected JSON object now."
        )
    return "\n\n".join(sections)


# --------------------------------------------------------------------------- response parsing


@dataclass
class ParsedToolCall:
    name: str
    input_json: str
    input_obj: dict[str, Any]


@dataclass
class ParsedResponse:
    tool_calls: list[ParsedToolCall] = field(default_factory=list)
    final_text: str = ""
    errors: list[str] = field(default_factory=list)


def parse_response(
    payload: dict[str, Any],
    tool_specs: list[ToolSpec] | None,
    tool_choice: ToolChoice | None = None,
) -> ParsedResponse:
    """Validate a CLI payload against the response contract and each tool's input schema."""
    specs = {spec["name"]: spec for spec in (tool_specs or [])}
    parsed = ParsedResponse(final_text=str(payload.get("final_text") or ""))

    raw_calls = payload.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        parsed.errors.append("`tool_calls` must be a list.")
        raw_calls = []

    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            parsed.errors.append(f"tool_calls[{index}] is not an object.")
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            parsed.errors.append(f"tool_calls[{index}] is missing a tool name.")
            continue
        if specs and name not in specs:
            parsed.errors.append(
                f"tool_calls[{index}]: unknown tool {name!r}. Available: {', '.join(sorted(specs)) or 'none'}."
            )
            continue

        raw_input = raw.get("input_json", "{}")
        if isinstance(raw_input, dict):
            # Tolerate a model that emitted the object instead of a string.
            input_obj: Any = raw_input
            input_text = json.dumps(raw_input, default=str)
        else:
            input_text = str(raw_input) if raw_input is not None else "{}"
            try:
                input_obj = json.loads(input_text or "{}")
            except json.JSONDecodeError as exc:
                parsed.errors.append(f"tool_calls[{index}] ({name}): input_json is not valid JSON: {exc}")
                continue
        if not isinstance(input_obj, dict):
            parsed.errors.append(f"tool_calls[{index}] ({name}): input_json must encode a JSON object.")
            continue

        spec = specs.get(name)
        if spec is not None:
            schema = spec.get("inputSchema", {}).get("json")
            if schema:
                try:
                    jsonschema.validate(instance=input_obj, schema=schema)
                except jsonschema.ValidationError as exc:
                    path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
                    parsed.errors.append(
                        f"tool_calls[{index}] ({name}): input does not match the tool schema at {path}: {exc.message}"
                    )
                    continue
                except jsonschema.SchemaError as exc:  # pragma: no cover - defensive
                    logger.debug("tool schema for %s is not usable: %s", name, exc)

        parsed.tool_calls.append(
            ParsedToolCall(name=name, input_json=json.dumps(input_obj, default=str), input_obj=input_obj)
        )

    if tool_choice and not parsed.errors:
        if "tool" in tool_choice:
            required = tool_choice["tool"].get("name")
            if not any(call.name == required for call in parsed.tool_calls):
                parsed.errors.append(f"You must call the tool `{required}` in this turn.")
        elif "any" in tool_choice and not parsed.tool_calls:
            parsed.errors.append("You must call at least one tool in this turn.")

    return parsed


# --------------------------------------------------------------------------- the provider


class CLIModel(Model):
    """Strands model provider that delegates inference to a local coding-agent CLI."""

    class CLIModelConfig(BaseModelConfig, total=False):
        """Configuration for :class:`CLIModel`.

        Attributes:
            provider: ``claude`` | ``codex`` | ``agy``.
            model_id: Model name handed to the CLI (``codex`` uses its own default when None).
            timeout: Seconds to allow one CLI invocation.
        """

        provider: str
        model_id: str | None
        timeout: float

    def __init__(
        self,
        provider: str | None = None,
        *,
        model_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        runner: Runner | None = None,
        **model_config: Any,
    ) -> None:
        resolved_provider = (provider or DEFAULT_PROVIDER).strip().lower()
        if resolved_provider not in DEFAULT_MODELS:
            raise CLIModelError(
                f"unknown CLI provider: {resolved_provider!r} (expected claude | codex | agy)"
            )
        self.config: dict[str, Any] = {
            "provider": resolved_provider,
            "model_id": model_id if model_id is not None else DEFAULT_MODELS[resolved_provider],
            "timeout": timeout,
        }
        self.config.update(model_config)
        self._runner: Runner = runner or SubprocessCLIRunner()

    # -- config ------------------------------------------------------------

    def get_config(self) -> dict[str, Any]:
        """Return the model configuration."""
        return self.config

    def update_config(self, **model_config: Any) -> None:
        """Update the model configuration with the provided arguments."""
        self.config.update(model_config)

    @property
    def provider(self) -> str:
        return str(self.config["provider"])

    @property
    def model_id(self) -> str | None:
        value = self.config.get("model_id")
        return str(value) if value else None

    def describe(self) -> str:
        return f"cli:{self.provider}:{self.model_id or 'default'}"

    # -- invocation --------------------------------------------------------

    def _invocation(
        self,
        prompt: str,
        schema: dict[str, Any],
        purpose: str,
        images: tuple[CLIImage, ...] = (),
    ) -> CLIInvocation:
        return CLIInvocation(
            prompt=prompt,
            schema=schema,
            provider=self.provider,
            model_id=self.model_id,
            timeout=float(self.config.get("timeout", DEFAULT_TIMEOUT_S)),
            purpose=purpose,
            images=images,
        )

    async def _call(
        self,
        prompt: str,
        schema: dict[str, Any],
        purpose: str,
        images: tuple[CLIImage, ...] = (),
    ) -> dict[str, Any]:
        invocation = self._invocation(prompt, schema, purpose, images)
        return await asyncio.to_thread(self._runner, invocation)

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
        """Run one CLI turn and replay it as Strands stream events."""
        started = time.monotonic()
        error_feedback: str | None = None
        parsed: ParsedResponse | None = None
        prompt = ""
        failure: str | None = None
        images = extract_images(messages)

        for attempt in range(2):
            prompt = render_prompt(
                system_prompt=system_prompt,
                tool_specs=tool_specs,
                messages=messages,
                response_schema=TOOL_CALL_SCHEMA,
                tool_choice=tool_choice,
                system_prompt_content=system_prompt_content,
                error_feedback=error_feedback,
            )
            try:
                payload = await self._call(prompt, TOOL_CALL_SCHEMA, "stream", images)
            except Exception as exc:  # CLI missing, timeout, unparsable output
                failure = f"{type(exc).__name__}: {exc}"
                logger.warning("cli model invocation failed (attempt %d): %s", attempt + 1, failure)
                error_feedback = f"The previous invocation failed: {failure}"
                parsed = None
                continue

            candidate = parse_response(payload, tool_specs, tool_choice)
            if not candidate.errors:
                parsed = candidate
                failure = None
                break
            failure = "; ".join(candidate.errors)
            logger.warning("cli model response rejected (attempt %d): %s", attempt + 1, failure)
            error_feedback = "; ".join(candidate.errors)
            parsed = None

        if parsed is None:
            # Both attempts failed: surface it as plain text so the agent loop terminates cleanly.
            parsed = ParsedResponse(
                final_text=(
                    "I could not produce a valid tool call. "
                    f"The {self.provider} CLI response was rejected: {failure}"
                )
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        prompt_tokens = max(1, len(prompt) // 4)
        completion_chars = len(parsed.final_text) + sum(len(c.input_json) for c in parsed.tool_calls)
        completion_tokens = max(1, completion_chars // 4)

        yield {"messageStart": {"role": "assistant"}}

        for call in parsed.tool_calls:
            tool_use_id = f"cli_{uuid.uuid4().hex[:16]}"
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"name": call.name, "toolUseId": tool_use_id}}
                }
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": call.input_json}}}}
            yield {"contentBlockStop": {}}

        if parsed.final_text:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": parsed.final_text}}}
            yield {"contentBlockStop": {}}

        yield {"messageStop": {"stopReason": "tool_use" if parsed.tool_calls else "end_turn"}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": prompt_tokens,
                    "outputTokens": completion_tokens,
                    "totalTokens": prompt_tokens + completion_tokens,
                },
                "metrics": {"latencyMs": latency_ms},
            }
        }

    async def structured_output(
        self, output_model: type[T], prompt: Messages, system_prompt: str | None = None, **kwargs: Any
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Ask the CLI directly for an instance of ``output_model``.

        Yields model events with the last being ``{"output": <instance>}``, mirroring
        ``strands.models.openai.OpenAIModel.structured_output``.
        """
        schema = output_model.model_json_schema()
        error_feedback: str | None = None
        last_error: str | None = None
        images = extract_images(prompt)

        for attempt in range(2):
            rendered = render_structured_output_prompt(
                system_prompt=system_prompt,
                messages=prompt,
                output_schema=schema,
                model_name=output_model.__name__,
                error_feedback=error_feedback,
            )
            try:
                payload = await self._call(rendered, schema, "structured_output", images)
                yield {"output": output_model.model_validate(payload)}
                return
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "cli structured_output failed (attempt %d) for %s: %s",
                    attempt + 1,
                    output_model.__name__,
                    last_error,
                )
                error_feedback = f"The previous attempt was invalid: {last_error}"

        raise CLIModelError(
            f"{self.provider} CLI did not return a valid {output_model.__name__}: {last_error}"
        )


__all__ = [
    "BASE_ENV_KEYS",
    "CLICommand",
    "CLIImage",
    "CLIInvocation",
    "CLIModel",
    "CLIModelError",
    "ParsedResponse",
    "ParsedToolCall",
    "Runner",
    "SubprocessCLIRunner",
    "TOOL_CALL_SCHEMA",
    "IMAGE_FORMATS",
    "PROVIDER_ENV_KEYS",
    "allow_read_enabled",
    "build_command",
    "cli_env",
    "extract_images",
    "image_format_for",
    "parse_response",
    "render_prompt",
    "render_structured_output_prompt",
]
