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

from hpc_agent import errors
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops.recover_flow import (
    _submit_resubmit_batches,
    render_overrides_to_extra_flags,
    resubmit_flow,
)
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord
from tests.conftest import make_sidecar_json

if TYPE_CHECKING:
    from pathlib import Path

PROFILE = "ml_ridge"
CLUSTER = "test_cluster"
RUN_ID = "ml_ridge_abcd1234"


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
    upsert_run(experiment, record)
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


class _StubBackend(HPCBackend):
    """Minimal backend stub over the real HPCBackend interface.

    Subclasses :class:`HPCBackend` so it inherits the shared per-batch primitive
    (:meth:`HPCBackend.submit_one`) that ``_submit_one_batch`` now routes through
    (#339 inc 3). Records every ``_build_command`` and ``_execute_command``
    invocation so tests can assert on the task_range strings + extra_flags that
    flowed through. Returns a synthetic job id by default; tests can override
    ``submit_responses`` to simulate failures.
    """

    JOB_ID_REGEX = re.compile(r"Submitted batch job (\d+)")

    def __init__(self, *, submit_responses=None):
        self.log_dir = "/tmp/recover-stub-logs"
        self.calls: list[dict] = []
        self.responses: list = list(submit_responses) if submit_responses else []
        self._next_id = 90000000

    def _setup_log_dir(self):
        self.calls.append({"step": "setup_log_dir"})

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):
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

    def test_sge_renders_mem_walltime_gpus_cpus(self, monkeypatch):
        # run-14: the SGE override renderer routes mem through the shared
        # sge_h_data_mb helper — h_data is PER-SLOT + vmem-enforced, so the
        # per-task-total mem_mb is divided across the -pe slots and grown by the
        # disclosed vmem headroom. Pin the factor: ceil(32000 * 2.0 / 8) = 8000M.
        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "2")
        flags = render_overrides_to_extra_flags(
            "sge",
            {"mem_mb": 32_000, "walltime_sec": 14400, "gpus": 2, "cpus": 8},
        )
        assert flags == [
            "-l",
            "h_data=8000M",
            "-l",
            "h_rt=04:00:00",
            "-l",
            "gpu=2",
            "-pe",
            "shared",
            "8",
        ]

    def test_pbspro_renders_select_chunk_and_separate_walltime(self):
        flags = render_overrides_to_extra_flags(
            "pbspro",
            {"mem_mb": 4096, "walltime_sec": 7200, "gpus": 2, "cpus": 8},
        )
        # cpus/mem/gpus combine into one select= chunk; walltime is its own -l.
        assert flags == [
            "-l",
            "select=1:ncpus=8:mem=4096mb:ngpus=2",
            "-l",
            "walltime=02:00:00",
        ]

    def test_pbspro_partial_override_only_walltime(self):
        # walltime alone must not emit an empty select= chunk.
        assert render_overrides_to_extra_flags("pbspro", {"walltime_sec": 3600}) == [
            "-l",
            "walltime=01:00:00",
        ]

    def test_torque_renders_single_comma_joined_l(self):
        flags = render_overrides_to_extra_flags(
            "torque",
            {"mem_mb": 4096, "walltime_sec": 7200, "gpus": 2, "cpus": 8},
        )
        assert flags == ["-l", "nodes=1:ppn=8:gpus=2,mem=4096mb,walltime=02:00:00"]

    def test_torque_partial_override_mem_only(self):
        assert render_overrides_to_extra_flags("torque", {"mem_mb": 8192}) == [
            "-l",
            "mem=8192mb",
        ]

    def test_unknown_keys_drop_silently(self):
        flags = render_overrides_to_extra_flags("slurm", {"unknown_knob": 42, "mem_mb": 16_000})
        assert flags == ["--mem=16000M"]

    def test_empty_overrides_returns_empty_list(self):
        assert render_overrides_to_extra_flags("slurm", None) == []
        assert render_overrides_to_extra_flags("slurm", {}) == []

    def test_unknown_scheduler_raises(self):
        with pytest.raises(errors.SpecInvalid, match="unknown scheduler"):
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
                submit_to_cluster=True,
                script="script.sh",
                job_name="resub",
            )


