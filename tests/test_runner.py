"""Tests for ``slash_commands.runner`` — the bundled mapreduce + journal ops.

SSH primitives are mocked so the tests exercise the wiring (journal-update
ordering, retry counting, drift reconciliation) without touching a network.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from slash_commands import runner, session
from slash_commands.session import RunRecord


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
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
        "manifest": "manifest.abcd1234.json",
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
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        manifest_filename="manifest.abcd1234.json",
        job_ids=["12345678"],
        total_tasks=100,
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
    """Second call with the same spec returns the existing record + deduped=True."""
    kwargs = dict(
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@hoffman2.idre.ucla.edu",
        remote_path="/u/scratch/exp",
        job_name="ml_ridge",
        manifest_filename="manifest.abcd1234.json",
        job_ids=["12345678"],
        total_tasks=100,
    )
    first, first_dedup = runner.submit_and_record(experiment, **kwargs)
    assert first_dedup is False

    # Replay with new job_ids should be ignored — dedup means the existing
    # record is returned untouched, so retries can't double-submit.
    replay_kwargs = {**kwargs, "job_ids": ["99999999"]}
    second, second_dedup = runner.submit_and_record(experiment, **replay_kwargs)
    assert second_dedup is True
    assert second.run_id == first.run_id
    assert second.job_ids == ["12345678"]  # original wins


def test_combine_wave_records_success(journal_home, experiment):
    _seed_run(experiment)
    with patch("slash_commands.runner.run_combiner_checked", return_value=(True, "ok", "")) as m:
        ok, _, _ = runner.combine_wave(
            experiment, "ml_ridge_abcd1234",
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
    with patch("slash_commands.runner.run_combiner_checked", return_value=(False, "", "boom")):
        ok, _, _ = runner.combine_wave(
            experiment, "ml_ridge_abcd1234",
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
    with patch("slash_commands.runner.run_combiner_checked", return_value=(False, "", "boom")):
        runner.combine_wave(
            experiment, "ml_ridge_abcd1234",
            wave=4, ssh_target="user@h", remote_path="/x",
        )
    with patch("slash_commands.runner.run_combiner_checked", return_value=(True, "ok", "")):
        runner.combine_wave(
            experiment, "ml_ridge_abcd1234",
            wave=4, ssh_target="user@h", remote_path="/x",
            force=True,
        )
    final = session.load_run(experiment, "ml_ridge_abcd1234")
    assert final.combined_waves == [4]
    assert final.failed_waves == []


def test_resubmit_failed_increments_retries(journal_home, experiment):
    _seed_run(experiment)

    runner.resubmit_failed(
        experiment, "ml_ridge_abcd1234",
        failed_task_ids=[3, 7],
        category="system_oom",
        overrides={"mem": "32G"},
        new_job_ids=["99999999"],
    )
    after_one = session.load_run(experiment, "ml_ridge_abcd1234")
    assert after_one.retries == {
        "3": {"attempts": 1, "category": "system_oom", "overrides": {"mem": "32G"}},
        "7": {"attempts": 1, "category": "system_oom", "overrides": {"mem": "32G"}},
    }
    assert after_one.job_ids == ["99999999"]

    runner.resubmit_failed(
        experiment, "ml_ridge_abcd1234",
        failed_task_ids=[3],
        category="system_oom",
        overrides={"mem": "64G"},
    )
    after_two = session.load_run(experiment, "ml_ridge_abcd1234")
    assert after_two.retries["3"] == {
        "attempts": 2, "category": "system_oom", "overrides": {"mem": "64G"},
    }
    assert after_two.retries["7"]["attempts"] == 1
    assert after_two.job_ids == ["99999999"]


def test_record_status_sets_checked_at(journal_home, experiment):
    _seed_run(experiment)
    payload = {"summary": {"complete": 7, "running": 3, "pending": 0, "failed": 1, "unknown": 0}}
    with patch(
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        record = runner.record_status(
            experiment, "ml_ridge_abcd1234",
            ssh_target="user@hoffman2.idre.ucla.edu",
            remote_path="/u/scratch/exp",
            manifest_filename="manifest.abcd1234.json",
            job_ids=["12345678"],
            job_name="ml_ridge",
        )
    assert record.last_status["complete"] == 7
    assert "checked_at" in record.last_status


def test_reconcile_overwrites_drifted_combined_waves(journal_home, experiment):
    _seed_run(experiment, combined_waves=[0, 1, 2], failed_waves=[2])
    status_payload = json.dumps({"summary": {"complete": 50, "running": 50, "failed": 0}})
    cluster_waves = "_combiner/wave_0.json\n_combiner/wave_2.json\n"
    alive_squeue = "12345678\n"

    def fake_ssh(cmd, *, host, user):
        if "python -m hpc_mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout=cluster_waves)
        return _completed(stdout=alive_squeue)

    with patch("slash_commands.runner.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")

    assert record.combined_waves == [0, 2]
    assert record.failed_waves == []
    assert record.status == "in_flight"


def test_reconcile_marks_abandoned_when_no_jobs_alive(journal_home, experiment):
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 0, "running": 0, "failed": 0}})

    def fake_ssh(cmd, *, host, user):
        if "python -m hpc_mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout="")
        return _completed(stdout="")

    with patch("slash_commands.runner.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")
    assert record.status == "abandoned"
    assert session.find_in_flight_runs(experiment) == []


def test_reconcile_idempotent(journal_home, experiment):
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 100, "running": 0, "failed": 0}})
    cluster_waves = "_combiner/wave_0.json\n_combiner/wave_1.json\n"
    alive = "12345678\n"

    def fake_ssh(cmd, *, host, user):
        if "python -m hpc_mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout=cluster_waves)
        return _completed(stdout=alive)

    with patch("slash_commands.runner.ssh_run", side_effect=fake_ssh):
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


def test_resubmit_failed_rejects_empty_list(journal_home, experiment):
    _seed_run(experiment)
    with pytest.raises(ValueError):
        runner.resubmit_failed(
            experiment, "ml_ridge_abcd1234",
            failed_task_ids=[],
            category="system_oom",
        )


def test_split_ssh_target_validates():
    with pytest.raises(ValueError, match="user@host"):
        runner._split_ssh_target("just-a-host")
