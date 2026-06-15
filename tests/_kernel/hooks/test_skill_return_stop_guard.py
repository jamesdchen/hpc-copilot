"""Tests for the ``skill_return_stop_guard`` Stop hook.

The guard is harness-mediated: Claude Code runs it when the agent is about to
end its turn, feeding the Stop payload on stdin. If a known sub-skill's
committed return envelope sits unfetched under ``<cwd>/.hpc/_returns/``, the
guard blocks the stop with a reason instructing the agent to
``fetch-skill-return`` and continue the parent skill (the empirical
2026-06-10 failure: ``hpc-wrap-entry-point`` emitted its return, the turn
ended, a human had to type "keep going").

These pin the pure core (:func:`build_hook_output`), the loop-safety
``stop_hook_active`` no-op, the committed-vs-staged distinction, and the
stdin/stdout ``main`` wrapper's fail-open contract.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import skill_return_stop_guard as guard
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path, _staged_path

_KNOWN_SKILL = "hpc-wrap-entry-point"
_SAMPLE_ENVELOPE = {"ok": True, "skill": _KNOWN_SKILL, "entry_point_kind": "register_run"}


@pytest.fixture(autouse=True)
def _isolate_breadcrumb_home(tmp_path, monkeypatch):
    """Isolate the skill-return breadcrumb (``<home>/_skill_return_dirs.json``)
    per test. The breadcrumb lives under ``_current_homedir()`` (``~/.claude/hpc``,
    HPC_JOURNAL_DIR-overridable); without isolation a sibling emit test on
    another xdist worker leaks a committed-return dir into these no-op
    assertions. Tests that set HPC_JOURNAL_DIR themselves override this default.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_bc_home"))


def _commit(exp: Path, skill: str, envelope: dict) -> Path:
    committed = _committed_path(exp, skill)
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(json.dumps(envelope), encoding="utf-8")
    return committed


def _payload(exp: Path, *, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
        "cwd": str(exp),
    }


# ─── happy path: pending envelope → block ───────────────────────────────────


def test_pending_envelope_blocks_the_stop(tmp_path: Path) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)

    out = guard.build_hook_output(_payload(tmp_path))

    assert out is not None
    assert out["decision"] == "block"
    assert _KNOWN_SKILL in out["reason"]
    assert f"fetch-skill-return --skill {_KNOWN_SKILL}" in out["reason"]
    # The reason must point at the dir the envelope actually lives under.
    assert "--experiment-dir" in out["reason"]


def test_block_does_not_delete_the_envelope(tmp_path: Path) -> None:
    """The guard observes; only the agent's fetch clears the file."""
    committed = _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    guard.build_hook_output(_payload(tmp_path))
    assert committed.exists()


def test_multiple_pending_envelopes_all_named(tmp_path: Path) -> None:
    _commit(tmp_path, "hpc-wrap-entry-point", _SAMPLE_ENVELOPE)
    _commit(tmp_path, "hpc-classify-axis", {"ok": True})

    out = guard.build_hook_output(_payload(tmp_path))

    assert out is not None
    assert "hpc-wrap-entry-point" in out["reason"]
    assert "hpc-classify-axis" in out["reason"]


@pytest.mark.parametrize("skill", list(_KNOWN_SKILLS))
def test_every_known_skill_triggers_the_guard(tmp_path: Path, skill: str) -> None:
    _commit(tmp_path, skill, {"ok": True, "skill": skill})
    out = guard.build_hook_output(_payload(tmp_path))
    assert out is not None and skill in out["reason"]


# ─── experiment_dir != cwd: breadcrumb scan ─────────────────────────────────


