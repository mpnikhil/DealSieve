"""CLIModel: prompt rendering, response parsing, tool_choice, retry, structured output.

Everything here runs with an injected fake runner. No subprocess is ever spawned.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from dealsieve.models.cli_model import (
    TOOL_CALL_SCHEMA,
    CLIInvocation,
    CLIModel,
    CLIModelError,
    build_command,
    cli_env,
    parse_response,
    render_prompt,
)


class FakeRunner:
    """Returns queued payloads and records every invocation it was given."""

    def __init__(self, *payloads: Any) -> None:
        self.payloads = list(payloads)
        self.invocations: list[CLIInvocation] = []

    def __call__(self, invocation: CLIInvocation) -> dict[str, Any]:
        self.invocations.append(invocation)
        if not self.payloads:
            raise AssertionError("fake runner called more times than it has payloads")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


async def collect(model: CLIModel, **kwargs: Any) -> list[dict[str, Any]]:
    return [event async for event in model.stream(**kwargs)]


def messages(text: str = "Process this email."):
    return [{"role": "user", "content": [{"text": text}]}]


# --------------------------------------------------------------------------- rendering


def test_render_prompt_includes_system_tools_transcript_and_schema(tool_specs):
    prompt = render_prompt(
        system_prompt="You are the acquisition analyst.",
        tool_specs=tool_specs,
        messages=messages("Broker email body"),
        response_schema=TOOL_CALL_SCHEMA,
    )
    assert "You are the acquisition analyst." in prompt
    assert "### underwrite" in prompt and "### notify_human" in prompt
    assert '"note"' in prompt, "each tool's input schema must be rendered"
    assert "Broker email body" in prompt
    assert '"final_text"' in prompt and '"tool_calls"' in prompt
    assert "MANDATORY" not in prompt


def test_render_prompt_renders_tool_use_and_tool_result_blocks(tool_specs):
    transcript = [
        {"role": "user", "content": [{"text": "go"}]},
        {
            "role": "assistant",
            "content": [{"toolUse": {"name": "underwrite", "toolUseId": "t1", "input": {}}}],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "t1",
                        "status": "success",
                        "content": [{"text": '{"status": "WATCH"}'}],
                    }
                }
            ],
        },
    ]
    prompt = render_prompt(
        system_prompt=None,
        tool_specs=tool_specs,
        messages=transcript,
        response_schema=TOOL_CALL_SCHEMA,
    )
    assert "[tool call] underwrite (id=t1)" in prompt
    assert "[tool result] id=t1 status=success" in prompt
    assert '{"status": "WATCH"}' in prompt


@pytest.mark.parametrize(
    ("choice", "needle"),
    [
        ({"any": {}}, "at least one tool"),
        ({"tool": {"name": "notify_human"}}, "`notify_human`"),
    ],
)
def test_render_prompt_states_the_tool_choice_mandate(tool_specs, choice, needle):
    prompt = render_prompt(
        system_prompt=None,
        tool_specs=tool_specs,
        messages=messages(),
        response_schema=TOOL_CALL_SCHEMA,
        tool_choice=choice,
    )
    assert "MANDATORY" in prompt and needle in prompt


# --------------------------------------------------------------------------- parsing


def test_parse_response_accepts_a_valid_tool_call(tool_specs):
    parsed = parse_response(
        {"tool_calls": [{"name": "notify_human", "input_json": '{"note": "crossed"}'}], "final_text": ""},
        tool_specs,
    )
    assert not parsed.errors
    assert [c.name for c in parsed.tool_calls] == ["notify_human"]
    assert parsed.tool_calls[0].input_obj == {"note": "crossed"}


def test_parse_response_rejects_unparsable_input_json(tool_specs):
    parsed = parse_response(
        {"tool_calls": [{"name": "notify_human", "input_json": "{not json"}], "final_text": ""},
        tool_specs,
    )
    assert parsed.errors and "not valid JSON" in parsed.errors[0]
    assert parsed.tool_calls == []


def test_parse_response_rejects_input_that_violates_the_tool_schema(tool_specs):
    parsed = parse_response(
        {"tool_calls": [{"name": "notify_human", "input_json": '{"note": 42}'}], "final_text": ""},
        tool_specs,
    )
    assert parsed.errors and "does not match the tool schema" in parsed.errors[0]


def test_parse_response_rejects_an_unknown_tool(tool_specs):
    parsed = parse_response(
        {"tool_calls": [{"name": "delete_everything", "input_json": "{}"}], "final_text": ""},
        tool_specs,
    )
    assert parsed.errors and "unknown tool" in parsed.errors[0]


def test_parse_response_enforces_tool_choice(tool_specs):
    empty = {"tool_calls": [], "final_text": "I would rather chat."}
    assert parse_response(empty, tool_specs, {"any": {}}).errors
    assert parse_response(empty, tool_specs, {"tool": {"name": "underwrite"}}).errors
    assert not parse_response(empty, tool_specs).errors


# --------------------------------------------------------------------------- streaming


async def test_stream_emits_the_tool_use_event_sequence(tool_specs):
    runner = FakeRunner(
        {"tool_calls": [{"name": "underwrite", "input_json": "{}"}], "final_text": ""}
    )
    model = CLIModel("claude", runner=runner)
    events = await collect(model, messages=messages(), tool_specs=tool_specs, system_prompt="sys")

    assert events[0] == {"messageStart": {"role": "assistant"}}
    start = events[1]["contentBlockStart"]["start"]["toolUse"]
    assert start["name"] == "underwrite" and start["toolUseId"]
    assert events[2]["contentBlockDelta"]["delta"]["toolUse"]["input"] == "{}"
    assert "contentBlockStop" in events[3]
    assert events[4]["messageStop"]["stopReason"] == "tool_use"
    usage = events[5]["metadata"]["usage"]
    assert usage["totalTokens"] == usage["inputTokens"] + usage["outputTokens"]


async def test_stream_emits_a_text_block_and_end_turn(tool_specs):
    runner = FakeRunner({"tool_calls": [], "final_text": "WATCH. No human attention required."})
    model = CLIModel("claude", runner=runner)
    events = await collect(model, messages=messages(), tool_specs=tool_specs)

    texts = [e["contentBlockDelta"]["delta"]["text"] for e in events if "contentBlockDelta" in e]
    assert texts == ["WATCH. No human attention required."]
    assert events[-2]["messageStop"]["stopReason"] == "end_turn"


async def test_stream_emits_tool_calls_then_text_when_both_are_present(tool_specs):
    runner = FakeRunner(
        {
            "tool_calls": [{"name": "notify_human", "input_json": '{"note": "crossed"}'}],
            "final_text": "Notified you.",
        }
    )
    events = await collect(
        CLIModel("claude", runner=runner), messages=messages(), tool_specs=tool_specs
    )
    kinds = [next(iter(event)) for event in events]
    assert kinds == [
        "messageStart",
        "contentBlockStart",
        "contentBlockDelta",
        "contentBlockStop",
        "contentBlockStart",
        "contentBlockDelta",
        "contentBlockStop",
        "messageStop",
        "metadata",
    ]
    assert events[-2]["messageStop"]["stopReason"] == "tool_use"


async def test_stream_retries_once_with_the_error_then_succeeds(tool_specs):
    runner = FakeRunner(
        {"tool_calls": [{"name": "notify_human", "input_json": "{oops"}], "final_text": ""},
        {"tool_calls": [{"name": "notify_human", "input_json": '{"note": "ok"}'}], "final_text": ""},
    )
    events = await collect(
        CLIModel("claude", runner=runner), messages=messages(), tool_specs=tool_specs
    )

    assert len(runner.invocations) == 2
    assert "previous attempt was rejected" in runner.invocations[1].prompt
    assert "not valid JSON" in runner.invocations[1].prompt
    assert events[-2]["messageStop"]["stopReason"] == "tool_use"
    assert events[2]["contentBlockDelta"]["delta"]["toolUse"]["input"] == '{"note": "ok"}'


async def test_stream_falls_back_to_end_turn_text_after_two_failures(tool_specs):
    bad = {"tool_calls": [{"name": "nope", "input_json": "{}"}], "final_text": ""}
    runner = FakeRunner(bad, bad)
    events = await collect(
        CLIModel("claude", runner=runner), messages=messages(), tool_specs=tool_specs
    )

    assert len(runner.invocations) == 2
    assert events[-2]["messageStop"]["stopReason"] == "end_turn"
    text = events[2]["contentBlockDelta"]["delta"]["text"]
    assert "could not produce a valid tool call" in text
    assert "unknown tool" in text


async def test_stream_survives_a_runner_that_raises(tool_specs):
    runner = FakeRunner(CLIModelError("claude exited 1"), CLIModelError("claude exited 1"))
    events = await collect(
        CLIModel("claude", runner=runner), messages=messages(), tool_specs=tool_specs
    )
    assert events[-2]["messageStop"]["stopReason"] == "end_turn"
    assert "claude exited 1" in events[2]["contentBlockDelta"]["delta"]["text"]


async def test_stream_forwards_the_response_schema_to_the_cli(tool_specs):
    runner = FakeRunner({"tool_calls": [], "final_text": "done"})
    await collect(CLIModel("claude", runner=runner), messages=messages(), tool_specs=tool_specs)
    assert runner.invocations[0].schema == TOOL_CALL_SCHEMA
    assert runner.invocations[0].provider == "claude"
    assert runner.invocations[0].model_id == "sonnet"


# --------------------------------------------------------------------------- structured output


class Verdict(BaseModel):
    verdict: str
    reasons: list[str] = []


async def test_structured_output_yields_a_validated_instance_last():
    runner = FakeRunner({"verdict": "proceed_with_questions", "reasons": ["roof age"]})
    model = CLIModel("agy", runner=runner)
    events = [e async for e in model.structured_output(Verdict, messages("Review this."))]

    assert len(events) == 1
    output = events[-1]["output"]
    assert isinstance(output, Verdict) and output.verdict == "proceed_with_questions"
    assert runner.invocations[0].schema == Verdict.model_json_schema()
    assert "Verdict" in runner.invocations[0].prompt


async def test_structured_output_retries_once_then_raises():
    runner = FakeRunner({"nope": True}, {"nope": True})
    model = CLIModel("claude", runner=runner)
    with pytest.raises(CLIModelError):
        [e async for e in model.structured_output(Verdict, messages())]
    assert len(runner.invocations) == 2
    assert "previous attempt was invalid" in runner.invocations[1].prompt


# --------------------------------------------------------------------------- config & commands


def test_get_and_update_config():
    model = CLIModel("codex")
    assert model.get_config()["provider"] == "codex"
    assert model.get_config()["model_id"] is None
    model.update_config(model_id="gpt-5-codex", timeout=30)
    assert model.get_config()["model_id"] == "gpt-5-codex"
    assert model.get_config()["timeout"] == 30
    assert model.describe() == "cli:codex:gpt-5-codex"


def test_unknown_provider_is_rejected():
    with pytest.raises(CLIModelError):
        CLIModel("gemini-cli")


def test_cli_env_strips_the_claude_code_markers():
    env = cli_env(
        "claude", {"PATH": "/bin", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    )
    assert env == {"PATH": "/bin"}


def _invocation(provider: str, model_id: str | None) -> CLIInvocation:
    return CLIInvocation(
        prompt="PROMPT", schema={"type": "object"}, provider=provider, model_id=model_id, timeout=240.0
    )


def test_build_command_claude_pipes_the_prompt_on_stdin(tmp_path: Path):
    command = build_command(
        _invocation("claude", "sonnet"),
        tmp_path,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
    )
    assert command.stdin_text == "PROMPT"
    assert command.output_file is None
    assert command.argv[:4] == ["claude", "-p", "--model", "sonnet"]
    assert "--no-session-persistence" in command.argv
    assert command.argv[command.argv.index("--output-format") + 1] == "json"
    assert json.loads(command.argv[command.argv.index("--json-schema") + 1]) == {"type": "object"}
    assert command.argv[command.argv.index("--tools") + 1] == ""
    assert "PROMPT" not in command.argv


def test_build_command_codex_writes_a_schema_file_and_reads_out_json(tmp_path: Path):
    schema_path = tmp_path / "schema.json"
    out_path = tmp_path / "out.json"
    command = build_command(
        _invocation("codex", None), tmp_path, schema_path=schema_path, output_path=out_path
    )
    assert command.stdin_text is None, "codex must have stdin redirected from /dev/null"
    assert command.output_file == out_path
    assert command.argv[:2] == ["codex", "exec"]
    assert "--skip-git-repo-check" in command.argv
    assert command.argv[command.argv.index("-s") + 1] == "read-only"
    assert command.argv[command.argv.index("-C") + 1] == str(tmp_path)
    assert command.argv[command.argv.index("--output-schema") + 1] == str(schema_path)
    assert command.argv[-1] == "PROMPT"
    assert json.loads(schema_path.read_text()) == {"type": "object"}


def test_build_command_agy_passes_the_prompt_as_an_argument(tmp_path: Path):
    command = build_command(
        _invocation("agy", "gemini-3.8-flash-low"),
        tmp_path,
        schema_path=tmp_path / "schema.json",
        output_path=tmp_path / "out.json",
    )
    assert command.stdin_text is None
    assert command.argv[0] == "agy"
    assert "--sandbox" in command.argv
    assert "--dangerously-skip-permissions" not in command.argv
    assert command.argv[command.argv.index("-p") + 1] == "PROMPT"
    assert command.argv[command.argv.index("--model") + 1] == "gemini-3.8-flash-low"


def test_subprocess_runner_unwraps_structured_output_and_hides_banners(monkeypatch):
    from dealsieve.models import cli_model

    monkeypatch.setattr(cli_model.shutil, "which", lambda name: f"/usr/bin/{name}")
    captured: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, 0, stdout='Welcome!\n{"structured_output": {"final_text": "hi", "tool_calls": []}}\n', stderr=""
        )

    runner = cli_model.SubprocessCLIRunner(run=fake_run)
    payload = runner(_invocation("claude", "sonnet"))

    assert payload == {"final_text": "hi", "tool_calls": []}
    assert captured["kwargs"]["input"] == "PROMPT"
    assert "CLAUDECODE" not in captured["kwargs"]["env"]


def test_subprocess_runner_reports_a_missing_cli(monkeypatch):
    from dealsieve.models import cli_model

    monkeypatch.setattr(cli_model.shutil, "which", lambda name: None)
    with pytest.raises(CLIModelError, match="not on PATH"):
        cli_model.SubprocessCLIRunner()(_invocation("codex", None))
