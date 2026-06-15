"""Tests for ``emit-skill-return`` / ``fetch-skill-return`` — the file-based
sub-skill return primitive (WS2 of the determinism migration).

The pair replaces returning via the Skill tool's chat-message envelope (which
fires an end-of-turn signal that stalls the parent skill) with an atomic file
write at ``<exp>/.hpc/_returns/<skill>.json``. The contract:

* emit-skill-return: reads staged file, validates against per-skill schema,
  atomic-renames to committed. Schema-fail leaves staged for debugging.
* fetch-skill-return: reads committed, re-validates, prints to stdout,
  deletes (unless --no-clear). Missing file → typed precondition_failed
  envelope with ``failure_features.error_class_raw == "skill_return_missing"``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli._helpers import parse_envelope, run_cli


@pytest.fixture(autouse=True)
def _isolate_breadcrumb_home(tmp_path, monkeypatch):
    """Isolate the committed-return breadcrumb per test so emit-skill-return
    doesn't write to the real ``~/.claude/hpc/_skill_return_dirs.json`` and leak
    a committed-return dir into the Stop-guard tests under xdist."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_bc_home"))


# A minimal valid Success envelope for each known skill — keyed by skill
# name so the round-trip / negative tests can parametrize over them. Each
# matches its per-skill schema's required-fields exactly.
_VALID_PAYLOADS: dict[str, dict] = {
    "hpc-classify-axis": {
        "ok": True,
        "skill": "hpc-classify-axis",
        "run_name": "forecast",
        "run_signature_sha": "abc123",
        "data_axis": {"kind": "independent"},
        "classified_by": "agent",
    },
    "hpc-wrap-entry-point": {
        "ok": True,
        "skill": "hpc-wrap-entry-point",
        "entry_point_kind": "register_run",
        "run_name": "forecast",
        "tasks_py_path": "/exp/.hpc/tasks.py",
        "interview_json_path": "/exp/interview.json",
        "total_tasks": 100,
        "cmd_sha": "deadbeef",
    },
    "hpc-build-executor": {
        "ok": True,
        "skill": "hpc-build-executor",
        "executor_path": "/exp/executors/forecast.py",
        "executor_type": "python_script",
        "executor_source": "template:starter",
    },
    "hpc-status": {
        "ok": True,
        "skill": "hpc-status",
        "run_id": "forecast-2026-01-01-abcd",
        "lifecycle_state": "complete",
    },
    "hpc-aggregate": {
        "ok": True,
        "skill": "hpc-aggregate",
        "run_id": "forecast-2026-01-01-abcd",
        "profile": "forecast",
        "stage": "final",
    },
}


def _stage(exp: Path, skill: str, payload: dict) -> Path:
    """Write the staged envelope to the canonical path and return it."""
    returns_dir = exp / ".hpc" / "_returns"
    returns_dir.mkdir(parents=True, exist_ok=True)
    staged = returns_dir / f"{skill}.staged.json"
    staged.write_text(json.dumps(payload), encoding="utf-8")
    return staged


# ─── round-trip happy path ─────────────────────────────────────────────────


@pytest.mark.parametrize("skill", list(_VALID_PAYLOADS.keys()))
def test_emit_then_fetch_round_trip(tmp_path: Path, skill: str) -> None:
    payload = _VALID_PAYLOADS[skill]
    _stage(tmp_path, skill, payload)

    rc, out, _ = run_cli("emit-skill-return", "--skill", skill, "--experiment-dir", str(tmp_path))
    assert rc == 0, out
    env = parse_envelope(out)
    assert env["ok"] is True
    assert env["data"]["skill"] == skill
    assert env["data"]["validated"] is True

    # Staged file is gone; committed file exists.
    returns_dir = tmp_path / ".hpc" / "_returns"
    assert not (returns_dir / f"{skill}.staged.json").exists()
    assert (returns_dir / f"{skill}.json").exists()

    # Fetch returns the envelope verbatim AND clears by default.
    rc, out, _ = run_cli("fetch-skill-return", "--skill", skill, "--experiment-dir", str(tmp_path))
    assert rc == 0, out
    fetched = json.loads(out.strip().splitlines()[0])
    # Comparing keys via canonical JSON sidesteps key ordering.
    assert fetched == {**payload}
    assert not (returns_dir / f"{skill}.json").exists()


def test_fetch_no_clear_keeps_file(tmp_path: Path) -> None:
    skill = "hpc-classify-axis"
    _stage(tmp_path, skill, _VALID_PAYLOADS[skill])
    run_cli("emit-skill-return", "--skill", skill, "--experiment-dir", str(tmp_path))

    rc, _, _ = run_cli(
        "fetch-skill-return",
        "--skill",
        skill,
        "--experiment-dir",
        str(tmp_path),
        "--no-clear",
    )
    assert rc == 0
    assert (tmp_path / ".hpc" / "_returns" / f"{skill}.json").exists()