def test_blocks_on_return_under_noncwd_experiment_dir(tmp_path: Path, monkeypatch) -> None:
    """The emit ran with --experiment-dir != cwd; the Stop payload has only
    cwd. The guard must still fire by scanning the emitter's breadcrumb, and
    point the fetch at the experiment dir the envelope actually lives in."""
    from hpc_agent.cli.skill_returns import record_return_dir

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    exp = tmp_path / "experiments" / "run-a"
    launch_cwd = tmp_path / "elsewhere"
    launch_cwd.mkdir(parents=True, exist_ok=True)
    _commit(exp, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    record_return_dir(exp)

    out = guard.build_hook_output(_payload(launch_cwd))

    assert out is not None
    assert out["decision"] == "block"
    assert _KNOWN_SKILL in out["reason"]
    # The fetch must target the experiment dir, not the (empty) launch cwd.
    assert exp.resolve().as_posix() in out["reason"]


def test_breadcrumb_roundtrip_prunes_missing_dirs(tmp_path: Path, monkeypatch) -> None:
    from hpc_agent.cli.skill_returns import known_return_dirs, record_return_dir

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    real = tmp_path / "exp-real"
    real.mkdir()
    gone = tmp_path / "exp-gone"
    gone.mkdir()
    record_return_dir(gone)
    record_return_dir(real)  # most-recent-first
    gone.rmdir()

    dirs = [d.resolve() for d in known_return_dirs()]
    assert real.resolve() in dirs
    assert gone.resolve() not in dirs  # pruned: no longer exists
    # Most-recent-first ordering preserved for surviving dirs.
    assert dirs[0] == real.resolve()


# ─── loop safety ────────────────────────────────────────────────────────────


def test_stop_hook_active_is_noop_even_with_pending(tmp_path: Path) -> None:
    """A stop that is already a hook-forced continuation must pass through —
    blocking it again would loop the harness."""
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    assert guard.build_hook_output(_payload(tmp_path, stop_hook_active=True)) is None


# ─── defensive no-ops ───────────────────────────────────────────────────────


def test_no_pending_envelope_is_noop(tmp_path: Path) -> None:
    assert guard.build_hook_output(_payload(tmp_path)) is None


def test_staged_only_envelope_is_noop(tmp_path: Path) -> None:
    """A staged (uncommitted) envelope is the emitter's WIP — not pending."""
    staged = _staged_path(tmp_path, _KNOWN_SKILL)
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(_SAMPLE_ENVELOPE), encoding="utf-8")
    assert guard.build_hook_output(_payload(tmp_path)) is None


def test_non_hpc_directory_is_noop(tmp_path: Path) -> None:
    """No .hpc/_returns at all (any non-hpc project) → clean pass-through."""
    assert guard.build_hook_output(_payload(tmp_path / "plain-project")) is None


def test_malformed_payload_is_noop() -> None:
    for bad in (None, [], "string", 42):
        assert guard.build_hook_output(bad) is None


def test_absent_cwd_falls_back_to_process_cwd(tmp_path: Path, monkeypatch) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    monkeypatch.chdir(tmp_path)
    payload = _payload(tmp_path)
    del payload["cwd"]
    assert guard.build_hook_output(payload) is not None


# ─── pending_skill_returns unit ─────────────────────────────────────────────


def test_pending_skill_returns_order_and_content(tmp_path: Path) -> None:
    _commit(tmp_path, "hpc-status", {"ok": True})
    _commit(tmp_path, "hpc-wrap-entry-point", {"ok": True})
    # Order follows _KNOWN_SKILLS, not commit order.
    assert guard.pending_skill_returns(tmp_path) == ["hpc-wrap-entry-point", "hpc-status"]


def test_pending_skill_returns_empty_dir(tmp_path: Path) -> None:
    assert guard.pending_skill_returns(tmp_path) == []


# ─── main() stdin/stdout wrapper ────────────────────────────────────────────


def _run_main(monkeypatch, stdin_text: str) -> tuple[int, str]:
    out_buf = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setattr("sys.stdout", out_buf)
    rc = guard.main([])
    return rc, out_buf.getvalue()


def test_main_pending_prints_block_decision(tmp_path: Path, monkeypatch) -> None:
    _commit(tmp_path, _KNOWN_SKILL, _SAMPLE_ENVELOPE)
    rc, out = _run_main(monkeypatch, json.dumps(_payload(tmp_path)))
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["decision"] == "block"
    assert _KNOWN_SKILL in parsed["reason"]


def test_main_no_pending_prints_nothing(tmp_path: Path, monkeypatch) -> None:
    rc, out = _run_main(monkeypatch, json.dumps(_payload(tmp_path)))
    assert rc == 0
    assert out == ""


def test_main_malformed_stdin_is_clean_noop(monkeypatch) -> None:
    rc, out = _run_main(monkeypatch, "{not json")
    assert rc == 0
    assert out == ""


def test_main_empty_stdin_is_clean_noop(monkeypatch) -> None:
    rc, out = _run_main(monkeypatch, "")
    assert rc == 0
    assert out == ""


def test_main_never_raises_on_core_error(tmp_path: Path, monkeypatch) -> None:
    """A bug inside the core degrades to letting the stop proceed, never a
    non-zero exit that could wedge the harness."""

    def _boom(_payload):
        raise RuntimeError("simulated core failure")

    monkeypatch.setattr(guard, "build_hook_output", _boom)
    rc, out = _run_main(monkeypatch, json.dumps(_payload(tmp_path)))
    assert rc == 0
    assert out == ""