class TestResubmitOutOfRangeGuard:
    """#339: resubmit on an index-bounded backend refuses out-of-range ids.

    A multi-wave run's failed ids can exceed the scheduler's array-index cap.
    The submit path waves past the cap with local-range + offset, but a resubmit
    replays the actual (possibly non-contiguous) ids as a global array
    expression a single offset can't encode — so it must fail loud rather than
    emit an out-of-range array.
    """

    def _call(self, stub, *, failed_task_ids, total_tasks, cap, tmp_path):
        from hpc_agent.infra.constraints import ClusterConstraints

        return _submit_resubmit_batches(
            experiment_dir=tmp_path,
            run_id="r",
            failed_task_ids=failed_task_ids,
            effective_overrides=None,
            ssh_target="u@h",
            remote_path="/r",
            scheduler="slurm",
            script="run.sh",
            job_name="resub",
            job_env={"HPC_RUN_ID": "r"},
            total_tasks=total_tasks,
            constraints=ClusterConstraints(max_array_size=cap),
            backend_factory=_make_factory(stub),
        )

    def test_over_cap_failed_id_raises(self, tmp_path):
        # id 150 → 1-based array index 151 > cap 100 → refuse.
        with pytest.raises(errors.SpecInvalid, match="out-of-range"):
            self._call(
                _StubBackend(), failed_task_ids=[150], total_tasks=200, cap=100, tmp_path=tmp_path
            )

    def test_in_range_ids_submit_as_before(self, tmp_path):
        # All ids' 1-based indices are <= cap → submits exactly as before.
        ids = self._call(
            _StubBackend(), failed_task_ids=[3, 50], total_tasks=200, cap=100, tmp_path=tmp_path
        )
        assert len(ids) == 1  # one batch


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
        # extra_flags carry the caller-supplied overrides verbatim,
        # rendered to scheduler flags: 14400s → --time=04:00:00.
        assert "--mem=32000M" in build_calls[0]["extra_flags"]
        assert "--time=04:00:00" in build_calls[0]["extra_flags"]
        # journal got the new job_id
        record = load_run(experiment, RUN_ID)
        assert result.new_job_ids[0] in record.job_ids

    def test_from_checkpoint_stamps_resume_var_into_job_env(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        """from_checkpoint=True single-sources HPC_RESUME_FROM_CHECKPOINT=1 into
        the job_env that ships to the scheduler (#294 PR3 / #299), so the
        dispatcher resumes each retried task from its latest checkpoint."""
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, total_tasks=10)
        stub = _StubBackend()
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="walltime",
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={"HPC_RUN_ID": RUN_ID},
            from_checkpoint=True,
            backend_factory=_make_factory(stub),
        )
        build_calls = [c for c in stub.calls if c["step"] == "build_command"]
        assert build_calls, "expected at least one batch submission"
        assert "HPC_RESUME_FROM_CHECKPOINT" in build_calls[0]["job_env_keys"]

    def test_no_from_checkpoint_leaves_job_env_unstamped(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, total_tasks=10)
        stub = _StubBackend()
        resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[1, 2],
            category="walltime",
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={"HPC_RUN_ID": RUN_ID},
            backend_factory=_make_factory(stub),
        )
        build_calls = [c for c in stub.calls if c["step"] == "build_command"]
        assert "HPC_RESUME_FROM_CHECKPOINT" not in build_calls[0]["job_env_keys"]

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
                submit_to_cluster=True,
                script="run.sh",
                backend="slurm",
                job_name="resub",
                job_env={},
                backend_factory=_make_factory(stub),
            )


class _RaisingBackend(_StubBackend):
    """Global-index backend stub whose responses may be exceptions.

    ``uses_global_array_index=True`` skips the resubmit out-of-range guard so a
    multi-batch plan (``max_array_size`` < ``len(failed)``) actually reaches the
    submit loop. Each ``_execute_command`` response that is a ``BaseException``
    is *raised* (simulating a mid-loop ``SshCircuitOpen`` / ``TimeoutError``
    that escapes ``_submit_one_batch``'s narrow ``except RuntimeError`` re-wrap)
    instead of returned.
    """

    uses_global_array_index = True

    def _execute_command(self, cmd, job_env, cwd):
        self.calls.append({"step": "execute", "cmd": cmd})
        if self.responses:
            resp = self.responses.pop(0)
            if isinstance(resp, BaseException):
                raise resp
            return resp
        self._next_id += 1
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=f"Submitted batch job {self._next_id}\n",
            stderr="",
        )


