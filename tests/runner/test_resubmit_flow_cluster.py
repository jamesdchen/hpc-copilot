"""Phase 5 tests — resubmit_flow's cluster-side submission step.

Covers ``submit_to_cluster=True``: building the ResubmitPlan,
rendering overrides to scheduler flags, dispatching each batch through
the backend, and recording the new job IDs in the journal.

The scheduler is mocked via ``backend_factory`` to avoid SSH / qsub.
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

import pytest

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal.session import RunRecord, run_record
from claude_hpc.flows.resubmit_flow import (
    render_overrides_to_extra_flags,
    resubmit_flow,
)
from tests.conftest import make_sidecar_json

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"


@pytest.fixture
def journal_home(tmp_path, monkeypatch):
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
    return tmp_path


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
        ssh_target="user@cluster.example.edu",
        remote_path="/u/scratch/exp",
        job_name=PROFILE,
        job_ids=["12345678"],
        total_tasks=total_tasks,
        submitted_at="2026-04-26T17:00:00+00:00",
        experiment_dir=str(experiment.resolve()),
    )
    session.upsert_run(experiment, record)
    make_sidecar_json(
        experiment,
        run_id=RUN_ID,
        cluster=CLUSTER,
        profile=PROFILE,
        ssh_target="user@cluster.example.edu",
        remote_path="/u/scratch/exp",
    )
    return record


def _write_clusters_yaml(tmp_path, monkeypatch):
    import yaml

    cfg = {
        CLUSTER: {
            "scheduler": "slurm",
            "ssh_target": "user@cluster.example.edu",
            "max_walltime_sec": 86400,
            "cold_start_mem_buffer": 0.0,
        }
    }
    yaml_path = tmp_path / "clusters.yaml"
    yaml_path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(yaml_path))


class _StubBackend:
    """Minimal backend stub mirroring HPCBackend's private surface.

    Records every ``_build_command`` and ``_execute_command`` invocation
    so tests can assert on the task_range strings + extra_flags that
    flowed through. Returns a synthetic job id by default; tests can
    override ``submit_responses`` to simulate failures.
    """

    JOB_ID_REGEX = re.compile(r"Submitted batch job (\d+)")

    def __init__(self, *, submit_responses=None):
        self.calls: list[dict] = []
        self.responses: list = list(submit_responses) if submit_responses else []
        self._next_id = 90000000

    def _setup_log_dir(self):
        self.calls.append({"step": "setup_log_dir"})

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None):
        self.calls.append(
            {
                "step": "build_command",
                "task_range": task_range,
                "job_name": job_name,
                "extra_flags": list(extra_flags or []),
                "job_env_keys": sorted(job_env.keys()),
            }
        )
        return ["sbatch", "--array", task_range, "--job-name", job_name, "script.sh"]

    def _execute_command(self, cmd, job_env, cwd):
        self.calls.append({"step": "execute", "cmd": cmd})
        if self.responses:
            return self.responses.pop(0)
        self._next_id += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=f"Submitted batch job {self._next_id}\n",
            stderr="",
        )


def _make_factory(stub: _StubBackend):
    def factory(**_kwargs):
        return stub

    return factory


class TestRenderOverridesToExtraFlags:
    def test_slurm_renders_mem_walltime_gpus_cpus(self):
        flags = render_overrides_to_extra_flags(
            "slurm",
            {"mem_mb": 32_000, "walltime_sec": 14400, "gpus": 2, "cpus": 8},
        )
        assert flags == [
            "--mem=32000M",
            "--time=04:00:00",
            "--gpus=2",
            "--cpus-per-task=8",
        ]

    def test_sge_renders_mem_walltime_gpus_cpus(self):
        flags = render_overrides_to_extra_flags(
            "sge",
            {"mem_mb": 32_000, "walltime_sec": 14400, "gpus": 2, "cpus": 8},
        )
        assert flags == [
            "-l",
            "h_data=32000M",
            "-l",
            "h_rt=04:00:00",
            "-l",
            "gpu=2",
            "-pe",
            "shared",
            "8",
        ]

    def test_unknown_keys_drop_silently(self):
        flags = render_overrides_to_extra_flags("slurm", {"unknown_knob": 42, "mem_mb": 16_000})
        assert flags == ["--mem=16000M"]

    def test_empty_overrides_returns_empty_list(self):
        assert render_overrides_to_extra_flags("slurm", None) == []
        assert render_overrides_to_extra_flags("slurm", {}) == []

    def test_unknown_scheduler_raises(self):
        with pytest.raises(ValueError, match="unknown scheduler"):
            render_overrides_to_extra_flags("kubernetes", {"mem_mb": 1024})

    def test_walltime_pads_to_hh_mm_ss(self):
        flags = render_overrides_to_extra_flags("slurm", {"walltime_sec": 65})
        assert "--time=00:01:05" in flags


class TestSubmitToClusterRequiredKwargs:
    def test_missing_script_raises(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        with pytest.raises(errors.SpecInvalid, match="script"):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[1, 2],
                category="system_oom",
                consult_forecast=False,
                submit_to_cluster=True,
                backend="slurm",
                job_name="resub",
            )

    def test_missing_backend_raises(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        with pytest.raises(errors.SpecInvalid):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[1, 2],
                category="system_oom",
                consult_forecast=False,
                submit_to_cluster=True,
                script="script.sh",
                job_name="resub",
            )


class TestClusterSubmission:
    def test_qsubs_each_batch_and_records_new_job_ids(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, total_tasks=100)
        stub = _StubBackend()
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[3, 7, 12, 13, 14],
            category="system_oom",
            overrides={"mem_mb": 32_000, "walltime_sec": 14400},
            consult_forecast=False,
            submit_to_cluster=True,
            script="/cluster/path/run.sh",
            backend="slurm",
            job_name="ml_ridge_resub",
            job_env={"HPC_RUN_ID": RUN_ID, "HPC_TASK_COUNT": "100"},
            backend_factory=_make_factory(stub),
        )
        assert result.cluster_submitted is True
        assert len(result.new_job_ids) == 1  # one batch
        # task_range packs failed IDs into "4,8,13-15": 1-based to match
        # the scheduler array-expression convention (the SLURM/SGE
        # templates subtract 1 to recover the 0-based HPC_TASK_ID).
        build_calls = [c for c in stub.calls if c["step"] == "build_command"]
        assert build_calls[0]["task_range"] == "4,8,13-15"
        # extra_flags carry the planner-adjusted overrides. Walltime
        # gets cold-start arbitraged from 14400s (4h) → 13500s (3h45m)
        # by the planner — the whole point of Phase 5 is that this
        # adjustment now appears in the qsub flags rather than the
        # static 4× table the slash command used to apply.
        assert "--mem=32000M" in build_calls[0]["extra_flags"]
        assert "--time=03:45:00" in build_calls[0]["extra_flags"]
        # journal got the new job_id
        record = session.load_run(experiment, RUN_ID)
        assert result.new_job_ids[0] in record.job_ids

    def test_skips_qsub_on_dedupe(self, journal_home, experiment, tmp_path, monkeypatch):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        stub = _StubBackend()
        # First submission stamps the request_id.
        first = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            request_id="rs_explicit",
            consult_forecast=False,
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={},
            backend_factory=_make_factory(stub),
        )
        assert first.cluster_submitted is True
        # Replay with the same request_id should NOT qsub again.
        stub2 = _StubBackend()
        second = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="system_oom",
            overrides={"mem_mb": 16_000},
            request_id="rs_explicit",
            consult_forecast=False,
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={},
            backend_factory=_make_factory(stub2),
        )
        # On dedup, ``cluster_submitted`` now reflects the durable
        # state (a prior call DID submit; the new call deduped against
        # it) rather than "this specific invocation submitted". Combined
        # with ``deduped=True``, callers can distinguish "fresh submit"
        # from "replay of prior submit". The qsub stub still sees zero
        # calls — no new cluster work was issued.
        assert second.cluster_submitted is True
        assert second.deduped is True
        assert stub2.calls == []  # no qsub calls

    def test_qsub_failure_raises_remote_command_failed(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        bad = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="invalid partition"
        )
        stub = _StubBackend(submit_responses=[bad])
        with pytest.raises(errors.RemoteCommandFailed, match="invalid partition"):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[1],
                category="system_oom",
                consult_forecast=False,
                submit_to_cluster=True,
                script="run.sh",
                backend="slurm",
                job_name="resub",
                job_env={},
                backend_factory=_make_factory(stub),
            )

    def test_planner_overrides_flow_into_qsub_flags(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """Cold-start mem buffer applied by planner appears in extra_flags."""
        # Reuse the cold-start cluster (cold_start_mem_buffer=0.15).
        import yaml

        cfg = {
            CLUSTER: {
                "scheduler": "slurm",
                "ssh_target": "user@cluster.example.edu",
                "max_walltime_sec": 86400,
                "cold_start_mem_buffer": 0.15,
                "walltime_arbitrage": False,  # keep the test focused on mem
            }
        }
        yaml_path = tmp_path / "clusters.yaml"
        yaml_path.write_text(yaml.safe_dump(cfg))
        monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(yaml_path))

        _seed(experiment)
        stub = _StubBackend()
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            overrides={"mem_mb": 16_000},  # planner grows to 18_400 on cold start
            consult_forecast=False,
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={},
            backend_factory=_make_factory(stub),
        )
        build_calls = [c for c in stub.calls if c["step"] == "build_command"]
        # The planner-grown 18400 (not the raw 16000) should be in the flags.
        assert "--mem=18400M" in build_calls[0]["extra_flags"]


class TestEnvelopeShape:
    def test_envelope_includes_new_job_ids_when_cluster_submitted(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        stub = _StubBackend()
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            consult_forecast=False,
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={},
            backend_factory=_make_factory(stub),
        )
        env = result.to_envelope_data()
        assert env["cluster_submitted"] is True
        assert "new_job_ids" in env
        assert env["new_job_ids"] == result.new_job_ids

    def test_envelope_omits_new_job_ids_when_journal_only(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment)
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1],
            category="system_oom",
            consult_forecast=False,
            submit_to_cluster=False,  # legacy default
        )
        env = result.to_envelope_data()
        assert env["cluster_submitted"] is False
        assert "new_job_ids" not in env  # empty list omitted from envelope
