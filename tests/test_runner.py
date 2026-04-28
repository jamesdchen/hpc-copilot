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


# ─── Bug 15: bad manifest_filename rejected, not silently mangled ──────────


def test_submit_and_record_rejects_non_conforming_manifest_filename(
    journal_home, experiment
):
    """A manifest filename outside ``manifest.<8 hex>.json`` used to silently
    produce garbage run_ids that violated the submit.output.json schema.
    """
    from slash_commands import errors

    with pytest.raises(errors.ManifestInvalid, match="manifest.<8 hex"):
        runner.submit_and_record(
            experiment,
            profile="ml_ridge",
            cluster="hoffman2",
            ssh_target="user@host",
            remote_path="/x",
            job_name="job",
            manifest_filename="latest.json",  # not the canonical pattern
            job_ids=["1"],
            total_tasks=1,
        )


def test_submit_and_record_accepts_explicit_run_id_with_any_filename(
    journal_home, experiment
):
    """Pre-validation only kicks in when the run_id is auto-derived; an
    explicit ``run_id=`` lets callers bypass the pattern (useful for legacy
    manifests we no longer rebuild).
    """
    record, _ = runner.submit_and_record(
        experiment,
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="user@host",
        remote_path="/x",
        job_name="job",
        manifest_filename="anything.json",
        job_ids=["1"],
        total_tasks=1,
        run_id="custom_run",
    )
    assert record.run_id == "custom_run"


# ─── Bug 4: SGE alive-check actually checks qstat exit codes ───────────────


def test_sge_alive_check_returns_empty_when_qstat_silent():
    """Previously the pipeline ``qstat | head -1 && echo __ALIVE__`` always
    fired because the pipeline's exit status came from ``head -1`` (which
    exits 0 even on empty input) — making every SGE alive-check return
    every job_id and ``reconcile`` never marking runs abandoned.
    """
    with patch(
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout=""),
    ) as m:
        alive = runner._ssh_alive_job_ids(
            ssh_target="user@host",
            remote_path="/x",
            job_ids=["123", "456"],
            scheduler="sge",
        )
    assert alive == set()
    # The new command anchors on qstat's *exit code*, not a piped tail.
    sent_cmd = m.call_args[0][0]
    assert ">/dev/null 2>&1" in sent_cmd
    assert "head -1" not in sent_cmd


def test_sge_alive_check_emits_marker_for_each_alive_job():
    """The marker line ``__ALIVE__<jid>`` is still produced (and parsed) for
    jobs that qstat knows about.
    """
    with patch(
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout="__ALIVE__123\n__ALIVE__456\n"),
    ):
        alive = runner._ssh_alive_job_ids(
            ssh_target="user@host",
            remote_path="/x",
            job_ids=["123", "456"],
            scheduler="sge",
        )
    assert alive == {"123", "456"}


# ─── Bug 5: Slurm alive-check no longer trusts sacct history ──────────────


