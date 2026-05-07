"""Tests for ``runner.update_constraints``.

The primitive is SSH-bound; tests mock ``ssh_run`` at the OS boundary
(``infra.remote.subprocess.run``-equivalent), keeping the function
under test unchanged. Real cluster integration is out of scope for
unit tests.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from claude_hpc import errors
from claude_hpc._schema_models.actions.update_run_constraints import (
    UpdateRunConstraintsSpec,
)
from claude_hpc.runner.update_constraints import update_run_constraints
from claude_hpc.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


_RUN_ID = "20260101-000000-aaaaaaa"


def _seed_sidecar(tmp_path: Path, *, job_ids: list[str], features: list[str] | None = None) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=_RUN_ID,
        cmd_sha="a" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="b" * 64,
        job_ids=job_ids,
        constraints={"features": features} if features else None,
        # Normally written by submit_flow; the primitive expects it.
        extra={"ssh_target": "alice@cluster"},
    )
    # write_run_sidecar doesn't expose ssh_target as a top-level v2
    # field today; patch it in directly so the test models the sidecar
    # the primitive reads.
    target = tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json"
    data = json.loads(target.read_text())
    data["ssh_target"] = "alice@cluster"
    target.write_text(json.dumps(data, indent=2, sort_keys=True))


def _ok_cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ─── happy paths ──────────────────────────────────────────────────────


def test_set_features_runs_scontrol_for_each_job(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["12345", "12346"])
    with patch(
        "claude_hpc.infra.remote.ssh_run",
        return_value=_ok_cp(),
    ) as mock_ssh:
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100", "l40s"]),
        )

    assert out.job_ids_updated == ["12345", "12346"]
    assert out.job_ids_failed == []
    assert out.new_features == ["a100", "l40s"]
    # One scontrol invocation per job, with the right Features expr.
    assert mock_ssh.call_count == 2
    cmds = [call.args[0] for call in mock_ssh.call_args_list]
    assert all("scontrol update" in cmd for cmd in cmds)
    assert all("Features=a100|l40s" in cmd for cmd in cmds)


def test_add_features_extends_existing_set(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("claude_hpc.infra.remote.ssh_run", return_value=_ok_cp()):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, add_features=["l40s"]),
        )
    assert out.new_features == ["a100", "l40s"]


def test_add_features_dedupes(tmp_path: Path) -> None:
    """add_features=['a100'] when a100 already exists is a no-op (no
    duplicate in the new set), but the scontrol still runs."""
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("claude_hpc.infra.remote.ssh_run", return_value=_ok_cp()):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, add_features=["a100"]),
        )
    assert out.new_features == ["a100"]


def test_sidecar_features_persisted_on_success(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"], features=["a100"])
    with patch("claude_hpc.infra.remote.ssh_run", return_value=_ok_cp()):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["v100"]),
        )
    sidecar = json.loads((tmp_path / ".hpc" / "runs" / f"{_RUN_ID}.json").read_text())
    assert sidecar["constraints"]["features"] == ["v100"]


# ─── failure paths ─────────────────────────────────────────────────────


def test_partial_failure_reports_per_job(tmp_path: Path) -> None:
    """Some jobs succeed, some fail. Both lists are populated; the
    sidecar is updated when at least one job succeeded."""
    _seed_sidecar(tmp_path, job_ids=["1", "2"])
    responses = [_ok_cp(), _ok_cp(returncode=1, stderr="invalid feature")]
    with patch("claude_hpc.infra.remote.ssh_run", side_effect=responses):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == ["1"]
    assert out.job_ids_failed == ["2"]


def test_ssh_unreachable_marks_job_failed(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"])
    with patch("claude_hpc.infra.remote.ssh_run", side_effect=errors.SshUnreachable("nope")):
        out = update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )
    assert out.job_ids_updated == []
    assert out.job_ids_failed == ["1"]


# ─── spec invariants ──────────────────────────────────────────────────


def test_both_set_and_add_features_rejected(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"])
    with pytest.raises(errors.SpecInvalid, match="Pass exactly one"):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a"], add_features=["b"]),
        )


def test_neither_set_nor_add_features_rejected(tmp_path: Path) -> None:
    _seed_sidecar(tmp_path, job_ids=["1"])
    with pytest.raises(errors.SpecInvalid, match="at least one"):
        update_run_constraints(tmp_path, spec=UpdateRunConstraintsSpec(run_id=_RUN_ID))


def test_no_job_ids_in_sidecar_rejected(tmp_path: Path) -> None:
    """Sidecar without job_ids is half-baked (rsync/qsub failed before
    submit_and_record); refuse to update."""
    _seed_sidecar(tmp_path, job_ids=[])
    with pytest.raises(errors.SpecInvalid, match="no job_ids"):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100"]),
        )


def test_feature_with_shell_metachar_rejected(tmp_path: Path) -> None:
    """Defence against shell injection through the scontrol command."""
    _seed_sidecar(tmp_path, job_ids=["1"])
    with pytest.raises(errors.SpecInvalid, match="contains characters outside"):
        update_run_constraints(
            tmp_path,
            spec=UpdateRunConstraintsSpec(run_id=_RUN_ID, set_features=["a100;rm -rf /"]),
        )
