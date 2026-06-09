"""Campaign compute-spend accounting from runtime-prior samples.

``campaign_budget`` enumerates a campaign's runs by ``campaign_id`` (their
sidecars). Each sidecar carries the ``(profile, cluster)`` the run submitted
under and its ``run_id``. The runtime-prior store keys per-task samples by
``(profile, cluster)`` and tags each sample with the ``run_id`` it came from
(see :mod:`hpc_agent.state.runtime_prior`). Joining those two on ``run_id``
yields the **consumed** compute for a campaign:

* ``walltime_sec``  — sum of per-task ``elapsed_sec``
* ``core_hours``    — sum of ``elapsed_sec × effective_cores`` / 3600, where
  effective cores come from the same ``cpu_seconds_used / elapsed_sec``
  estimate the planner uses (:func:`runtime_prior.cores_used_from_sample`);
  when a sample lacks ``cpu_seconds_used`` it contributes its walltime but
  NOT core-hours, and the run is flagged as partial-coverage for cores.
* ``gpu_hours``     — sum of ``elapsed_sec`` for samples whose ``gpu_type``
  is a real GPU (non-empty, not ``"cpu"``) / 3600, assuming one GPU per task
  (the runtime-prior sample does not record a per-task GPU count, so this is
  a one-GPU-per-task lower bound, surfaced honestly as such).

This replaces the old ``_spent_walltime_sec`` that always returned ``0.0``
because it read a sidecar key (``last_status['tasks']``) that never existed —
so the ``max_walltime_sec`` cap never fired.

Honest partial coverage: a run with **no** runtime-prior samples (never
ingested, or pre-dates sample collection) accounts 0 for that run and is
listed under ``coverage.runs_without_samples`` rather than silently folded
into a global 0. The caller can see exactly which runs are uncounted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent.state.runtime_prior import cores_used_from_sample, read_samples

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["consumed_compute_for_campaign"]


def _is_gpu_type(gpu_type: Any) -> bool:
    """True when *gpu_type* names a real GPU (so the task burned GPU-hours).

    The runtime-prior sample's ``gpu_type`` is ``""`` for CPU-only tasks
    (see ``ingest_runtime_samples_from_combiner_dir`` which coerces a
    missing type to ``""``). Treat empty / ``"cpu"`` / ``"none"`` as
    non-GPU; everything else (a100, h100, v100, …) as a GPU.
    """
    if not gpu_type:
        return False
    return str(gpu_type).strip().lower() not in {"cpu", "none", ""}


def consumed_compute_for_campaign(
    experiment_dir: Path,
    sidecars: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sum consumed walltime / core-hours / gpu-hours over a campaign's runs.

    *sidecars* is the list ``campaign_budget`` already enumerated by
    ``campaign_id`` (each carries ``run_id`` + ``profile`` + ``cluster``).
    Returns::

        {
          "walltime_sec": int,          # Σ elapsed_sec across all matched tasks
          "core_hours": float,          # Σ elapsed_sec × cores / 3600
          "gpu_hours": float,           # Σ elapsed_sec(GPU tasks) / 3600
          "tasks_counted": int,         # task-samples that contributed
          "coverage": {
            "runs_total": int,
            "runs_with_samples": int,
            "runs_without_samples": [run_id, ...],   # honest zeros
            "tasks_missing_core_estimate": int,      # had walltime, no cpu_sec
            "partial": bool,                         # any run/task uncounted
          },
        }

    The join is per ``(profile, cluster)``: we read the runtime-prior sample
    list once per distinct ``(profile, cluster)`` the campaign's sidecars
    name, index it by ``run_id``, then attribute each run's samples. Runs
    whose sidecar lacks profile/cluster (legacy v1) or that have no samples
    contribute 0 and are recorded under ``runs_without_samples``.
    """
    # Group the campaign's runs by (profile, cluster) so each runtime-prior
    # file is read exactly once, then bucket that file's samples by run_id.
    run_ids_by_pc: dict[tuple[str, str], set[str]] = {}
    all_run_ids: list[str] = []
    runs_without_pc: list[str] = []
    for sc in sidecars:
        run_id = str(sc.get("run_id") or "")
        if not run_id:
            continue
        all_run_ids.append(run_id)
        profile = sc.get("profile")
        cluster = sc.get("cluster")
        if not profile or not cluster:
            # A legacy sidecar with no (profile, cluster) can't be joined to
            # the runtime-prior store (which is keyed on that pair). Account
            # it as uncounted rather than guessing a key.
            runs_without_pc.append(run_id)
            continue
        run_ids_by_pc.setdefault((str(profile), str(cluster)), set()).add(run_id)

    total_walltime = 0
    total_core_hours = 0.0
    total_gpu_hours = 0.0
    tasks_counted = 0
    tasks_missing_core = 0
    runs_with_samples: set[str] = set()

    for (profile, cluster), wanted_run_ids in run_ids_by_pc.items():
        # only_successful=False: a task that ran for hours and then failed
        # still consumed that walltime / those core-hours. Budget accounting
        # is about spend, not about successful spend.
        samples = read_samples(
            experiment_dir,
            profile=profile,
            cluster=cluster,
            only_successful=False,
        )
        for s in samples:
            run_id = str(s.get("run_id") or "")
            if run_id not in wanted_run_ids:
                continue
            try:
                elapsed = int(s.get("elapsed_sec") or 0)
            except (TypeError, ValueError):
                continue
            if elapsed <= 0:
                continue
            runs_with_samples.add(run_id)
            tasks_counted += 1
            total_walltime += elapsed

            cores = cores_used_from_sample(s, elapsed)
            if cores is None:
                tasks_missing_core += 1
            else:
                total_core_hours += (elapsed * cores) / 3600.0

            if _is_gpu_type(s.get("gpu_type")):
                total_gpu_hours += elapsed / 3600.0

    # The two uncounted-run buckets are disjoint: ``runs_without_profile_cluster``
    # is the legacy-sidecar bucket (no join key), so a run already recorded there
    # is NOT repeated in ``runs_without_samples`` (which is "had a join key but no
    # samples"). Keeping them disjoint lets a consumer sum the two without
    # double-counting a single uncounted run.
    _without_pc = set(runs_without_pc)
    runs_without_samples = [
        rid for rid in all_run_ids if rid not in runs_with_samples and rid not in _without_pc
    ]
    partial = bool(runs_without_samples) or bool(runs_without_pc) or tasks_missing_core > 0

    return {
        "walltime_sec": int(total_walltime),
        "core_hours": round(total_core_hours, 4),
        "gpu_hours": round(total_gpu_hours, 4),
        "tasks_counted": tasks_counted,
        "coverage": {
            "runs_total": len(all_run_ids),
            "runs_with_samples": len(runs_with_samples),
            "runs_without_samples": runs_without_samples,
            "runs_without_profile_cluster": runs_without_pc,
            "tasks_missing_core_estimate": tasks_missing_core,
            "partial": partial,
        },
    }
