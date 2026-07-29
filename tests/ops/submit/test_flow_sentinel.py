"""Submit-flow wiring of the W1 run-terminal sentinel (crash-only-monitoring).

Through ``_submit_one_spec`` (the one seam both the single and batch flows
route): flag OFF (the default) the submit is byte-identical — zero sentinel
traffic, no new sidecar field; flag ON the sentinel is staged + submitted
AFTER the run is recorded, its id lands on the SEPARATE ``sentinel_job_id``
sidecar field while ``job_ids`` / the result envelope stay byte-identical; a
sentinel failure is disclosed and never fails the run.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_flow as sf
from hpc_agent.ops.monitor import sentinel as sen
from hpc_agent.state.runs import read_run_sidecar


@pytest.fixture
def _cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {"c": {"scheduler": "sge"}}
    monkeypatch.setattr("hpc_agent.infra.clusters.load_clusters_config", lambda: cfg)


class _Backend(HPCBackend):
    """Capturing stub with completion-dependency support (hold_jid style)."""

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self) -> None:
        self.log_dir = "/tmp/sentinel-stub-logs"
        self.script = "run.sh"
        self._counter = 100
        self.commands: list[list[str]] = []

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        return ["-hold_jid", ",".join(job_ids)] if job_ids else []

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):
        cmd = ["qsub"]
        if array:
            cmd += ["-t", str(task_range)]
        cmd += ["-N", job_name]
        cmd.extend(extra_flags or [])
        cmd.append(self.script)
        return cmd

    def _execute_command(self, cmd, job_env, cwd):
        self.commands.append(list(cmd))
        self._counter += 1
        return SimpleNamespace(stdout=f"JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        pass


def _spec(run_id: str, total_tasks: int = 3):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    return SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/r",
        job_name=run_id,
        run_id=run_id,
        total_tasks=total_tasks,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python run.py"},
        canary=False,
        result_dir_template="results/{run_id}/task_{task_id}",
    )


def _run_flow(tmp_path: Path, spec, backend):
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
        mock.patch.object(sf, "load_run", return_value=None),
    ):
        return sf._submit_one_spec(experiment_dir=tmp_path, spec=spec)


def test_flag_off_submit_is_byte_identical(
    tmp_path: Path,
    _cluster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (flag unset): no staging dial, no extra qsub, no sidecar field."""
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    monkeypatch.delenv(sen.SENTINEL_JOB_ENV, raising=False)

    def _boom(**_kw):
        raise AssertionError("flag off must never stage a sentinel script")

    monkeypatch.setattr(sen, "stage_sentinel_script", _boom)

    spec = _spec("20260729-000000-sentoff")
    sf._ensure_run_sidecar(tmp_path, spec)
    backend = _Backend()
    result = _run_flow(tmp_path, spec, backend)

    assert result.job_ids == ["101"]
    # Exactly the one main-array qsub — no sentinel command rode along.
    assert backend.commands == [["qsub", "-t", "1-3", "-N", spec.run_id, "run.sh"]]
    sidecar = read_run_sidecar(tmp_path, spec.run_id)
    assert "sentinel_job_id" not in sidecar
    assert sidecar["job_ids"] == ["101"]


def test_flag_on_submits_sentinel_and_keeps_job_ids_byte_identical(
    tmp_path: Path,
    _cluster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")
    staged: list[dict] = []

    def _stage(**kw):
        staged.append(kw)
        return sen.sentinel_script_relpath(kw["run_id"])

    monkeypatch.setattr(sen, "stage_sentinel_script", _stage)

    spec = _spec("20260729-000000-senton")
    sf._ensure_run_sidecar(tmp_path, spec)
    backend = _Backend()
    result = _run_flow(tmp_path, spec, backend)

    # Result envelope + journal-facing ids: the compute array only.
    assert result.job_ids == ["101"]
    # The sentinel was staged for this run and submitted as a NON-array job
    # holding behind the main array's id, with the recognisable name suffix.
    assert staged and staged[0]["run_id"] == spec.run_id
    assert len(backend.commands) == 2
    sentinel_cmd = backend.commands[1]
    assert "-t" not in sentinel_cmd  # non-array
    assert sentinel_cmd[sentinel_cmd.index("-hold_jid") + 1] == "101"
    assert f"{spec.run_id}_sentinel" in sentinel_cmd
    assert sentinel_cmd[-1] == sen.sentinel_script_relpath(spec.run_id)
    # Accounting: the sentinel id lives ONLY on the separate sidecar field.
    sidecar = read_run_sidecar(tmp_path, spec.run_id)
    assert sidecar["job_ids"] == ["101"]
    assert sidecar["sentinel_job_id"] == "102"
    assert "102" not in sidecar["job_ids"]


def test_flag_on_sentinel_failure_never_fails_the_run(
    tmp_path: Path,
    _cluster: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from hpc_agent.state import run_record

    monkeypatch.setattr(run_record, "HPC_HOMEDIR", tmp_path / "home_hpc")
    monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")

    def _stage_fail(**_kw):
        raise RuntimeError("login node rejected the staging write")

    monkeypatch.setattr(sen, "stage_sentinel_script", _stage_fail)

    spec = _spec("20260729-000000-senfail")
    sf._ensure_run_sidecar(tmp_path, spec)
    backend = _Backend()
    with caplog.at_level(logging.WARNING, logger="hpc_agent.ops.monitor.sentinel"):
        result = _run_flow(tmp_path, spec, backend)

    # The run landed exactly as today; the failure was disclosed, not raised.
    assert result.job_ids == ["101"]
    assert backend.commands == [["qsub", "-t", "1-3", "-N", spec.run_id, "run.sh"]]
    sidecar = read_run_sidecar(tmp_path, spec.run_id)
    assert "sentinel_job_id" not in sidecar
    assert any("not submitted" in r.message for r in caplog.records)