def test_slurm_alive_check_skips_sacct_so_completed_jobs_drop_off():
    """``sacct -j <ids>`` returns historical jobs (completed, cancelled,
    failed); previously the code unioned squeue + sacct, so any job that
    ever ran was considered alive forever.  The fix skips sacct entirely
    and trusts squeue (which only lists active states).
    """
    with patch(
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout=""),  # squeue: no active jobs
    ) as m:
        alive = runner._ssh_alive_job_ids(
            ssh_target="user@host",
            remote_path="/x",
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
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout="123_4\n123_5\n"),
    ):
        alive = runner._ssh_alive_job_ids(
            ssh_target="user@host",
            remote_path="/x",
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

    def fake_ssh(cmd, *, host, user):
        if "python -m hpc_mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            raise OSError("ssh: connection reset by peer")
        return _completed(stdout=alive_squeue)

    with patch("slash_commands.runner.ssh_run", side_effect=fake_ssh):
        record = runner.reconcile(experiment, "ml_ridge_abcd1234", scheduler="slurm")

    assert record.combined_waves == [5]  # unchanged, fallback used
    assert "warnings" in record.last_status
    assert any("wave list" in w for w in record.last_status["warnings"])


def test_reconcile_does_not_mark_abandoned_when_alive_check_ssh_fails(
    journal_home, experiment
):
    """The dangerous edge case: if the *alive* SSH call itself fails, we
    must not flip a healthy run to ``abandoned``.  Previously the
    fault-tolerant path returned an empty alive set, falling straight
    through into the abandonment branch.
    """
    _seed_run(experiment)
    status_payload = json.dumps({"summary": {"complete": 0}})

    def fake_ssh(cmd, *, host, user):
        if "python -m hpc_mapreduce.reduce.status" in cmd:
            return _completed(stdout=status_payload)
        if "_combiner/wave_*.json" in cmd:
            return _completed(stdout="")
        raise OSError("alive check ssh failed")

    with patch("slash_commands.runner.ssh_run", side_effect=fake_ssh):
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
        "slash_commands.runner.ssh_run",
        return_value=_completed(stdout=json.dumps(payload)),
    ):
        runner.record_status(
            experiment, "ml_ridge_abcd1234",
            ssh_target="user@host",
            remote_path="/x",
            manifest_filename="manifest.abcd1234.json",
            job_ids=["1"],
            job_name="job",
        )
    cache = session.runs_dir(experiment) / "ml_ridge_abcd1234.last_status.json"
    assert cache.exists()
    # Round-trip parse — would raise on a half-written file.
    body = json.loads(cache.read_text())
    assert body["complete"] == 1


# ─── aggregate verification helpers ────────────────────────────────────────


def test_verify_per_task_outputs_returns_missing_paths(journal_home, experiment):
    """Wave's task ids are read from manifest.wave_map; one SSH call enumerates
    missing files."""
    manifest_json = json.dumps(
        {
            "schema_version": 2,
            "tasks": {"0": {}, "1": {}, "2": {}},
            "wave_map": {"0": ["0", "1", "2"]},
        }
    )

    def fake_ssh_run(cmd, *, host, user, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=manifest_json)
        # Existence check: pretend task 1's output is missing.
        return _completed(
            stdout="MISSING:results/metrics.1.json\n", returncode=0
        )

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        missing = runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            manifest_filename="manifest.abcd1234.json",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    assert missing == ["results/metrics.1.json"]


def test_verify_per_task_outputs_returns_empty_when_all_present(journal_home, experiment):
    manifest_json = json.dumps(
        {"schema_version": 2, "tasks": {"0": {}, "1": {}}, "wave_map": {"0": ["0", "1"]}}
    )

    def fake_ssh_run(cmd, *, host, user, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=manifest_json)
        return _completed(stdout="", returncode=0)  # nothing missing

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        missing = runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            manifest_filename="m.json",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    assert missing == []


def test_verify_per_task_outputs_falls_back_to_all_tasks_without_wave_map(
    journal_home, experiment
):
    """Older un-batched manifests have no wave_map; treat wave 0 as 'all'."""
    manifest_json = json.dumps({"schema_version": 2, "tasks": {"0": {}, "1": {}}})

    captured: list[str] = []

    def fake_ssh_run(cmd, *, host, user, **_kw):
        captured.append(cmd)
        if cmd.startswith("cat "):
            return _completed(stdout=manifest_json)
        return _completed(stdout="", returncode=0)

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        runner.verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            manifest_filename="m.json",
            wave=0,
            template="results/metrics.{task_id}.json",
        )
    # The existence-check script should mention both task ids.
    check_cmd = next(c for c in captured if not c.startswith("cat "))
    assert "metrics.0.json" in check_cmd and "metrics.1.json" in check_cmd


def test_verify_combiner_artifact_ok_for_valid_json():
    def fake_ssh_run(cmd, *, host, user, **_kw):
        # python3 -c json.load returns 0; script echoes OK.
        return _completed(stdout="OK\n", returncode=0)

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        ok, detail = runner.verify_combiner_artifact(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
        )
    assert ok is True
    assert detail == "ok"


