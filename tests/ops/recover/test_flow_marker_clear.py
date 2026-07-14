"""resubmit_flow clears the retry preamble's terminal ``.hpc_failed`` markers.

``hpc_run_with_retry`` (hpc_preamble.sh) refuses on entry any (run, task)
whose ``.hpc_failed/<run>.<task>.failed`` marker exists — deliberate
loop-bounding (#161). Nothing used to clear the markers, so the documented
resubmit-with-adjusted-resources recovery exited 1 in milliseconds forever
for any task that had exhausted its in-job attempts. These tests pin the
fix: the cluster-side resubmit path clears the markers for exactly the ids
it re-submits, over ONE ssh call, BEFORE the new array lands — and a
marker-clear hiccup never blocks the resubmit itself.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

import pytest

from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops.recover_flow import resubmit_flow
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from tests.conftest import make_sidecar_json

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"
SSH_TARGET = "user@cluster.example.edu"
REMOTE_PATH = "/u/scratch/exp"


@pytest.fixture
def experiment(tmp_path):
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed(experiment: Path, *, total_tasks: int = 100) -> RunRecord:
    record = RunRecord(
        run_id=RUN_ID,
        profile=PROFILE,
        cluster=CLUSTER,
        ssh_target=SSH_TARGET,
        remote_path=REMOTE_PATH,
        job_name=PROFILE,
        job_ids=["12345678"],
        total_tasks=total_tasks,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    upsert_run(experiment, record)
    make_sidecar_json(
        experiment,
        run_id=RUN_ID,
        cluster=CLUSTER,
        profile=PROFILE,
        ssh_target=SSH_TARGET,
        remote_path=REMOTE_PATH,
    )
    return record


def _write_clusters_yaml(tmp_path, monkeypatch):
    import yaml

    cfg = {
        CLUSTER: {
            "scheduler": "slurm",
            "ssh_target": SSH_TARGET,
            "max_walltime_sec": 86400,
            "cold_start_mem_buffer": 0.0,
        }
    }
    yaml_path = tmp_path / "clusters.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(yaml_path))


class _EventStubBackend(HPCBackend):
    """Backend stub that records qsub events into a shared ordering log."""

    JOB_ID_REGEX = re.compile(r"Submitted batch job (\d+)")

    def __init__(self, events: list):
        self.log_dir = "/tmp/marker-clear-stub-logs"
        self.events = events
        self._next_id = 91000000

    def _setup_log_dir(self):
        pass

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):
        return ["sbatch", "--array", task_range, "--job-name", job_name, "script.sh"]

    def _execute_command(self, cmd, job_env, cwd):
        self.events.append(("qsub", " ".join(cmd)))
        self._next_id += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=f"Submitted batch job {self._next_id}\n",
            stderr="",
        )


def _install_fake_ssh_run(monkeypatch, events: list, *, returncode: int = 0):
    """Record every remote.ssh_run into *events* and return *returncode*."""

    def fake_ssh_run(cmd, *, ssh_target, **kwargs):
        events.append(("ssh", ssh_target, cmd))
        return subprocess.CompletedProcess(
            args=["ssh", ssh_target, cmd], returncode=returncode, stdout="", stderr="boom"
        )

    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", fake_ssh_run)
    return fake_ssh_run


def _run_flow(experiment, events, **overrides):
    kwargs = dict(
        failed_task_ids=[3, 7],
        category="system_oom",
        overrides={"mem_mb": 32_000},
        submit_to_cluster=True,
        script="run.sh",
        backend="slurm",
        job_name="resub",
        job_env={"HPC_RUN_ID": RUN_ID},
        backend_factory=lambda **_kw: _EventStubBackend(events),
    )
    kwargs.update(overrides)
    return resubmit_flow(experiment, RUN_ID, **kwargs)


class TestMarkerClearOnResubmit:
    def test_clears_markers_for_resubmitted_ids_before_qsub(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        events: list = []
        _install_fake_ssh_run(monkeypatch, events)

        result = _run_flow(experiment, events)

        assert result.cluster_submitted is True
        ssh_events = [e for e in events if e[0] == "ssh"]
        assert len(ssh_events) == 1, "marker clear must be ONE ssh call (throttle discipline)"
        _, ssh_target, cmd = ssh_events[0]
        assert ssh_target == SSH_TARGET
        assert cmd.startswith("rm -f -- ")
        # Exactly the resubmitted ids' markers, under the documented layout
        # ($RESULT_DIR defaults to the job cwd = remote_path).
        assert f"{REMOTE_PATH}/.hpc_failed/{RUN_ID}.3.failed" in cmd
        assert f"{REMOTE_PATH}/.hpc_failed/{RUN_ID}.7.failed" in cmd
        assert f"{RUN_ID}.4." not in cmd  # no stray ids, no 1-based shift
        # The clear lands BEFORE any batch is submitted, so no task can start
        # while its marker still exists.
        assert events[0][0] == "ssh"
        assert any(kind == "qsub" for kind, *_ in events[1:])

    def test_ssh_failure_does_not_block_resubmit(
        self, journal_home, experiment, tmp_path, monkeypatch, caplog
    ):
        """Best-effort: a marker-clear hiccup is logged loudly (the tasks
        would be refused again) but never blocks the resubmission."""
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        events: list = []
        _install_fake_ssh_run(monkeypatch, events, returncode=1)

        with caplog.at_level("WARNING", logger="hpc_agent.ops.recover_flow"):
            result = _run_flow(experiment, events)

        assert result.cluster_submitted is True
        assert result.new_job_ids, "qsub must still have run"
        assert any(".hpc_failed" in rec.message for rec in caplog.records)

    def test_journal_only_resubmit_never_touches_cluster(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        events: list = []
        _install_fake_ssh_run(monkeypatch, events)

        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[3, 7],
            category="system_oom",
            submit_to_cluster=False,
        )

        assert events == []

    def test_dedup_replay_does_not_clear_again(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """A replayed request_id submits nothing — so it must clear nothing
        (a second clear could erase a marker a FRESH post-resubmit failure
        just wrote, unbounding the loop the marker exists to bound)."""
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        events: list = []
        _install_fake_ssh_run(monkeypatch, events)

        first = _run_flow(experiment, events, request_id="rs_explicit")
        assert first.cluster_submitted is True
        clears_after_first = len([e for e in events if e[0] == "ssh"])
        assert clears_after_first == 1

        second = _run_flow(experiment, events, request_id="rs_explicit")
        assert second.deduped is True
        assert len([e for e in events if e[0] == "ssh"]) == clears_after_first
