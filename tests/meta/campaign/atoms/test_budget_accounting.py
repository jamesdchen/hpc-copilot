"""Commit-1 accounting: campaign-budget joins runtime-prior samples on run_id.

The pre-fix ``_spent_walltime_sec`` always returned 0.0 (it read a sidecar
key that never existed), so ``max_walltime_sec`` could never fire. These
tests pin that consumed walltime / core-hours / gpu-hours are now summed
from the runtime-prior store, that the walltime cap actually exhausts, and
that runs without samples are reported as honest partial coverage rather
than a silent global zero.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.meta.campaign.atoms.budget import campaign_budget
from hpc_agent.state.runs import write_run_sidecar
from hpc_agent.state.runtime_prior import append_sample

if TYPE_CHECKING:
    from pathlib import Path

_PROFILE = "ml"
_CLUSTER = "hoffman2"


def _seed_run(experiment_dir: Path, *, run_id: str, task_count: int = 2) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=task_count,
        tasks_py_sha="0" * 12,
        campaign_id="camp_a",
        profile=_PROFILE,
        cluster=_CLUSTER,
        remote_path="/u/scratch/exp",
    )


def _seed_sample(
    experiment_dir: Path,
    *,
    run_id: str,
    task_id: int,
    elapsed_sec: int,
    gpu_type: str = "a100",
    cpu_seconds_used: int | None = None,
    exit_code: int = 0,
) -> None:
    append_sample(
        experiment_dir,
        profile=_PROFILE,
        cluster=_CLUSTER,
        run_id=run_id,
        task_id=task_id,
        gpu_type=gpu_type,
        node="d11-07",
        elapsed_sec=elapsed_sec,
        exit_code=exit_code,
        cpu_seconds_used=cpu_seconds_used,
    )


def test_consumed_walltime_summed_across_runs(tmp_path: Path) -> None:
    # Two runs, two tasks each. Total elapsed = 100+200+300+400 = 1000s.
    _seed_run(tmp_path, run_id="run_0000")
    _seed_run(tmp_path, run_id="run_0001")
    # Supply cpu_seconds_used so every task has a core estimate → full coverage.
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=100, cpu_seconds_used=100)
    _seed_sample(tmp_path, run_id="run_0000", task_id=1, elapsed_sec=200, cpu_seconds_used=200)
    _seed_sample(tmp_path, run_id="run_0001", task_id=0, elapsed_sec=300, cpu_seconds_used=300)
    _seed_sample(tmp_path, run_id="run_0001", task_id=1, elapsed_sec=400, cpu_seconds_used=400)

    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["walltime_sec"] == 1000
    assert out["coverage"]["partial"] is False
    assert out["coverage"]["runs_with_samples"] == 2
    assert out["coverage"]["runs_without_samples"] == []


def test_core_hours_from_cpu_seconds(tmp_path: Path) -> None:
    # One task, 3600s elapsed, 7200 cpu-seconds → ceil(7200/3600)=2 cores.
    # core-hours = 3600 * 2 / 3600 = 2.0.
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=3600, cpu_seconds_used=7200)
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["core_hours"] == 2.0
    # GPU task → 1 gpu-hour (3600s / 3600).
    assert out["spent"]["gpu_hours"] == 1.0


def test_max_walltime_sec_cap_now_fires(tmp_path: Path) -> None:
    """The regression the old docstring described: the walltime cap could
    never exhaust because consumed walltime was hard-coded 0.0."""
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=5000)

    over = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=4000)
    assert over["spent"]["walltime_sec"] == 5000
    assert over["exhausted"] is True
    assert "max_walltime_sec" in over["reason"]

    under = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a", max_walltime_sec=6000)
    assert under["exhausted"] is False
    assert under["remaining"]["max_walltime_sec"] == 1000


def test_partial_coverage_reported_honestly(tmp_path: Path) -> None:
    # run_0000 has a sample; run_0001 has none. The uncovered run must be
    # listed, not silently folded into a global zero.
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_run(tmp_path, run_id="run_0001", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=500)

    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["walltime_sec"] == 500
    assert out["coverage"]["partial"] is True
    assert out["coverage"]["runs_without_samples"] == ["run_0001"]
    assert out["coverage"]["runs_with_samples"] == 1


def test_failed_task_walltime_still_counted(tmp_path: Path) -> None:
    # A task that ran for hours then failed still burned that walltime.
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=900, exit_code=1)
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["walltime_sec"] == 900


def test_missing_core_estimate_counts_walltime_not_cores(tmp_path: Path) -> None:
    # Sample has elapsed but no cpu_seconds_used → walltime counted,
    # core-hours not, and the task flagged under partial coverage.
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=600)
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["walltime_sec"] == 600
    assert out["spent"]["core_hours"] == 0.0
    assert out["coverage"]["tasks_missing_core_estimate"] == 1
    assert out["coverage"]["partial"] is True


def test_cpu_only_task_no_gpu_hours(tmp_path: Path) -> None:
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=3600, gpu_type="")
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["spent"]["gpu_hours"] == 0.0
    assert out["spent"]["walltime_sec"] == 3600


# ─── Commit 2: max_core_hours cap (durable surface) ─────────────────────────


def test_max_core_hours_cap_fires(tmp_path: Path) -> None:
    # 7200s elapsed × 2 cores = 4 core-hours.
    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=7200, cpu_seconds_used=14400)
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a", max_core_hours=3.0)
    assert out["spent"]["core_hours"] == 4.0
    assert out["exhausted"] is True
    assert "max_core_hours" in out["reason"]

    under = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a", max_core_hours=10.0)
    assert under["exhausted"] is False
    assert under["remaining"]["max_core_hours"] == 6.0


def test_advance_stops_over_budget_on_core_hours(tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.atoms.advance import campaign_advance

    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=3600, cpu_seconds_used=7200)
    out = campaign_advance(experiment_dir=tmp_path, campaign_id="camp_a", max_core_hours=1.0)
    assert out["decision"] == "stop_over_budget"
    assert "max_core_hours" in out["reason"]


def test_max_core_hours_defaults_from_manifest(tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.manifest import write_manifest

    _seed_run(tmp_path, run_id="run_0000", task_count=1)
    _seed_sample(tmp_path, run_id="run_0000", task_id=0, elapsed_sec=3600, cpu_seconds_used=7200)
    write_manifest(tmp_path, campaign_id="camp_a", budget={"max_core_hours": 1.0})
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="camp_a")
    assert out["budget"]["max_core_hours"] == 1.0
    assert out["exhausted"] is True


def test_init_persists_max_core_hours(tmp_path: Path) -> None:
    from hpc_agent.meta.campaign.atoms.init import campaign_init
    from hpc_agent.meta.campaign.manifest import read_manifest

    campaign_init(experiment_dir=tmp_path, campaign_id="camp_z", max_core_hours=250.0)
    manifest = read_manifest(tmp_path, "camp_z")
    assert manifest is not None
    assert manifest["budget"]["max_core_hours"] == 250.0
