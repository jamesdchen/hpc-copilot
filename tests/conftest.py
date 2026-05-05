"""Shared test fixtures and helpers.

Reduces duplication across the seven test files that hand-write a
sidecar JSON and/or a stub ``.hpc/tasks.py``. Helpers are intentionally
plain functions (not pytest fixtures) so callers compose them with
their own ``tmp_path`` and ``monkeypatch``.

- :func:`make_sidecar_json` writes a per-run sidecar at
  ``<dir>/.hpc/runs/<run_id>.json`` with sensible defaults; any field
  may be overridden via kwargs. Returns the path written.
- :func:`write_hpc_tasks` writes a ``.hpc/tasks.py`` exposing
  ``total()`` / ``resolve(i)`` over a list of kwarg dicts. Returns the
  path written.

Both helpers default to the v1 sidecar shape — that is what the
existing fixtures wrote, and the production read path
(:func:`claude_hpc.orchestrator.runs.read_run_sidecar`) backfills v1 to v2
on read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# Default sidecar fields reproduced verbatim from the seven existing
# call sites. Test overrides take precedence; anything not overridden
# matches the historical fixture.
_DEFAULT_SIDECAR: dict[str, Any] = {
    "sidecar_schema_version": 1,
    "cmd_sha": "deadbeef" * 8,
    "claude_hpc_version": "0.0.0+test",
    "submitted_at": "2026-01-01T00:00:00Z",
    "executor": "true",
    "task_count": 1,
    "tasks_py_sha": "abc",
}


def make_sidecar_json(
    tmp_path: Path,
    *,
    run_id: str = "test_run",
    result_dir_template: str | None = None,
    **overrides: Any,
) -> Path:
    """Write ``<tmp_path>/.hpc/runs/<run_id>.json`` and return its path.

    Overrides may include any sidecar field (``executor``,
    ``task_count``, ``wave_map``, ``sidecar_schema_version``, …) and
    are merged on top of the historical defaults.

    *result_dir_template* defaults to ``<tmp_path>/out`` to match the
    most common pattern in the existing tests; pass an explicit value
    when the test cares about format placeholders.
    """
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    sidecar: dict[str, Any] = dict(_DEFAULT_SIDECAR)
    sidecar["run_id"] = run_id
    sidecar["result_dir_template"] = result_dir_template or str(tmp_path / "out")
    sidecar.update(overrides)

    target = runs_dir / f"{run_id}.json"
    target.write_text(json.dumps(sidecar))
    return target


def write_hpc_tasks(hpc_dir: Path, tasks: list[dict[str, Any]]) -> Path:
    """Write a ``.hpc/tasks.py`` stub exposing ``total()``/``resolve()``.

    *hpc_dir* must already exist (call :func:`make_sidecar_json` first
    when both are needed; or create the dir yourself).
    """
    hpc_dir.mkdir(parents=True, exist_ok=True)
    tasks_py = hpc_dir / "tasks.py"
    tasks_py.write_text(
        "import json\n"
        f"_TASKS = {json.dumps(tasks)}\n"
        "def total(): return len(_TASKS)\n"
        "def resolve(i): return _TASKS[i]\n"
    )
    return tasks_py


def seed_diurnal_dip(
    tmp_path: Path,
    *,
    profile: str,
    cluster: str,
    days: int = 14,
    dip_hours: tuple[int, ...] = (3, 4, 5, 6),
    dip_wait_sec: int = 100,
    busy_wait_sec: int = 1500,
) -> None:
    """Seed the runtime-prior pool with a diurnal queue-wait pattern.

    Two samples per hour for *days* days (UTC). Hours in *dip_hours*
    receive ``dip_wait_sec``; all other hours receive ``busy_wait_sec``.
    Used by the queue-wait baseline / best-submit-window / resubmit
    advisor tests, which all need a dense diurnal signal that the
    ±1h blend fallback can recover even when a target bucket is sparse.
    """
    from datetime import datetime, timedelta, timezone

    from claude_hpc.orchestrator import runtime_prior as rp

    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
    dip_set = frozenset(dip_hours)
    for day in range(days):
        for hour in range(24):
            for offset_min in (0, 30):
                ts = base + timedelta(days=day, hours=hour, minutes=offset_min)
                wait = dip_wait_sec if hour in dip_set else busy_wait_sec
                rp.append_sample(
                    tmp_path,
                    profile=profile,
                    cluster=cluster,
                    run_id=f"r{day}-{hour}-{offset_min}",
                    task_id=0,
                    gpu_type="a100",
                    node="d11-07",
                    elapsed_sec=4150,
                    submitted_at_iso=ts.isoformat(),
                    queue_wait_sec=wait,
                )


@pytest.fixture(scope="session", autouse=True)
def _register_primitives_once() -> None:
    """Populate the @primitive registry once per pytest session.

    The C\u2032-v2 spine no longer auto-imports primitive-bearing modules
    on first registry query; ``register_primitives()`` must be called
    explicitly. Tests that exercise ``get_registry`` / ``get_meta``
    would otherwise hit the new RuntimeError. Idempotent.
    """
    from claude_hpc import register_primitives

    register_primitives()
