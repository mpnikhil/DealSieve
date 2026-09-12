"""ScriptedModel replays fixtures deterministically, including structured output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from dealsieve.models.scripted import EXHAUSTED_TEXT, ScriptedModel, ScriptError, load_script

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "fixtures" / "scripted"


async def collect(model: ScriptedModel, **kwargs: Any) -> list[dict[str, Any]]:
    return [event async for event in model.stream(**kwargs)]


def messages():
    return [{"role": "user", "content": [{"text": "process"}]}]


def tool_calls_in(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for event in events:
        start = event.get("contentBlockStart", {}).get("start", {})
        if "toolUse" in start:
            name = start["toolUse"]["name"]
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "toolUse" in delta:
            calls.append((name, json.loads(delta["toolUse"]["input"])))
    return calls


def texts_in(events: list[dict[str, Any]]) -> list[str]:
    return [
        e["contentBlockDelta"]["delta"]["text"]
        for e in events
        if "text" in e.get("contentBlockDelta", {}).get("delta", {})
    ]


def write_script(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- replay


async def test_turns_are_consumed_in_order(tmp_path: Path):
    script = write_script(
        tmp_path,
        {
            "turns": [
                {"tool_calls": [{"name": "record_claims", "input": {"claims": {"asking_price": 1}}}]},
                {"tool_calls": [{"name": "underwrite", "input": {}}]},
                {"final_text": "WATCH. No human attention required."},
            ]
        },
    )
    model = ScriptedModel(script)
    assert model.turns_remaining == 3

    first = await collect(model, messages=messages())
    assert tool_calls_in(first) == [("record_claims", {"claims": {"asking_price": 1}})]
    assert first[-2]["messageStop"]["stopReason"] == "tool_use"

    second = await collect(model, messages=messages())
    assert tool_calls_in(second) == [("underwrite", {})]

    third = await collect(model, messages=messages())
    assert tool_calls_in(third) == []
    assert texts_in(third) == ["WATCH. No human attention required."]
    assert third[-2]["messageStop"]["stopReason"] == "end_turn"
    assert model.turns_remaining == 0


async def test_exhausted_script_ends_the_turn_rather_than_looping(tmp_path: Path):
    model = ScriptedModel(write_script(tmp_path, {"turns": [{"final_text": "done"}]}))
    await collect(model, messages=messages())
    extra = await collect(model, messages=messages())
    assert texts_in(extra) == [EXHAUSTED_TEXT]
    assert extra[-2]["messageStop"]["stopReason"] == "end_turn"


async def test_input_ref_is_resolved_relative_to_the_repo_root(tmp_path: Path):
    referenced = tmp_path / "claims.json"
    referenced.write_text(json.dumps({"asking_price": 1250000}), encoding="utf-8")
    script = write_script(
        tmp_path,
        {
            "turns": [
                {
                    "tool_calls": [
                        {
                            "name": "record_claims",
                            "input_ref": str(referenced),
                            "input_arg": "claims",
                        }
                    ]
                }
            ]
        },
    )
    events = await collect(ScriptedModel(script), messages=messages())
    assert tool_calls_in(events) == [("record_claims", {"claims": {"asking_price": 1250000}})]


def test_a_missing_input_ref_fails_loudly(tmp_path: Path):
    script = write_script(
        tmp_path, {"turns": [{"tool_calls": [{"name": "x", "input_ref": "nope/missing.json"}]}]}
    )
    with pytest.raises(ScriptError):
        ScriptedModel(script)


def test_a_missing_script_fails_loudly():
    with pytest.raises(ScriptError):
        ScriptedModel("fixtures/scripted/does_not_exist.json")


# --------------------------------------------------------------------------- structured output


class SkepticOutput(BaseModel):
    verdict: str
    summary: str = ""


async def test_a_structured_output_tool_spec_is_answered_without_consuming_a_turn(tmp_path: Path):
    script = write_script(
        tmp_path,
        {
            "turns": [{"final_text": "unused"}],
            "structured_outputs": {"SkepticOutput": {"verdict": "reject", "summary": "no"}},
        },
    )
    model = ScriptedModel(script)
    events = await collect(
        model,
        messages=messages(),
        tool_specs=[
            {
                "name": "SkepticOutput",
                "description": "structured output",
                "inputSchema": {"json": {"type": "object"}},
            }
        ],
    )
    assert tool_calls_in(events) == [("SkepticOutput", {"verdict": "reject", "summary": "no"})]
    assert model.turns_remaining == 1, "structured output must not eat a scripted turn"


async def test_tool_choice_can_name_the_structured_output_tool(tmp_path: Path):
    script = write_script(
        tmp_path,
        {"turns": [], "structured_outputs": {"SkepticOutput": {"verdict": "proceed", "summary": "ok"}}},
    )
    events = await collect(
        ScriptedModel(script),
        messages=messages(),
        tool_specs=[],
        tool_choice={"tool": {"name": "SkepticOutput"}},
    )
    assert tool_calls_in(events) == [("SkepticOutput", {"verdict": "proceed", "summary": "ok"})]


async def test_structured_output_method_validates_into_the_model(tmp_path: Path):
    script = write_script(
        tmp_path,
        {"turns": [], "structured_outputs": {"SkepticOutput": {"verdict": "proceed", "summary": "ok"}}},
    )
    model = ScriptedModel(script)
    events = [e async for e in model.structured_output(SkepticOutput, messages())]
    assert isinstance(events[-1]["output"], SkepticOutput)
    assert events[-1]["output"].verdict == "proceed"


async def test_structured_output_without_a_scripted_answer_raises(tmp_path: Path):
    model = ScriptedModel(write_script(tmp_path, {"turns": []}))
    with pytest.raises(ScriptError):
        [e async for e in model.structured_output(SkepticOutput, messages())]


# --------------------------------------------------------------------------- the shipped fixtures


@pytest.mark.parametrize(
    "name",
    [
        "01_initial_offer",
        "02_price_drop",
        "03_structural_single_tenant",
        "04_obvious_economic_failure",
        "05_inspection_report",
    ],
)
def test_every_shipped_script_loads_and_references_real_claims(name: str):
    turns, structured = load_script(SCRIPTS / f"{name}.json")
    assert turns, f"{name} has no turns"
    first = turns[0].tool_calls
    assert [c.name for c in first] == ["record_claims"]
    assert "claims" in first[0].input, "record_claims takes a `claims` argument"
    assert first[0].input["claims"], "the referenced claims fixture must not be empty"
    assert turns[-1].final_text, f"{name} must end with a one-line summary"
    assert isinstance(structured, dict)


def test_the_price_drop_script_runs_the_full_review_procedure():
    turns, structured = load_script(SCRIPTS / "02_price_drop.json")
    assert [c.name for turn in turns for c in turn.tool_calls] == [
        "record_claims",
        "underwrite",
        "request_skeptic_review",
        "request_diligence",
        "notify_human",
    ]

    items = next(
        c.input["items"] for turn in turns for c in turn.tool_calls if c.name == "request_diligence"
    )
    assert len(items) == 3
    joined = " ".join(item["topic"] + " " + item["question"] for item in items).lower()
    assert "roof" in joined and "phase i" in joined and "cam reconciliation" in joined

    skeptic = structured["SkepticOutput"]
    assert skeptic["verdict"] == "proceed_with_questions"
    missing = {c["topic"].lower() for c in skeptic["concerns"] if c["evidence_status"] == "missing"}
    assert {"roof age", "phase i environmental", "cam reconciliation"} <= missing
    weak = {c["topic"].lower() for c in skeptic["concerns"] if c["evidence_status"] == "weak"}
    assert any("rollover" in topic for topic in weak)


def test_the_quiet_scripts_never_reach_for_a_human():
    for name in ("01_initial_offer", "03_structural_single_tenant", "04_obvious_economic_failure"):
        turns, _ = load_script(SCRIPTS / f"{name}.json")
        called = [c.name for turn in turns for c in turn.tool_calls]
        assert called == ["record_claims", "underwrite"], name


def test_the_inspection_script_runs_the_fell_below_procedure():
    turns, structured = load_script(SCRIPTS / "05_inspection_report.json")
    assert [c.name for turn in turns for c in turn.tool_calls] == [
        "record_claims",
        "analyze_document",
        "underwrite",
        "request_price_adjustment",
        "notify_human",
    ]
    analysis = structured["DocumentAnalysisOutput"]
    assert analysis["images_reviewed"] == 3
    assert any("roof" in item["item"].lower() for item in analysis["capex_items"])
