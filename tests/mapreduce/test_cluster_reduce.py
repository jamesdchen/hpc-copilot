"""Tests for ``claude_hpc.atoms.cluster_reduce``.

The atom SSHes into the cluster, runs the user's reducer, and pulls
just its single output. We mock ``ssh_run`` and ``rsync_pull`` to
drive the state machine; the on-disk JSON parsing path is exercised
by writing the file to the local tempdir before the rsync mock
returns.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.session import RunRecord

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path / "home_hpc")
    return tmp_path / "home_hpc"


def _seed(experiment: Path, run_id: str = "r1") -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="user@h",
        remote_path="/x",
        job_name="p",
        job_ids=["job_42"],
        total_tasks=10,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    session.upsert_run(experiment, record)
    return record


def _seed_sidecar(
    experiment: Path,
    run_id: str = "r1",
    aggregate_cmd: str | None = None,
) -> None:
    from claude_hpc.state.runs import write_run_sidecar

    write_run_sidecar(
        experiment,
        run_id=run_id,
        cmd_sha="0" * 64,
        claude_hpc_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
        aggregate_defaults=({"aggregate_cmd": aggregate_cmd} if aggregate_cmd else None),
    )


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Mimic subprocess.CompletedProcess shape used by ssh_run / rsync_pull."""
    from subprocess import CompletedProcess

    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _stage_pulled_output(local_dir: Path, basename: str, payload: dict) -> None:
    """Simulate rsync_pull landing the reducer's JSON output locally."""
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / basename).write_text(json.dumps(payload), encoding="utf-8")


def test_happy_path_runs_reducer_and_returns_parsed_json(
    tmp_path: Path, journal_home: Path
) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    local_dir = tmp_path / "_aggregated" / "r1"

    def fake_rsync_pull(*args, **kwargs):
        # Simulate the reducer's output landing locally.
        _stage_pulled_output(local_dir, "r1.json", {"qlike": 0.42, "n": 1200})
        return _completed(returncode=0)

    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", return_value=_completed(returncode=0)),
        mock.patch("claude_hpc.infra.remote.rsync_pull", side_effect=fake_rsync_pull),
    ):
        out = cluster_reduce(
            tmp_path,
            run_id="r1",
            aggregate_cmd="python -m my.qlike_reducer",
        )
    assert out["ok"] is True
    assert out["run_id"] == "r1"
    assert out["reduced"] == {"qlike": 0.42, "n": 1200}
    assert out["output_path_remote"] == "_aggregated/r1.json"


def test_aggregate_cmd_falls_back_to_sidecar(tmp_path: Path, journal_home: Path) -> None:
    """When aggregate_cmd is None, the primitive reads aggregate_defaults
    from the sidecar."""
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    _seed_sidecar(tmp_path, aggregate_cmd="python -m sidecar.reducer")
    local_dir = tmp_path / "_aggregated" / "r1"

    def fake_rsync_pull(*args, **kwargs):
        _stage_pulled_output(local_dir, "r1.json", {"score": 0.1})
        return _completed(returncode=0)

    captured_cmd: list[str] = []

    def fake_ssh(cmd: str, **kwargs):
        captured_cmd.append(cmd)
        return _completed(returncode=0)

    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", side_effect=fake_ssh),
        mock.patch("claude_hpc.infra.remote.rsync_pull", side_effect=fake_rsync_pull),
    ):
        out = cluster_reduce(tmp_path, run_id="r1")
    assert out["ok"] is True
    assert "python -m sidecar.reducer" in captured_cmd[0]


def test_no_aggregate_cmd_anywhere_raises(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="no aggregate_cmd"):
        cluster_reduce(tmp_path, run_id="r1")


def test_reducer_nonzero_exit_raises_remote_command_failed(
    tmp_path: Path, journal_home: Path
) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    with (
        mock.patch(
            "claude_hpc.infra.remote.ssh_run",
            return_value=_completed(returncode=2, stderr="ImportError: no qlike module\n"),
        ),
        pytest.raises(errors.RemoteCommandFailed, match="exited 2"),
    ):
        cluster_reduce(tmp_path, run_id="r1", aggregate_cmd="python -m my.reducer")


def test_rsync_pull_failure_raises(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", return_value=_completed(returncode=0)),
        mock.patch(
            "claude_hpc.infra.remote.rsync_pull",
            return_value=_completed(
                returncode=23, stderr="rsync: connection unexpectedly closed\n"
            ),
        ),
        pytest.raises(errors.RemoteCommandFailed, match="rsync_pull"),
    ):
        cluster_reduce(tmp_path, run_id="r1", aggregate_cmd="python -m my.reducer")


def test_invalid_json_output_raises(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    local_dir = tmp_path / "_aggregated" / "r1"

    def fake_rsync_pull(*args, **kwargs):
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "r1.json").write_text("this is not json")
        return _completed(returncode=0)

    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", return_value=_completed(returncode=0)),
        mock.patch("claude_hpc.infra.remote.rsync_pull", side_effect=fake_rsync_pull),
        pytest.raises(errors.RemoteCommandFailed, match="not valid JSON"),
    ):
        cluster_reduce(tmp_path, run_id="r1", aggregate_cmd="python -m my.reducer")


def test_extra_env_threaded_into_remote_cmd(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    local_dir = tmp_path / "_aggregated" / "r1"

    def fake_rsync_pull(*args, **kwargs):
        _stage_pulled_output(local_dir, "r1.json", {"x": 1})
        return _completed(returncode=0)

    captured_cmd: list[str] = []

    def fake_ssh(cmd: str, **kwargs):
        captured_cmd.append(cmd)
        return _completed(returncode=0)

    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", side_effect=fake_ssh),
        mock.patch("claude_hpc.infra.remote.rsync_pull", side_effect=fake_rsync_pull),
    ):
        cluster_reduce(
            tmp_path,
            run_id="r1",
            aggregate_cmd="python -m my.reducer",
            extra_env={"DATA_DIR": "/scratch/data", "VERBOSE": "1"},
        )
    assert "DATA_DIR=/scratch/data" in captured_cmd[0]
    assert "VERBOSE=1" in captured_cmd[0]
    assert "HPC_RUN_ID=r1" in captured_cmd[0]


def test_custom_output_path_template_substitutes_run_id(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    _seed(tmp_path)
    local_dir = tmp_path / "_aggregated" / "r1"

    def fake_rsync_pull(*args, **kwargs):
        _stage_pulled_output(local_dir, "r1.summary.json", {"y": 2})
        return _completed(returncode=0)

    with (
        mock.patch("claude_hpc.infra.remote.ssh_run", return_value=_completed(returncode=0)),
        mock.patch("claude_hpc.infra.remote.rsync_pull", side_effect=fake_rsync_pull),
    ):
        out = cluster_reduce(
            tmp_path,
            run_id="r1",
            aggregate_cmd="python -m my.reducer",
            output_path="custom/{run_id}.summary.json",
        )
    assert out["output_path_remote"] == "custom/r1.summary.json"


def test_no_journal_record_raises(tmp_path: Path, journal_home: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    with pytest.raises(errors.SpecInvalid, match="no journal record"):
        cluster_reduce(tmp_path, run_id="missing", aggregate_cmd="x")


def test_empty_run_id_raises(tmp_path: Path) -> None:
    from claude_hpc.atoms.cluster_reduce import cluster_reduce

    with pytest.raises(errors.SpecInvalid, match="run_id"):
        cluster_reduce(tmp_path, run_id="", aggregate_cmd="x")
