"""build-spawn-prompt generator + spawn_guard PreToolUse hook."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hpc_agent.atoms.spawn_prompt import (
    WORKFLOW_SKILLS,
    build_spawn_prompt,
    render_spawn_prompt,
)
from hpc_agent.hooks.spawn_guard import evaluate


def test_render_spawn_prompt_is_deterministic() -> None:
    kwargs = dict(workflow="submit", experiment_dir="/exp", fields={"run": "f"})
    assert render_spawn_prompt(**kwargs) == render_spawn_prompt(**kwargs)


def test_render_names_the_workflow_skill() -> None:
    for workflow, skill in WORKFLOW_SKILLS.items():
        prompt = render_spawn_prompt(workflow=workflow, experiment_dir="/exp", fields={})
        assert skill in prompt
        assert "load-context" in prompt


def test_build_spawn_prompt_writes_content_addressed_spec(tmp_path: Path) -> None:
    out = build_spawn_prompt(experiment_dir=tmp_path, workflow="submit", fields={"cluster": "sge1"})
    sha = out["sha256"]
    assert out["spawn_ref"] == f"spec://{sha}"

    spec_path = Path(out["spec_path"])
    assert spec_path == tmp_path / ".hpc" / "spawn" / f"{sha}.json"
    # The filename IS the hash of the file's exact bytes.
    assert hashlib.sha256(spec_path.read_bytes()).hexdigest() == sha

    record = json.loads(spec_path.read_text())
    assert record["workflow"] == "submit"
    assert record["fields"] == {"cluster": "sge1"}
    assert "hpc-submit" in record["prompt"]


def test_build_spawn_prompt_rejects_unknown_workflow(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown workflow"):
        build_spawn_prompt(experiment_dir=tmp_path, workflow="nope", fields={})


def test_hook_passes_through_non_spec_prompts() -> None:
    event = {"tool_input": {"prompt": "go explore the repo", "subagent_type": "Explore"}}
    assert evaluate(event) is None


def test_hook_ignores_calls_without_a_string_prompt() -> None:
    assert evaluate({"tool_input": {"subagent_type": "Explore"}}) is None
    assert evaluate({"tool_input": {"prompt": 42}}) is None
    assert evaluate({}) is None


def test_hook_rewrites_a_valid_spec_ref(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = build_spawn_prompt(experiment_dir=tmp_path, workflow="status", fields={"run_id": "r1"})
    event = {"tool_input": {"prompt": out["spawn_ref"], "subagent_type": "general"}}
    decision = evaluate(event)
    assert decision is not None
    inner = decision["hookSpecificOutput"]
    assert inner["permissionDecision"] == "allow"
    # The model-authored prompt is replaced by the canonical generated text.
    rewritten = inner["updatedInput"]["prompt"]
    assert rewritten != out["spawn_ref"]
    assert "hpc-status" in rewritten
    # Other tool_input keys survive the rewrite.
    assert inner["updatedInput"]["subagent_type"] == "general"


def test_hook_denies_a_missing_spec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    sha = "0" * 64
    decision = evaluate({"tool_input": {"prompt": f"spec://{sha}"}})
    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_denies_a_tampered_spec(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    out = build_spawn_prompt(experiment_dir=tmp_path, workflow="aggregate", fields={})
    # Edit the file after generation — its hash no longer matches the name.
    spec_path = Path(out["spec_path"])
    record = json.loads(spec_path.read_text())
    record["prompt"] += " (smuggled instruction)"
    spec_path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))

    decision = evaluate({"tool_input": {"prompt": out["spawn_ref"]}})
    assert decision is not None
    inner = decision["hookSpecificOutput"]
    assert inner["permissionDecision"] == "deny"
    assert "integrity" in inner["permissionDecisionReason"]


def test_cli_build_spawn_prompt_smoke(tmp_path: Path) -> None:
    from tests.cli._helpers import parse_envelope, run_cli

    rc, out, _ = run_cli(
        "build-spawn-prompt",
        "--experiment-dir",
        str(tmp_path),
        "--workflow",
        "campaign",
        "--fields-json",
        '{"campaign_id": "q1"}',
    )
    assert rc == 0
    env = parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["spawn_ref"].startswith("spec://")
    assert (tmp_path / ".hpc" / "spawn").is_dir()


def test_cli_build_spawn_prompt_rejects_bad_json(tmp_path: Path) -> None:
    from tests.cli._helpers import parse_envelope, run_cli

    rc, out, _ = run_cli(
        "build-spawn-prompt",
        "--experiment-dir",
        str(tmp_path),
        "--workflow",
        "submit",
        "--fields-json",
        "not-json",
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
