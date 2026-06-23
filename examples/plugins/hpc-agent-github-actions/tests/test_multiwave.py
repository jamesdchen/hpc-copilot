"""Multi-wave fan-out keys on GLOBAL task ids end-to-end (#339 increment 5).

A >256-task sweep is submitted as multiple WAVES; each wave dispatches its own
GLOBAL id window so ``task-<i>`` job names, per-task artifacts, ``task_statuses``,
and the combiner's 0-based global ``wave_map`` all agree on the SAME ids with no
collisions. No live GitHub calls: a fake API records dispatch inputs and serves
per-run jobs.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent_github_actions.backend import (
    GitHubActionsBackend,
    _parse_window,
)

from hpc_agent.infra.throughput import (
    WorkloadSpec,
    build_wave_map,
    compute_submission_plan,
)

# --------------------------------------------------------------------------- #
# _parse_window: 1-based "start-end" -> (global 0-based start, count).
# --------------------------------------------------------------------------- #


def test_parse_window_single_wave_starts_at_zero() -> None:
    assert _parse_window("1-256") == (0, 256)


def test_parse_window_later_wave_carries_global_start() -> None:
    # Wave covering global ids 257-512 (1-based) -> 0-based start 256, count 256.
    assert _parse_window("257-512") == (256, 256)
    # A ragged final wave.
    assert _parse_window("513-600") == (512, 88)


def test_parse_window_degenerate_forms() -> None:
    assert _parse_window(None) == (0, 1)  # single non-array job
    assert _parse_window("7") == (6, 1)  # bare 1-based index
    assert _parse_window("5-5") == (4, 1)  # single-element range


# --------------------------------------------------------------------------- #
# Dispatch forwards the GLOBAL window per wave.
# --------------------------------------------------------------------------- #


class _RecordingAPI:
    """Fake pooled account that records every dispatch and serves run jobs."""

    def __init__(self, repo: str = "o/r") -> None:
        self.repo = repo
        self.token = "t"
        self.dispatches: list[dict[str, str]] = []
        self._runs: dict[str, list[dict[str, object]]] = {}
        self._next_run = 7000

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        self.dispatches.append(dict(inputs))

    def find_run(self, *, correlation: str) -> str:
        self._next_run += 1
        run_id = str(self._next_run)
        # Materialise the run's jobs from the just-dispatched window so
        # task_statuses can key on global ids.
        last = self.dispatches[-1]
        start = int(last["task_start"])
        count = int(last["total_tasks"])
        self._runs[run_id] = [
            {"name": f"task-{i}", "status": "completed", "conclusion": "success"}
            for i in range(start, start + count)
        ]
        return run_id

    def get_run(self, run_id: str) -> dict[str, object] | None:
        return {"status": "completed"} if run_id in self._runs else None

    def list_jobs(self, run_id: str) -> list[dict[str, object]]:
        return list(self._runs.get(run_id, []))


def _backend_with(api: _RecordingAPI) -> GitHubActionsBackend:
    backend = GitHubActionsBackend(repo="o/r", token="t", workflow="fan-out.yml")
    backend._accounts = [api]  # type: ignore[assignment]
    return backend


def _submit_window(backend: GitHubActionsBackend, task_range: str, run_id: str) -> str:
    """Drive the backend's real submit edge for one wave's task_range."""
    cmd = backend._build_command(task_range, "sweep", {"HPC_RUN_ID": run_id})
    result = backend._execute_command(cmd, {"HPC_RUN_ID": run_id, "EXECUTOR": "x"}, Path("."))
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_each_wave_dispatches_its_global_window() -> None:
    api = _RecordingAPI()
    backend = _backend_with(api)

    # Three waves of a 600-task sweep at the 256 cap: 256 / 256 / 88.
    _submit_window(backend, "1-256", "run1")
    _submit_window(backend, "257-512", "run1")
    _submit_window(backend, "513-600", "run1")

    windows = [(d["task_start"], d["total_tasks"]) for d in api.dispatches]
    assert windows == [("0", "256"), ("256", "256"), ("512", "88")]


def test_multiwave_task_statuses_key_on_global_ids_no_collision() -> None:
    # A 600-task run submitted as 3 waves; reconcile/monitor calls task_statuses
    # over ALL wave run ids and must get a clean 0..599 map with no collisions.
    api = _RecordingAPI()
    backend = _backend_with(api)

    run_ids = [
        _submit_window(backend, "1-256", "run1"),
        _submit_window(backend, "257-512", "run1"),
        _submit_window(backend, "513-600", "run1"),
    ]

    statuses = backend.task_statuses(run_ids, total_tasks=600)
    # Every global id 0..599 resolved to a real (complete) status — no id was
    # double-claimed by two waves, none left unseen.
    assert set(statuses) == set(range(600))
    assert all(v == "complete" for v in statuses.values())


def test_wave_windows_match_combiner_wave_map_global_ids() -> None:
    # The dispatched windows must cover EXACTLY the same global ids the combiner's
    # wave_map keys on, so per-task artifacts and the aggregate agree.
    api = _RecordingAPI()
    backend = _backend_with(api)

    # Pack 600 tasks at the GHA 256 cap (concurrency 1 → one wave per batch).
    from hpc_agent.infra.constraints import ClusterConstraints

    constraints = ClusterConstraints(max_array_size=256, max_concurrent_jobs=1)
    plan = compute_submission_plan(constraints, WorkloadSpec(total_tasks=600))
    wave_map = build_wave_map(plan)

    # Submit each batch's 1-based task_range; collect the global windows dispatched.
    for batch in plan.batches:
        _submit_window(backend, batch.task_range, "run1")

    dispatched_ids: set[int] = set()
    for d in api.dispatches:
        start = int(d["task_start"])
        count = int(d["total_tasks"])
        dispatched_ids.update(range(start, start + count))

    combiner_ids = {i for ids in wave_map.values() for i in ids}
    assert dispatched_ids == combiner_ids == set(range(600))
