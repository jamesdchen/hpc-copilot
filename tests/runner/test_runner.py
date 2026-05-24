"""Tests for ``hpc_agent.runner`` — the bundled mapreduce + journal ops.

SSH primitives are mocked so the tests exercise the wiring (journal-update
ordering, retry counting, drift reconciliation) without touching a network.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from hpc_agent import runner
from hpc_agent._wire.actions.resubmit import ResubmitSpec
from hpc_agent._wire.actions.submit import SubmitSpec as _WireSubmitSpec
from hpc_agent.ops.monitor.reconcile import _ssh_alive_job_ids
from hpc_agent.state import session
from hpc_agent.state.session import RunRecord, run_record

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    monkeypatch.setattr(session, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _seed_run(experiment: Path, **overrides) -> RunRecord:
    base = {
        "run_id": "ml_ridge_abcd1234",
        "profile": "ml_ridge",
        "cluster": "hoffman2",
        "ssh_target": "user@hoffman2.idre.ucla.edu",
        "remote_path": "/u/scratch/exp",
        "job_name": "ml_ridge",
        "job_ids": ["12345678"],
        "total_tasks": 100,
        "submitted_at": "2026-04-26T17:00:00+00:00",
        "experiment_dir": str(experiment.resolve()),
    }
    base.update(overrides)
    record = RunRecord(**base)
    session.upsert_run(experiment, record)
    return record


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_submit_and_record_writes_journal(journal_home, experiment):
    record, deduped = runner.submit_and_record(
        experiment,
        spec=_WireSubmitSpec(
            profile="ml_ridge",
            cluster="hoffman2",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_name="ml_ridge",
            run_id="ml_ridge_abcd1234",
            job_ids=["12345678"],
            total_tasks=100,
        ),
    )
    assert deduped is False
    assert record.run_id == "ml_ridge_abcd1234"
    assert record.status == "in_flight"
    assert record.stage == "monitor"

    loaded = session.load_run(experiment, record.run_id)
    assert loaded is not None
    assert loaded.job_ids == ["12345678"]
    assert loaded.total_tasks == 100


def test_submit_and_record_dedups_replay(journal_home, experiment):
    """Second call with the same run_id returns the existing record + deduped=True."""
    base = dict(
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        run_id="ml_ridge_abcd1234",
        total_tasks=100,
    )
    first, first_dedup = runner.submit_and_record(
        experiment,
        spec=_WireSubmitSpec(**base, job_ids=["12345678"]),
    )
    assert first_dedup is False

    # Replay with new job_ids should be ignored — dedup means the existing
    # record is returned untouched, so retries can't double-submit.
    second, second_dedup = runner.submit_and_record(
        experiment,
        spec=_WireSubmitSpec(**base, job_ids=["99999999"]),
    )
    assert second_dedup is True
    assert second.run_id == first.run_id
    assert second.job_ids == ["12345678"]  # original wins


def test_combine_wave_records_success(journal_home, experiment):
    _seed_run(experiment)
    with patch("hpc_agent.infra.remote.run_combiner_checked", return_value=(True, "ok", "")) as m:
        ok, _, _ = runner.combine_wave(
            experiment,
            "ml_ridge_abcd1234",
            wave=2,
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
        )
    assert ok is True
    m.assert_called_once()
    final = session.load_run(experiment, "ml_ridge_abcd1234")
    assert final.combined_waves == [2]
    assert final.failed_waves == []


def test_combine_wave_records_failure(journal_home, experiment):
    _seed_run(experiment)
    with patch("hpc_agent.infra.remote.run_combiner_checked", return_value=(False, "", "boom")):
        ok, _, _ = runner.combine_wave(
            experiment,
            "ml_ridge_abcd1234",
            wave=3,
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
        )
    assert ok is False
    final = session.load_run(experiment, "ml_ridge_abcd1234")
    assert final.combined_waves == []
    assert final.failed_waves == [3]


def test_combine_wave_failed_then_success_clears_failure(journal_home, experiment):
    _seed_run(experiment)
    with patch("hpc_agent.infra.remote.run_combiner_checked", return_value=(False, "", "boom")):
        runner.combine_wave(
            experiment,
            "ml_ridge_abcd1234",
            wave=4,
            ssh_target="user@h",
            remote_path="/x",
        )
    with patch("hpc_agent.infra.remote.run_combiner_checked", return_value=(True, "ok", "")):
        runner.combine_wave(
            experiment,
            "ml_ridge_abcd1234",
            wave=4,
            ssh_target="user@h",
            remote_path="/x",
            force=True,
        )
    final = session.load_run(experiment, "ml_ridge_abcd1234")
    assert final.combined_waves == [4]
    assert final.failed_waves == []


def test_resubmit_failed_increments_retries(journal_home, experiment):
    _seed_run(experiment)

    runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[3, 7],
            category="system_oom",
            overrides={"mem": "32G"},
            new_job_ids=["99999999"],
        ),
    )
    after_one = session.load_run(experiment, "ml_ridge_abcd1234")
    assert after_one.retries == {
        "3": {"attempts": 1, "category": "system_oom", "overrides": {"mem": "32G"}},
        "7": {"attempts": 1, "category": "system_oom", "overrides": {"mem": "32G"}},
    }
    assert after_one.job_ids == ["99999999"]

    runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[3],
            category="system_oom",
            overrides={"mem": "64G"},
        ),
    )
    after_two = session.load_run(experiment, "ml_ridge_abcd1234")
    assert after_two.retries["3"] == {
        "attempts": 2,
        "category": "system_oom",
        "overrides": {"mem": "64G"},
    }
    assert after_two.retries["7"]["attempts"] == 1
    assert after_two.job_ids == ["99999999"]


def test_record_status_sets_checked_at(journal_home, experiment):
    _seed_run(experiment)
    payload = {"summary": {"complete": 7, "running": 3, "pending": 0, "failed": 1, "unknown": 0}}
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        record = runner.record_status(
            experiment,
            "ml_ridge_abcd1234",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_ids=["12345678"],
            job_name="ml_ridge",
        )
    assert record.last_status["complete"] == 7
    assert "checked_at" in record.last_status


def test_record_status_threads_min_rows_to_cluster_cmd(journal_home, experiment):
    """`--min-rows` set on record_status reaches the cluster-side reporter cmd."""
    _seed_run(experiment)
    payload = {"summary": {"complete": 7, "running": 0, "pending": 0, "failed": 0, "unknown": 0}}
    seen_cmds: list[str] = []

    def fake_ssh(cmd, *, ssh_target, **_kw):
        seen_cmds.append(cmd)
        return _completed(stdout=json.dumps(payload))

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        runner.record_status(
            experiment,
            "ml_ridge_abcd1234",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_ids=["12345678"],
            job_name="ml_ridge",
            min_rows=5,
        )
    assert seen_cmds, "ssh_run was never called"
    assert "--min-rows 5" in seen_cmds[0]


def test_record_status_min_rows_defaults_to_zero(journal_home, experiment):
    """Default record_status emits `--min-rows 0` (accepts header-only CSVs)."""
    _seed_run(experiment)
    payload = {"summary": {"complete": 7, "running": 0, "pending": 0, "failed": 0, "unknown": 0}}
    seen_cmds: list[str] = []

    def fake_ssh(cmd, *, ssh_target, **_kw):
        seen_cmds.append(cmd)
        return _completed(stdout=json.dumps(payload))

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        runner.record_status(
            experiment,
            "ml_ridge_abcd1234",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            job_ids=["12345678"],
            job_name="ml_ridge",
        )
    assert "--min-rows 0" in seen_cmds[0]


def test_reconcile_overwrites_drifted_combined_waves(journal_home, experiment):
    _seed_run(experiment, combined_waves=[0, 1, 2], failed_waves=[2])
    status_payload = json.dumps({"summary": {"complete": 50, "running": 50, "failed": 0}})
    cluster_waves = "_combiner/wave_0.json\n_combiner/wave_2.json\n"
    alive_squeue = "12345678\n"

    def fake_ssh(cmd, *, ssh_target):
        if "python -m hpc_agent.models.mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout=cluster_waves)
        return _completed(stdout=alive_squeue)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")

    assert record.combined_waves == [0, 2]
    assert record.failed_waves == []
    assert record.status == "in_flight"


def test_reconcile_marks_abandoned_when_no_jobs_alive(journal_home, experiment):
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 0, "running": 0, "failed": 0}})

    def fake_ssh(cmd, *, ssh_target):
        if "python -m hpc_agent.models.mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout="")
        return _completed(stdout="")

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")
    assert record.status == "abandoned"
    assert session.find_in_flight_runs(experiment) == []


def test_reconcile_idempotent(journal_home, experiment):
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 100, "running": 0, "failed": 0}})
    cluster_waves = "_combiner/wave_0.json\n_combiner/wave_1.json\n"
    alive = "12345678\n"

    def fake_ssh(cmd, *, ssh_target):
        if "python -m hpc_agent.models.mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout=cluster_waves)
        return _completed(stdout=alive)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        first = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")
        second = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")
    assert first.combined_waves == [0, 1]
    assert second.combined_waves == [0, 1]


def test_mark_terminal_pass_through(journal_home, experiment):
    _seed_run(experiment)
    runner.mark_terminal(experiment, "ml_ridge_abcd1234", status="complete", stage="done")
    record = session.load_run(experiment, "ml_ridge_abcd1234")
    assert record.status == "complete"
    assert record.stage == "done"


def test_validate_ssh_target_accepts_alias_and_userhost():
    from hpc_agent.infra.remote import validate_ssh_target

    # Both forms are accepted: an OpenSSH config alias and explicit user@host.
    assert validate_ssh_target("usc-discovery") == "usc-discovery"
    assert validate_ssh_target("alice@cluster.example") == "alice@cluster.example"


def test_validate_ssh_target_rejects_empty_and_shell_chars():
    from hpc_agent.infra.remote import validate_ssh_target

    with pytest.raises(ValueError, match="non-empty"):
        validate_ssh_target("")
    with pytest.raises(ValueError, match="disallowed"):
        validate_ssh_target("alice@host; rm -rf /")


# ─── Bug 4: SGE alive-check actually checks qstat exit codes ───────────────


def test_sge_alive_check_returns_empty_when_qstat_silent():
    """Empty ``qstat -u $USER`` output means no live jobs — the alive set
    must be empty so ``reconcile`` can flag abandoned runs.

    Hot-path perf: a single batched ``qstat -u $USER`` call replaces the
    previous per-job ``qstat -j <jid>`` loop (which spawned N subprocesses
    per poll on the cluster head node).
    """
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout=""),
    ) as m:
        alive = _ssh_alive_job_ids(
            ssh_target="user@host",
            job_ids=["123", "456"],
            scheduler="sge",
        )
    assert alive == set()
    sent_cmd = m.call_args[0][0]
    # Exactly one qstat invocation, querying by user (not per-job ``-j``).
    assert sent_cmd.count("qstat") == 1
    assert "-u" in sent_cmd
    assert "qstat -j" not in sent_cmd
    # Still falls back to rc 0 so the SSH transport guard isn't tripped
    # by an empty queue.
    assert "|| true" in sent_cmd


def test_sge_alive_check_filters_qstat_output_to_requested_ids():
    """Lines from ``qstat -u $USER`` whose first column matches a requested
    job id count as alive; unrelated co-tenant jobs and header rows are
    ignored.
    """
    qstat_out = (
        "job-ID  prior   name       user         state submit/start at     queue\n"
        "------------------------------------------------------------------------\n"
        "    123 0.50000 myjob      alice        r     05/23/2026 10:00:00 q@n1\n"
        "    456 0.50000 myjob      alice        qw    05/23/2026 10:00:00\n"
        "    999 0.50000 otherjob   alice        r     05/23/2026 09:00:00 q@n2\n"
    )
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout=qstat_out),
    ):
        alive = _ssh_alive_job_ids(
            ssh_target="user@host",
            job_ids=["123", "456"],
            scheduler="sge",
        )
    # 999 belongs to another run and must not leak in.
    assert alive == {"123", "456"}


# ─── Bug 5: Slurm alive-check no longer trusts sacct history ──────────────


def test_slurm_alive_check_skips_sacct_so_completed_jobs_drop_off():
    """``sacct -j <ids>`` returns historical jobs (completed, cancelled,
    failed); previously the code unioned squeue + sacct, so any job that
    ever ran was considered alive forever.  The fix skips sacct entirely
    and trusts squeue (which only lists active states).
    """
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout=""),  # squeue: no active jobs
    ) as m:
        alive = _ssh_alive_job_ids(
            ssh_target="user@host",
            job_ids=["123"],
            scheduler="slurm",
        )
    assert alive == set()
    sent_cmd = m.call_args[0][0]
    assert "squeue" in sent_cmd
    assert "sacct" not in sent_cmd  # historical jobs no longer leak in


def test_slurm_alive_check_accepts_squeue_output():
    """Squeue lines containing the job id (possibly suffixed with array
    indices like ``123_4``) still count as alive.
    """
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout="123_4\n123_5\n"),
    ):
        alive = _ssh_alive_job_ids(
            ssh_target="user@host",
            job_ids=["123"],
            scheduler="slurm",
        )
    assert alive == {"123"}


# ─── Bug 2: reconcile is fault-tolerant on every SSH future ──────────────


def test_reconcile_falls_back_when_wave_listing_ssh_fails(journal_home, experiment):
    """A network blip on ``_ssh_list_combined_waves`` used to abort the
    whole reconcile after the status side-call had already finished;
    now it falls back to the journaled list and records a warning.
    """
    _seed_run(experiment, combined_waves=[5])
    status_payload = json.dumps({"summary": {"complete": 1}})
    alive_squeue = "12345678\n"

    def fake_ssh(cmd, *, ssh_target):
        if "python -m hpc_agent.models.mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            raise OSError("ssh: connection reset by peer")
        return _completed(stdout=alive_squeue)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")

    assert record.combined_waves == [5]  # unchanged, fallback used
    assert "warnings" in record.last_status
    assert any("wave list" in w for w in record.last_status["warnings"])


def test_reconcile_does_not_mark_abandoned_when_alive_check_ssh_fails(journal_home, experiment):
    """The dangerous edge case: if the *alive* SSH call itself fails, we
    must not flip a healthy run to ``abandoned``.  Previously the
    fault-tolerant path returned an empty alive set, falling straight
    through into the abandonment branch.
    """
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 0}})

    def fake_ssh(cmd, *, ssh_target):
        if "python -m hpc_agent.models.mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout="")
        raise OSError("alive check ssh failed")

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")

    assert record.status == "in_flight"  # NOT abandoned
    assert "warnings" in record.last_status
    assert any("alive check" in w for w in record.last_status["warnings"])


# ─── Bug 9: last_status.json cache is written atomically ──────────────────


def test_record_status_cache_is_atomic(journal_home, experiment, tmp_path):
    """A reader that opens the cache mid-write must not see a truncated
    file.  Atomic writes (tempfile + os.replace) guarantee any successful
    open returns a fully-formed JSON document.
    """
    _seed_run(experiment)
    payload = {"summary": {"complete": 1, "running": 0, "failed": 0, "unknown": 0}}
    with patch(
        "hpc_agent.infra.remote.ssh_run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        runner.record_status(
            experiment,
            "ml_ridge_abcd1234",
            ssh_target="user@host",
            remote_path="/x",
            job_ids=["1"],
            job_name="job",
        )
    cache = session.runs_dir(experiment) / "ml_ridge_abcd1234.last_status.json"
    assert cache.exists()
    # Round-trip parse — would raise on a half-written file.
    body = json.loads(cache.read_text())
    assert body["complete"] == 1


def test_record_status_cache_write_skips_fsync(journal_home, experiment):
    """Hot-path perf: the ``last_status.json`` cache write must NOT fsync.

    On a monitor tick the journal record write fsyncs (durable source of
    truth); the cache write is a strict denormalization of that same
    payload and would double the per-tick fsync cost on networked
    filesystems. The cache write is best-effort and self-heals on the
    next tick, so it passes ``fsync=False`` to ``atomic_write_json``.

    Count ``os.fsync`` calls and split them by which file the underlying
    fd points at — the cache temp file must never appear in the
    fsync'd set.
    """
    import os

    _seed_run(experiment)
    payload = {"summary": {"complete": 1, "running": 0, "failed": 0, "unknown": 0}}

    real_fsync = os.fsync
    fsynced_paths: list[str] = []

    def tracking_fsync(fd: int) -> None:
        # Resolve the fd to a path so we can attribute each fsync to a
        # specific file (NOT just count calls). The journal/index writes
        # legitimately fsync; the cache write must not.
        try:
            path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            path = "<unknown>"
        fsynced_paths.append(path)
        return real_fsync(fd)

    with (
        patch(
            "hpc_agent.infra.remote.ssh_run",
            return_value=_completed(stdout=json.dumps(payload)),
        ),
        patch("os.fsync", side_effect=tracking_fsync),
    ):
        runner.record_status(
            experiment,
            "ml_ridge_abcd1234",
            ssh_target="user@host",
            remote_path="/x",
            job_ids=["1"],
            job_name="job",
        )

    cache = session.runs_dir(experiment) / "ml_ridge_abcd1234.last_status.json"
    assert cache.exists(), "cache file must still be written, just without fsync"

    # Each ``atomic_write_json(fsync=True)`` call contributes exactly one
    # file-fd fsync (the data fsync — the parent-dir fsync opens a
    # separate fd via ``os.open`` and runs through the same hook). The
    # journal record + index = at most 4 fsyncs (2 data + 2 dir). The
    # cache must add ZERO. Assert the cache temp-file path never shows
    # up in the fsynced set.
    cache_dir = str(session.runs_dir(experiment))
    cache_temp_fsyncs = [
        p
        for p in fsynced_paths
        # mkstemp pattern: <name>.<random>.tmp inside the cache dir
        if p.startswith(cache_dir) and ".last_status.json." in p and p.endswith(".tmp")
    ]
    assert cache_temp_fsyncs == [], (
        "cache write must not fsync its temp file (hot-path perf fix); "
        f"saw fsyncs on: {cache_temp_fsyncs}"
    )


# ─── aggregate verification helpers ────────────────────────────────────────


def test_verify_per_task_outputs_returns_missing_paths(journal_home, experiment):
    """Wave's task ids are read from sidecar.wave_map; one SSH call enumerates
    missing files."""
    sidecar_json = json.dumps(
        {
            "sidecar_schema_version": 1,
            "task_count": 3,
            "wave_map": {"0": [0, 1, 2]},
        }
    )

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=sidecar_json)
        # Existence check: pretend task 1's output is missing.
        return _completed(stdout="MISSING:results/metrics.1.json\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        missing = runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            run_id="run_abcd1234",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    assert missing == ["results/metrics.1.json"]


def test_verify_per_task_outputs_returns_empty_when_all_present(journal_home, experiment):
    sidecar_json = json.dumps(
        {"sidecar_schema_version": 1, "task_count": 2, "wave_map": {"0": [0, 1]}}
    )

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=sidecar_json)
        return _completed(stdout="", returncode=0)  # nothing missing

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        missing = runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            run_id="run_x",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    assert missing == []


def test_verify_per_task_outputs_falls_back_to_all_tasks_without_wave_map(journal_home, experiment):
    """A sidecar without wave_map (un-batched single-array submission) treats
    wave 0 as 'every task in [0, task_count)'."""
    sidecar_json = json.dumps({"sidecar_schema_version": 1, "task_count": 2})

    captured: list[str] = []

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        captured.append(cmd)
        if cmd.startswith("cat "):
            return _completed(stdout=sidecar_json)
        return _completed(stdout="", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            run_id="run_x",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    # The existence-check script should mention both task ids.
    check_cmd = next(c for c in captured if not c.startswith("cat "))
    assert "metrics.0.json" in check_cmd and "metrics.1.json" in check_cmd


def test_verify_combiner_artifact_ok_for_valid_json():
    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        # python3 -c json.load returns 0; script echoes OK.
        return _completed(stdout="OK\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        ok, detail = runner.verify_combiner_artifact(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
        )
    assert ok is True
    assert detail == "ok"


def test_verify_combiner_artifact_missing():
    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        return _completed(stdout="MISSING\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        ok, detail = runner.verify_combiner_artifact(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
        )
    assert ok is False
    assert "missing" in detail


def test_verify_combiner_artifact_invalid_json():
    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        return _completed(stdout="INVALID_JSON\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        ok, detail = runner.verify_combiner_artifact(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
        )
    assert ok is False
    assert "JSON" in detail


def test_build_provenance_carries_run_metadata(experiment):
    record = _seed_run(experiment, run_id="r_42", profile="prof", cluster="hoffman2")
    prov = runner.build_provenance(record, wave=2)
    assert prov["run_id"] == "r_42"
    assert prov["profile"] == "prof"
    assert prov["cluster"] == "hoffman2"
    assert prov["wave"] == 2
    # combined_at is ISO 8601 with offset; check shape, not exact value.
    assert "T" in prov["combined_at"] and prov["combined_at"].endswith("+00:00")


def test_derive_resubmit_request_id_is_deterministic():
    """Same input → same id, regardless of dict-key order in overrides."""
    a = runner.derive_resubmit_request_id(
        failed_task_ids=[3, 7, 1],  # unsorted
        category="system_oom",
        overrides={"mem": "32G", "walltime": "2:00:00"},
    )
    b = runner.derive_resubmit_request_id(
        failed_task_ids=[1, 3, 7],
        category="system_oom",
        overrides={"walltime": "2:00:00", "mem": "32G"},  # reordered
    )
    assert a == b
    assert a.startswith("rs_")


def test_derive_resubmit_request_id_differs_on_overrides():
    a = runner.derive_resubmit_request_id(
        failed_task_ids=[3], category="walltime", overrides={"walltime": "2:00:00"}
    )
    b = runner.derive_resubmit_request_id(
        failed_task_ids=[3], category="walltime", overrides={"walltime": "4:00:00"}
    )
    assert a != b


def test_resubmit_failed_dedupes_on_repeat(journal_home, experiment):
    """Second call with the same spec returns deduped=True without bumping
    retry counters."""
    _seed_run(experiment)

    rec1, dedup1, rid1 = runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[3],
            category="system_oom",
            overrides={"mem": "32G"},
        ),
    )
    assert dedup1 is False
    assert rec1.retries["3"]["attempts"] == 1

    # Same spec again — should dedupe.
    rec2, dedup2, rid2 = runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[3],
            category="system_oom",
            overrides={"mem": "32G"},
        ),
    )
    assert dedup2 is True
    assert rid2 == rid1
    # Counter must NOT have incremented.
    after = session.load_run(experiment, "ml_ridge_abcd1234")
    assert after.retries["3"]["attempts"] == 1


def test_resubmit_failed_explicit_request_id_dedupes(journal_home, experiment):
    """Caller-supplied request_id is honored for dedupe."""
    _seed_run(experiment)

    _, dedup1, rid1 = runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[3],
            category="system_oom",
            request_id="rs_explicit_abc",
        ),
    )
    assert dedup1 is False
    assert rid1 == "rs_explicit_abc"

    _, dedup2, rid2 = runner.resubmit_failed(
        experiment,
        "ml_ridge_abcd1234",
        spec=ResubmitSpec(
            failed_task_ids=[7],  # different task!
            category="walltime",  # different category!
            request_id="rs_explicit_abc",  # but same id
        ),
    )
    # Same explicit request_id wins over differing spec.
    assert dedup2 is True
    assert rid2 == "rs_explicit_abc"


def test_annotate_clusters_with_retry_advice_tags_eligible_and_blocked(journal_home, experiment):
    """Tasks with attempts < max_attempts are eligible; at-or-over are blocked."""
    record = _seed_run(
        experiment,
        retries={
            "3": {"attempts": 1, "category": "gpu_oom", "overrides": {}},  # at cap (1)
            "7": {"attempts": 0, "category": "gpu_oom", "overrides": {}},  # eligible
        },
    )
    clusters = [
        {
            "category": "gpu_oom",
            "fingerprint": "...",
            "count": 3,
            "task_ids": [3, 7, 12],  # 12 has no prior attempts -> eligible
        },
    ]
    annotated = runner.annotate_clusters_with_retry_advice(
        clusters,
        auto_retry_policy={"gpu_oom": {"max_attempts": 1, "mem_multiplier": 1.5}},
        record=record,
    )
    advice = annotated[0]["retry_advice"]
    assert sorted(advice["eligible_task_ids"]) == [7, 12]
    assert advice["blocked_task_ids"] == [3]
    assert advice["policy"]["mem_multiplier"] == 1.5


def test_annotate_clusters_skips_categories_without_policy(journal_home, experiment):
    record = _seed_run(experiment)
    clusters = [
        {"category": "walltime", "task_ids": [1, 2], "count": 2},
        {"category": "gpu_oom", "task_ids": [3], "count": 1},
    ]
    annotated = runner.annotate_clusters_with_retry_advice(
        clusters,
        auto_retry_policy={"gpu_oom": {"max_attempts": 1}},  # walltime not configured
        record=record,
    )
    assert "retry_advice" in annotated[1]
    assert "retry_advice" not in annotated[0]


def test_fingerprint_strips_volatile_noise():
    """Two failures differing only in path / pid / timestamp share a fingerprint."""
    line_a = (
        "Traceback ...\n"
        "  File '/u/scratch/exp/run_42/train.py', line 87, in <module>\n"
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom"
    )
    line_b = (
        "Traceback ...\n"
        "  File '/u/scratch/exp/run_99/train.py', line 87, in <module>\n"
        "    raise RuntimeError('boom')\n"
        "RuntimeError: boom"
    )
    fp_a = runner.fingerprint_stderr_tail(line_a)
    fp_b = runner.fingerprint_stderr_tail(line_b)
    assert fp_a == fp_b
    assert "RuntimeError: boom" in fp_a


def test_fingerprint_returns_empty_for_empty_input():
    assert runner.fingerprint_stderr_tail("") == ""
    assert runner.fingerprint_stderr_tail(None) == ""
    assert runner.fingerprint_stderr_tail("   \n  ") == ""


def test_cluster_failures_groups_same_fingerprint():
    logs = [
        {"task_id": 1, "content": "RuntimeError: boom"},
        {"task_id": 2, "content": "RuntimeError: boom"},
        {"task_id": 3, "content": "RuntimeError: boom"},
        {"task_id": 4, "content": "ValueError: nope"},
    ]
    clusters = runner.cluster_failures_by_fingerprint(logs)
    assert len(clusters) == 2
    # Sorted by count desc → biggest cluster first.
    assert clusters[0]["count"] == 3
    assert sorted(clusters[0]["task_ids"]) == [1, 2, 3]
    assert clusters[1]["count"] == 1
    assert clusters[1]["task_ids"] == [4]


def test_cluster_failures_categorizes_known_modes():
    logs = [
        {
            "task_id": 1,
            "content": "torch.cuda.OutOfMemoryError: CUDA out of memory.",
        },
        {
            "task_id": 2,
            "content": "slurmstepd: error: ... DUE TO TIME LIMIT ***",
        },
        {"task_id": 3, "content": "ImportError: No module named 'foo'"},
    ]
    clusters = runner.cluster_failures_by_fingerprint(logs)
    cats = {c["category"] for c in clusters}
    assert {"gpu_oom", "walltime", "import_error"}.issubset(cats)


def test_cluster_failures_groups_preempted_tasks():
    """The campus user's bumped jobs (cluster preemption) must group
    under a single ``preempted`` cluster regardless of whether the
    dispatcher's SIGTERM-trap stderr line is in the tail or only the
    exit code (130) is present."""
    logs = [
        # Two tasks with the dispatcher's SIGTERM-trap stderr line.
        {
            "task_id": 1,
            "content": "[hpc-agent] SIGTERM received; cluster preemption imminent\n",
            "exit_code": 130,
        },
        {
            "task_id": 2,
            "content": "[hpc-agent] SIGTERM received; cluster preemption imminent\n",
            "exit_code": 130,
        },
        # One task where the stderr was clipped but exit code is 130.
        {"task_id": 3, "content": "", "exit_code": 130},
    ]
    clusters = runner.cluster_failures_by_fingerprint(logs)
    preempted_clusters = [c for c in clusters if c["category"] == "preempted"]
    # All three tasks land under the preempted category (possibly
    # split across two clusters by fingerprint, since the empty-stderr
    # task has a different fingerprint).
    preempted_tids: list[int] = []
    for c in preempted_clusters:
        preempted_tids.extend(c["task_ids"])
    assert sorted(preempted_tids) == [1, 2, 3]


def test_cluster_failures_buckets_missing_logs():
    logs = [
        {"task_id": 7, "missing": True},
        {"task_id": 8, "missing": True},
    ]
    clusters = runner.cluster_failures_by_fingerprint(logs)
    assert len(clusters) == 1
    assert clusters[0]["category"] == "log_missing"
    assert sorted(clusters[0]["task_ids"]) == [7, 8]


def test_fetch_task_logs_returns_content_for_slurm():
    """SLURM log path: <remote_path>/logs/<job>_<jid>_<tid+1>.err."""
    captured: list[str] = []

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        captured.append(cmd)
        # First job_id attempt found.
        return _completed(stdout="FOUND\nline1\nline2\nline3\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        logs = runner.fetch_task_logs(
            ssh_target="user@host",
            remote_path="/exp",
            job_name="ml",
            job_ids=["12345"],
            scheduler="slurm",
            task_ids=[7],
            lines=50,
        )

    assert len(logs) == 1
    entry = logs[0]
    assert entry["task_id"] == 7
    assert entry["job_id"] == "12345"
    assert entry["path"] == "/exp/logs/ml_12345_8.err"
    assert "line1\nline2\nline3" in entry["content"]


def test_fetch_task_logs_marks_missing_when_all_job_ids_have_no_log():
    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        return _completed(stdout="MISSING\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        logs = runner.fetch_task_logs(
            ssh_target="user@host",
            remote_path="/exp",
            job_name="ml",
            job_ids=["111", "222"],
            scheduler="slurm",
            task_ids=[7],
        )

    assert logs == [{"task_id": 7, "missing": True}]


def test_fetch_task_logs_falls_back_to_earlier_job_id():
    """When the latest job_id has no log, try the next-most-recent."""
    sequence = [
        _completed(stdout="MISSING\n", returncode=0),  # job 222 (newest)
        _completed(stdout="FOUND\nold log\n", returncode=0),  # job 111
    ]

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        return sequence.pop(0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        logs = runner.fetch_task_logs(
            ssh_target="user@host",
            remote_path="/exp",
            job_name="ml",
            job_ids=["111", "222"],  # reversed -> 222 first
            scheduler="slurm",
            task_ids=[7],
        )

    assert logs[0]["job_id"] == "111"
    assert "old log" in logs[0]["content"]


def test_fetch_task_logs_uses_sge_path_for_sge_scheduler():
    captured: list[str] = []

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        captured.append(cmd)
        return _completed(stdout="FOUND\nbody\n", returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        logs = runner.fetch_task_logs(
            ssh_target="user@host",
            remote_path="/exp",
            job_name="ml",
            job_ids=["12345"],
            scheduler="sge",
            task_ids=[7],
        )

    assert logs[0]["path"] == "/exp/logs/ml.o12345.8"


def test_write_remote_provenance_writes_sidecar_path():
    captured: list[str] = []

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        captured.append(cmd)
        return _completed(returncode=0)

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        path = runner.write_remote_provenance(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
            provenance={"run_id": "r_42"},
        )
    assert path == "/exp/results/_provenance.json"
    # Script should base64-decode into the sidecar path.
    assert any("base64 -d" in c and "_provenance.json" in c for c in captured), captured
