"""``GitHubActionsBackend.task_statuses`` derives true per-task status from jobs.

No live GitHub calls: a fake pooled API stands in for ``get_run`` /
``list_jobs`` so the test pins the per-job → ``TaskStatus`` mapping the host
monitor reads through the ``HPCBackend.task_statuses`` hook. The point of the
follow-up: the run's *jobs* (one per matrix task) distinguish a *queued*
(``pending``) task from an *in_progress* (``running``) one — which run-level
liveness alone cannot (#337).
"""

from __future__ import annotations

import pytest
from hpc_agent_github_actions.backend import (
    GitHubActionsBackend,
    _job_to_task_status,
    _task_index_from_job_name,
)


class _FakeAPI:
    """Stand-in for one pooled ``GitHubActionsAPI`` account."""

    def __init__(self, run_status: str, jobs: list[dict[str, object]]) -> None:
        self._run_status = run_status
        self._jobs = jobs

    def get_run(self, run_id: str) -> dict[str, object] | None:
        return {"status": self._run_status}

    def list_jobs(self, run_id: str) -> list[dict[str, object]]:
        return list(self._jobs)


def _job(i: int, status: str, conclusion: str | None = None) -> dict[str, object]:
    return {"name": f"task-{i}", "status": status, "conclusion": conclusion}


def _backend_with(api: _FakeAPI | None) -> GitHubActionsBackend:
    backend = GitHubActionsBackend(repo="o/r", token="t")
    backend._accounts = [api] if api is not None else []  # type: ignore[assignment]
    return backend


def test_pending_vs_running_distinguished_from_jobs() -> None:
    # task 0 finished ok, 1 still queued, 2 in progress, 3 failed — the queued
    # task is the one run-level liveness could never separate from running.
    api = _FakeAPI(
        run_status="in_progress",
        jobs=[
            _job(0, "completed", "success"),
            _job(1, "queued"),
            _job(2, "in_progress"),
            _job(3, "completed", "failure"),
        ],
    )
    statuses = _backend_with(api).task_statuses(["999"], total_tasks=4)
    assert statuses == {0: "complete", 1: "pending", 2: "running", 3: "failed"}


def test_unmaterialised_task_is_pending_while_run_alive_else_failed() -> None:
    # total_tasks=3 but only job 0 has surfaced; run still alive → the rest are
    # pending (cells not yet created), not silently failed.
    alive = _FakeAPI(run_status="queued", jobs=[_job(0, "in_progress")])
    assert _backend_with(alive).task_statuses(["999"], total_tasks=3) == {
        0: "running",
        1: "pending",
        2: "pending",
    }
    # Run finished but job 1 never ran → failed.
    done = _FakeAPI(run_status="completed", jobs=[_job(0, "completed", "success")])
    assert _backend_with(done).task_statuses(["999"], total_tasks=2) == {
        0: "complete",
        1: "failed",
    }


def test_vanished_run_fails_its_tasks() -> None:
    # No account knows the run (retention) → tasks fail, no crash.
    assert _backend_with(None).task_statuses(["999"], total_tasks=2) == {0: "failed", 1: "failed"}


@pytest.mark.parametrize(
    "name,expected",
    [
        ("task-0", 0),
        ("task-42", 42),
        ("task (7)", 7),  # GitHub auto matrix name (older copied workflow)
        ("reduce", None),
        ("prefetch", None),
        ("task-", None),
    ],
)
def test_task_index_parsing(name: str, expected: int | None) -> None:
    assert _task_index_from_job_name(name) == expected


@pytest.mark.parametrize(
    "status,conclusion,expected",
    [
        ("queued", None, "pending"),
        ("waiting", None, "pending"),
        ("in_progress", None, "running"),
        ("completed", "success", "complete"),
        ("completed", "failure", "failed"),
        ("completed", "timed_out", "failed"),
        ("completed", "cancelled", "failed"),
        ("completed", "skipped", "unknown"),
    ],
)
def test_job_status_mapping(status: str, conclusion: str | None, expected: str) -> None:
    assert _job_to_task_status({"status": status, "conclusion": conclusion}) == expected