class TestPartialResubmitResumeMarker:
    """#16: a mid-loop failure that is NOT RemoteCommandFailed must still
    persist the resume marker, so a retry resumes after the last landed batch
    instead of re-submitting duplicate array jobs.
    """

    def _run_until_batch2_fails(self, experiment, tmp_path, monkeypatch, exc):
        from hpc_agent.infra.constraints import ClusterConstraints

        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, total_tasks=100)
        # Batch 1 lands (job 90000001); batch 2 raises the supplied exception.
        good = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Submitted batch job 90000001\n", stderr=""
        )
        stub = _RaisingBackend(submit_responses=[good, exc])
        with pytest.raises(type(exc)):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[0, 1, 2, 3],
                category="system_oom",
                submit_to_cluster=True,
                script="run.sh",
                backend="slurm",
                job_name="resub",
                job_env={"HPC_RUN_ID": RUN_ID},
                # max_array_size=2 with 4 failed ids → the plan splits into two
                # batches, so batch 2 is reachable after batch 1 lands.
                constraints=ClusterConstraints(max_array_size=2),
                backend_factory=_make_factory(stub),
            )
        return stub

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("bounded runner deadline fired"),
            errors.SshCircuitOpen("per-host breaker open"),
        ],
        ids=["timeout", "circuit_open"],
    )
    def test_non_remotecommandfailed_persists_marker(
        self, exc, journal_home, experiment, tmp_path, monkeypatch
    ):
        from hpc_agent.ops.recover.runner import derive_resubmit_request_id

        self._run_until_batch2_fails(experiment, tmp_path, monkeypatch, exc)

        rec = load_run(experiment, RUN_ID)
        marker = dict(rec.pending_resubmit or {})
        assert marker, "resume marker must be persisted on a non-RCF mid-loop failure"
        # Only batch 1's id landed — the marker records exactly that.
        assert marker["job_ids"] == ["90000001"]
        expected_rid = derive_resubmit_request_id(
            failed_task_ids=[0, 1, 2, 3], category="system_oom", overrides=None
        )
        assert marker["request_id"] == expected_rid
        # Top-level job_ids extended (monitor reads these), original preserved.
        assert "90000001" in rec.job_ids
        assert "12345678" in rec.job_ids

    def test_retry_resumes_after_last_landed_batch(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        from hpc_agent.infra.constraints import ClusterConstraints

        self._run_until_batch2_fails(
            experiment, tmp_path, monkeypatch, errors.SshCircuitOpen("open")
        )

        # Retry: same args → same derived request_id → resumes. start_batch is
        # len(prior landed ids)=1, so ONLY the remaining batch is submitted.
        retry_stub = _RaisingBackend()
        result = resubmit_flow(
            experiment,
            RUN_ID,
            failed_task_ids=[0, 1, 2, 3],
            category="system_oom",
            submit_to_cluster=True,
            script="run.sh",
            backend="slurm",
            job_name="resub",
            job_env={"HPC_RUN_ID": RUN_ID},
            constraints=ClusterConstraints(max_array_size=2),
            backend_factory=_make_factory(retry_stub),
        )
        build_calls = [c for c in retry_stub.calls if c["step"] == "build_command"]
        assert len(build_calls) == 1, "retry must submit only the un-landed batch"
        assert result.cluster_submitted is True
        # Marker cleared once the resubmit completed and stamped the request_id.
        assert dict(load_run(experiment, RUN_ID).pending_resubmit or {}) == {}

    def test_zero_progress_failure_writes_no_spurious_marker(
        self, journal_home, experiment, tmp_path, monkeypatch
    ):
        from hpc_agent.infra.constraints import ClusterConstraints

        _write_clusters_yaml(tmp_path, monkeypatch)
        _seed(experiment, total_tasks=100)
        # Batch 1 (the very first) raises → nothing landed → no marker.
        stub = _RaisingBackend(submit_responses=[TimeoutError("first batch died")])
        with pytest.raises(TimeoutError):
            resubmit_flow(
                experiment,
                RUN_ID,
                failed_task_ids=[0, 1, 2, 3],
                category="system_oom",
                submit_to_cluster=True,
                script="run.sh",
                backend="slurm",
                job_name="resub",
                job_env={"HPC_RUN_ID": RUN_ID},
                constraints=ClusterConstraints(max_array_size=2),
                backend_factory=_make_factory(stub),
            )
        assert dict(load_run(experiment, RUN_ID).pending_resubmit or {}) == {}


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
            submit_to_cluster=False,  # legacy default
        )
        env = result.to_envelope_data()
        assert env["cluster_submitted"] is False
        assert "new_job_ids" not in env  # empty list omitted from envelope
