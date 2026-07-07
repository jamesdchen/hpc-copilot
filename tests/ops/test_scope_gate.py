"""Unit tests for the scope gate (rigor-primitives T3).

:func:`hpc_agent.ops.scope_gate.assert_scopes_unlocked` is the precondition on
the one reduction seam: a run whose caller-attached evidence-scope is locked
must not be reduced. It is a pure LOCAL read (no SSH) and is fail-safe — a
scope-less or sidecar-less run passes so it can never false-trip.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.ops.scope_gate import assert_scopes_unlocked
from hpc_agent.state import scopes
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "20260706-120000-aaa"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _write_sidecar(
    experiment: Path, *, run_id: str = _RUN_ID, scope_tags: list[str] | None
) -> None:
    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.11.0",
        submitted_at="2026-07-06T12:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/task-{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        wave_map={},
        remote_path="/remote",
        scopes=scope_tags,
    )


def test_locked_scope_refuses_reduction(experiment: Path) -> None:
    """A locked tag → ScopeLocked naming the tag, the lock ts, and the one exit."""
    _write_sidecar(experiment, scope_tags=["holdout"])
    rec = scopes.record_lock(experiment, "holdout", reason="embargo until preregistration")

    with pytest.raises(errors.ScopeLocked) as ei:
        assert_scopes_unlocked(experiment, _RUN_ID)

    msg = str(ei.value)
    assert "holdout" in msg  # names the tag
    assert "scope-unlock" in msg  # names the ONE exit
    assert rec["ts"] in msg  # names the lock record's ts
    # Reuses the precondition_failed envelope code (no wire-enum widening).
    assert ei.value.error_code == "precondition_failed"
    # Remediation follows the house style and points at append-decision.
    assert "append-decision" in (ei.value.remediation or "")


def test_unlocked_and_untagged_runs_pass(experiment: Path) -> None:
    """Unlocked tag passes; scope-less sidecar passes; missing sidecar passes."""
    # (a) A tag locked then UNLOCKED reads unlocked → passes.
    _write_sidecar(experiment, scope_tags=["holdout"])
    scopes.record_lock(experiment, "holdout", reason="embargo")
    append_decision(
        experiment,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-unlock",
        response="y",
        resolved={"scope_action": "unlock"},
    )
    assert scopes.is_scope_locked(experiment, "holdout") is False
    assert_scopes_unlocked(experiment, _RUN_ID)  # no raise

    # (b) A never-locked tag also passes.
    _write_sidecar(experiment, run_id="20260706-120000-bbb", scope_tags=["free-scope"])
    assert_scopes_unlocked(experiment, "20260706-120000-bbb")

    # (c) A scope-less sidecar (scopes=None) passes.
    _write_sidecar(experiment, run_id="20260706-120000-ccc", scope_tags=None)
    assert_scopes_unlocked(experiment, "20260706-120000-ccc")

    # (d) A MISSING sidecar passes — never a false trip.
    assert_scopes_unlocked(experiment, "20260706-120000-nope")
