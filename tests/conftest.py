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
(:func:`hpc_agent.state.runs.read_run_sidecar`) backfills v1 to v2
on read.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


# Populate the primitive registry at conftest IMPORT time so test
# modules whose top-level imports trigger ``@primitive(composes=[...])``
# decorators with string-name composes find the dependency primitives
# already registered. Pytest fixtures (including the session-scoped
# autouse one below) run during execution; collection is too late for
# import-time decorator errors. The call is idempotent.
from hpc_agent import register_primitives as _register_primitives_at_collection_time  # noqa: E402

_register_primitives_at_collection_time()


# Default sidecar fields reproduced verbatim from the seven existing
# call sites. Test overrides take precedence; anything not overridden
# matches the historical fixture.
_DEFAULT_SIDECAR: dict[str, Any] = {
    "sidecar_schema_version": 1,
    "cmd_sha": "deadbeef" * 8,
    "hpc_agent_version": "0.0.0+test",
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
    # Use repr() rather than json.dumps so tests can exercise richer Python
    # literals (tuples, sets, etc.) that production handles fine.
    tasks_py.write_text(
        f"_TASKS = {tasks!r}\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )
    return tasks_py


@pytest.fixture(scope="session", autouse=True)
def _register_primitives_once() -> None:
    """Populate the @primitive registry once per pytest session.

    The C\u2032-v2 spine no longer auto-imports primitive-bearing modules
    on first registry query; ``register_primitives()`` must be called
    explicitly. Tests that exercise ``get_registry`` / ``get_meta``
    would otherwise hit the new RuntimeError. Idempotent.

    The duplicate top-level call below (executed at conftest IMPORT
    time, before pytest collection scans test files) covers the case
    where a test module's top-level imports trigger a primitive
    decorator whose ``composes=[...]`` uses string names \u2014 the
    registry must already be populated when that decoration runs.
    Without it, e.g. ``from hpc_agent.ops import aggregate_flow``
    fails at collection with ``ValueError: composes references
    'poll-run-status' which is not a registered primitive``.
    """
    from hpc_agent import register_primitives

    register_primitives()
