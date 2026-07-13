"""Run-#12 finding 17 leg 3: a detached worker that exits non-zero leaves a
failure terminal for its (run_id, block) — silence is never an outcome."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.cli.dispatch import _record_detached_failure_terminal
from hpc_agent.state.block_terminal import read_terminal, record_terminal


@pytest.fixture
def worker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "run-x")
    monkeypatch.setenv("HPC_DETACHED_BLOCK", "submit-s2")
    monkeypatch.chdir(tmp_path)  # the spawn contract: worker cwd = experiment dir
    return tmp_path


def test_nonzero_exit_records_failure_terminal(worker_env: Path) -> None:
    _record_detached_failure_terminal(1)
    rec = read_terminal(worker_env, "run-x", "submit-s2")
    assert rec is not None
    assert rec["result"]["ok"] is False
    assert rec["result"]["detached_failure"] is True
    assert rec["result"]["exit_code"] == 1
    assert "re-invoke" in rec["result"]["message"]


def test_never_overwrites_a_real_terminal(worker_env: Path) -> None:
    record_terminal(
        worker_env,
        run_id="run-x",
        block="submit-s2",
        cmd_sha="abc",
        result_dump={"ok": True, "real": True},
    )
    _record_detached_failure_terminal(1)
    rec = read_terminal(worker_env, "run-x", "submit-s2")
    assert rec is not None
    assert rec["result"] == {"ok": True, "real": True}


def test_noop_outside_a_marked_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_DETACHED_RUN_ID", raising=False)
    monkeypatch.delenv("HPC_DETACHED_BLOCK", raising=False)
    monkeypatch.chdir(tmp_path)
    _record_detached_failure_terminal(1)
    assert read_terminal(tmp_path, "run-x", "submit-s2") is None
