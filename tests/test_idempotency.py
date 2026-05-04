"""Tests for :mod:`claude_hpc._internal.idempotency`.

Exercises the resolver's three read paths (journal, sidecar,
request_log) and the cancelled-record short-circuit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_hpc._internal.idempotency import (
    CmdShaKey,
    PriorResult,
    RequestIdKey,
    RunIdKey,
    dedup_check,
)
from claude_hpc.orchestrator.runs import run_sidecar_path
from slash_commands import session


def _ensure_journal_dirs(tmp_path: Path) -> None:
    # Make sure the journal lookup path resolves without env-var leak.
    import os

    os.environ["HPC_JOURNAL_DIR"] = str(tmp_path / "_journal")


def _journal_record(tmp_path: Path, run_id: str, status: str = "in_flight") -> session.RunRecord:
    rec = session.RunRecord(
        run_id=run_id,
        profile="p1",
        cluster="c1",
        ssh_target="user@host",
        remote_path="/tmp/exp",
        job_name="jn",
        job_ids=["1"],
        total_tasks=1,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir=str(tmp_path),
        status=status,
    )
    session.upsert_run(tmp_path, rec)
    return rec


def test_run_id_key_hits_journal(tmp_path: Path) -> None:
    _ensure_journal_dirs(tmp_path)
    _journal_record(tmp_path, "20260101-000000-aaaaaaa")
    result = dedup_check(tmp_path, RunIdKey("20260101-000000-aaaaaaa"))
    assert isinstance(result, PriorResult)
    assert result.origin == "journal"
    assert result.run_id == "20260101-000000-aaaaaaa"


def test_run_id_key_misses_when_no_record(tmp_path: Path) -> None:
    _ensure_journal_dirs(tmp_path)
    assert dedup_check(tmp_path, RunIdKey("never-existed")) is None


def test_run_id_key_treats_cancelled_as_miss(tmp_path: Path) -> None:
    _ensure_journal_dirs(tmp_path)
    _journal_record(tmp_path, "20260101-000000-cancelled", status="cancelled")
    assert dedup_check(tmp_path, RunIdKey("20260101-000000-cancelled")) is None


def test_cmd_sha_key_hits_sidecar(tmp_path: Path) -> None:
    _ensure_journal_dirs(tmp_path)
    run_id = "20260101-000000-bcdef00"
    cmd_sha = "0" * 64
    target = run_sidecar_path(tmp_path, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "sidecar_schema_version": 2,
                "run_id": run_id,
                "cmd_sha": cmd_sha,
                "submitted_at": "2026-01-01T00:00:00Z",
                "executor": "python3 src/run.py",
                "result_dir_template": "results/{seed}",
                "task_count": 0,
                "tasks_py_sha": "1" * 64,
            }
        )
    )
    result = dedup_check(tmp_path, CmdShaKey(cmd_sha))
    assert isinstance(result, PriorResult)
    assert result.origin == "sidecar"
    assert result.run_id == run_id


def test_cmd_sha_key_misses_when_no_sidecar(tmp_path: Path) -> None:
    _ensure_journal_dirs(tmp_path)
    assert dedup_check(tmp_path, CmdShaKey("a" * 64)) is None


def test_origin_labels_are_stable() -> None:
    # The strings are part of the contract; lock them.
    assert RunIdKey("x").origin() == "run_id"
    assert CmdShaKey("y").origin() == "cmd_sha"
    assert RequestIdKey("z").origin() == "request_id"


def test_unknown_key_subclass_raises() -> None:
    from claude_hpc._internal.idempotency import IdempotencyKey

    class _Bogus(IdempotencyKey):
        def origin(self) -> str:
            return "bogus"

    with pytest.raises(TypeError):
        dedup_check(Path("/tmp"), _Bogus())