def test_verify_combiner_artifact_missing():
    def fake_ssh_run(cmd, *, host, user, **_kw):
        return _completed(stdout="MISSING\n", returncode=0)

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        ok, detail = runner.verify_combiner_artifact(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
        )
    assert ok is False
    assert "missing" in detail


def test_verify_combiner_artifact_invalid_json():
    def fake_ssh_run(cmd, *, host, user, **_kw):
        return _completed(stdout="INVALID_JSON\n", returncode=0)

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
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
    assert prov["manifest"] == "manifest.abcd1234.json"
    # combined_at is ISO 8601 with offset; check shape, not exact value.
    assert "T" in prov["combined_at"] and prov["combined_at"].endswith("+00:00")


def test_validate_manifest_file_passes_for_clean_manifest(tmp_path: Path):
    """A v2 manifest with resolved cmds and consistent wave_map validates."""
    manifest = {
        "schema_version": 2,
        "total_tasks": 2,
        "tasks": {
            "0": {
                "cmd": "python3 train.py --lr 0.01",
                "result_dir": "results/run_a",
                "params": {"lr": 0.01},
                "cmd_sha": "0" * 16,
            },
            "1": {
                "cmd": "python3 train.py --lr 0.001",
                "result_dir": "results/run_b",
                "params": {"lr": 0.001},
                "cmd_sha": "1" * 16,
            },
        },
        "wave_map": {"0": ["0", "1"]},
    }
    manifest_path = tmp_path / "manifest.abcd1234.json"
    manifest_path.write_text(json.dumps(manifest))
    runner.validate_manifest_file(manifest_path)  # no raise


def test_validate_manifest_file_raises_for_unresolved_placeholder(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": {
                    "0": {
                        "cmd": "python3 train.py --model {model_name}",  # unresolved
                        "result_dir": "results/run",
                        "params": {},
                    }
                },
            }
        )
    )
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(manifest_path)
    assert "unresolved placeholder" in str(exc_info.value)
    assert "{model_name}" in str(exc_info.value)


def test_validate_manifest_file_raises_for_total_tasks_mismatch(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "total_tasks": 5,  # claimed
                "tasks": {  # but only 1 here
                    "0": {"cmd": "x", "result_dir": "y", "params": {}}
                },
            }
        )
    )
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(manifest_path)
    assert "total_tasks" in str(exc_info.value)


def test_validate_manifest_file_raises_for_unknown_schema_version(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "tasks": {"0": {"cmd": "x", "result_dir": "y", "params": {}}},
            }
        )
    )
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(manifest_path)
    assert "schema_version" in str(exc_info.value)


def test_validate_manifest_file_raises_for_wave_map_drift(tmp_path: Path):
    """Wave map references a task id not in tasks."""
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "tasks": {"0": {"cmd": "x", "result_dir": "y", "params": {}}},
                "wave_map": {"0": ["0", "99"]},  # 99 doesn't exist
            }
        )
    )
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(manifest_path)
    assert "wave_map" in str(exc_info.value)
    assert "99" in str(exc_info.value)


def test_validate_manifest_file_raises_for_invalid_json(tmp_path: Path):
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text("not json {")
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(manifest_path)
    assert "not valid JSON" in str(exc_info.value)


def test_validate_manifest_file_raises_for_missing_file(tmp_path: Path):
    with pytest.raises(Exception) as exc_info:
        runner.validate_manifest_file(tmp_path / "does-not-exist.json")
    assert "not found" in str(exc_info.value)


def test_write_remote_provenance_writes_sidecar_path():
    captured: list[str] = []

    def fake_ssh_run(cmd, *, host, user, **_kw):
        captured.append(cmd)
        return _completed(returncode=0)

    with patch.object(runner, "ssh_run", side_effect=fake_ssh_run):
        path = runner.write_remote_provenance(
            ssh_target="user@host",
            remote_path="/exp",
            expect_output="results/metrics.json",
            provenance={"run_id": "r_42"},
        )
    assert path == "/exp/results/_provenance.json"
    # Script should base64-decode into the sidecar path.
    assert any(
        "base64 -d" in c and "_provenance.json" in c for c in captured
    ), captured