# ─── schema validation on the emit side ────────────────────────────────────


def test_emit_rejects_missing_required_field(tmp_path: Path) -> None:
    # ``run_name`` is required by hpc-classify-axis.json's Success branch.
    bad = {"ok": True, "skill": "hpc-classify-axis"}
    staged = _stage(tmp_path, "hpc-classify-axis", bad)

    rc, out, _ = run_cli(
        "emit-skill-return",
        "--skill",
        "hpc-classify-axis",
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    # The remediation must name the schema path and the failing JSON path —
    # the recovery seam the contract docs promise.
    assert "hpc-classify-axis.json" in env["remediation"]
    assert "JSON path" in env["remediation"]
    # And the staged file must still be on disk for debugging.
    assert staged.exists()


def test_emit_rejects_unknown_skill(tmp_path: Path) -> None:
    rc, out, _ = run_cli(
        "emit-skill-return", "--skill", "not-a-skill", "--experiment-dir", str(tmp_path)
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["error_code"] == "spec_invalid"
    assert "not a registered sub-skill" in env["message"]


def test_emit_rejects_missing_staged_file(tmp_path: Path) -> None:
    rc, out, _ = run_cli(
        "emit-skill-return",
        "--skill",
        "hpc-classify-axis",
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["error_code"] == "precondition_failed"
    assert "staged.json" in env["message"]


def test_emit_rejects_malformed_json(tmp_path: Path) -> None:
    returns_dir = tmp_path / ".hpc" / "_returns"
    returns_dir.mkdir(parents=True)
    (returns_dir / "hpc-classify-axis.staged.json").write_text("not json", encoding="utf-8")

    rc, out, _ = run_cli(
        "emit-skill-return",
        "--skill",
        "hpc-classify-axis",
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["error_code"] == "spec_invalid"
    assert "not valid JSON" in env["message"]


# ─── fetch error paths ─────────────────────────────────────────────────────


def test_fetch_missing_emits_typed_skill_return_missing(tmp_path: Path) -> None:
    """The parent skill branches on this exact failure_features payload —
    pin it so a future error-feature refactor doesn't silently drop the
    typed signal."""
    rc, out, _ = run_cli(
        "fetch-skill-return",
        "--skill",
        "hpc-status",
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 1
    env = parse_envelope(out)
    assert env["error_code"] == "precondition_failed"
    assert env["failure_features"]["error_class_raw"] == "skill_return_missing"
    # Remediation names the staged sibling — the recovery seam.
    assert "staged.json" in env["remediation"]


def test_fetch_rejects_committed_envelope_that_no_longer_matches(tmp_path: Path) -> None:
    # Hand-write a committed envelope that bypassed the emit-side validator
    # (simulates a stale envelope from a prior schema version, or hand-edit).
    returns_dir = tmp_path / ".hpc" / "_returns"
    returns_dir.mkdir(parents=True)
    (returns_dir / "hpc-classify-axis.json").write_text(
        json.dumps({"ok": True, "skill": "hpc-classify-axis", "stale": "shape"}),
        encoding="utf-8",
    )

    rc, out, _ = run_cli(
        "fetch-skill-return",
        "--skill",
        "hpc-classify-axis",
        "--experiment-dir",
        str(tmp_path),
    )
    # Exit 3 (internal) — the file got past the emit-side gate (hand-edited
    # or a schema bump) so the corruption is on the framework side, not a
    # caller-supplied input. The category in the envelope must agree.
    assert rc == 3
    env = parse_envelope(out)
    assert env["error_code"] == "spec_invalid"
    assert env["category"] == "internal"
    assert "no longer matches" in env["message"]


# ─── known-skills / schemas coverage pin ───────────────────────────────────


def test_every_known_skill_has_a_schema() -> None:
    """The CLI's _KNOWN_SKILLS list and the schemas/skill_returns/*.json
    files must stay lock-step — a skill that registers without a schema
    crashes ``emit-skill-return`` with ``internal``, which the round-trip
    test would not catch in isolation. Pin the invariant here."""
    from importlib.resources import files

    from hpc_agent.cli.skill_returns import _KNOWN_SKILLS

    pkg = files("hpc_agent.schemas") / "skill_returns"
    on_disk = {p.name for p in pkg.iterdir() if p.name.endswith(".json")}
    expected = {f"{name}.json" for name in _KNOWN_SKILLS}
    assert on_disk == expected, (
        f"_KNOWN_SKILLS and schemas/skill_returns/*.json must match. "
        f"In _KNOWN_SKILLS but no schema: {expected - on_disk}; "
        f"on disk but not in _KNOWN_SKILLS: {on_disk - expected}."
    )
