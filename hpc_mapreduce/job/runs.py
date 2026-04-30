"""Per-run sidecars and ``cmd_sha`` computation.

Each ``/submit`` writes a JSON sidecar to
``$EXPERIMENT/.hpc/runs/<run_id>.json`` carrying audit-trail metadata for
the run: identity, executor command, result-dir template, materialized
task count, and the wave map computed by the throughput optimizer.

The user's per-task definition lives in ``$EXPERIMENT/.hpc/tasks.py``
exposing ``total()`` and ``resolve(task_id)``. ``cmd_sha`` is derived from
materializing ``[resolve(i) for i in range(total())]`` and hashing the
sorted-keys JSON line-joined form — every task's full kwargs dict
contributes to the digest, so any change to ``tasks.py`` that affects
task content also changes the run's identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION",
    "compute_cmd_sha",
    "compute_tasks_py_sha",
    "find_existing_runs",
    "find_run_by_cmd_sha",
    "prune_old_runs",
    "read_run_sidecar",
    "run_sidecar_path",
    "write_run_sidecar",
]

# Maximum number of per-experiment run sidecars retained on disk.
# Oldest-first eviction by mtime. Module-level so callers (and tests) can
# monkeypatch.
MAX_RUNS: int = 10

# Sidecar JSON schema version. Bump on incompatible changes.
SIDECAR_SCHEMA_VERSION: int = 1

# A run_id is a timestamp-prefixed identifier produced by the slash command
# layer. Format: ``YYYYMMDD-HHMMSS-<short_sha>``. We only validate loosely
# — anything filesystem-safe that doesn't contain a path separator works.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _runs_dir(experiment_dir: Path) -> Path:
    return Path(experiment_dir) / ".hpc" / "runs"


def run_sidecar_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the canonical path to a run's sidecar (file may not exist)."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return _runs_dir(experiment_dir) / f"{run_id}.json"


def compute_cmd_sha(tasks_module: Any) -> str:
    """Materialize the task list and return a deterministic SHA-256.

    Imports the user's ``tasks.py`` module (already loaded by the caller),
    calls ``total()``, then ``resolve(i)`` for every ``i`` in
    ``range(total())``. Each kwargs dict is normalized to sorted-keys JSON
    and the lines are joined with ``\\n`` before hashing. The resulting
    digest is stable across equivalent task lists and changes whenever any
    kwarg dict changes.

    Returns a 64-char hex string.

    Raises
    ------
    AttributeError
        If *tasks_module* lacks ``total`` or ``resolve``.
    TypeError
        If ``resolve(i)`` does not return a dict.
    """
    n = int(tasks_module.total())
    parts: list[str] = []
    for i in range(n):
        kwargs = tasks_module.resolve(i)
        if not isinstance(kwargs, dict):
            raise TypeError(
                f"tasks.resolve({i}) must return a dict, got {type(kwargs).__name__}"
            )
        parts.append(json.dumps(kwargs, sort_keys=True, separators=(",", ":")))
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_tasks_py_sha(tasks_py_path: Path) -> str:
    """Return SHA-256 of ``tasks.py``'s bytes — diagnostic only."""
    return hashlib.sha256(Path(tasks_py_path).read_bytes()).hexdigest()


def write_run_sidecar(
    experiment_dir: Path,
    *,
    run_id: str,
    cmd_sha: str,
    claude_hpc_version: str,
    submitted_at: str,
    executor: str,
    result_dir_template: str,
    task_count: int,
    tasks_py_sha: str,
    wave_map: dict[str, list[int]] | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write the per-run sidecar JSON. Returns the path written.

    *wave_map* is optional: when present it carries the throughput
    optimizer's task-id-to-wave assignment (str-keyed for JSON
    round-tripping). *extra* is a free-form pocket for callers that want
    to record additional run-scoped metadata without bumping the schema.
    """
    sidecar = {
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "claude_hpc_version": claude_hpc_version,
        "submitted_at": submitted_at,
        "executor": executor,
        "result_dir_template": result_dir_template,
        "task_count": int(task_count),
        "tasks_py_sha": tasks_py_sha,
    }
    if wave_map is not None:
        sidecar["wave_map"] = {str(k): list(v) for k, v in wave_map.items()}
    if extra:
        sidecar["extra"] = extra
    target = run_sidecar_path(experiment_dir, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sidecar, indent=2, sort_keys=True))
    prune_old_runs(experiment_dir, keep=MAX_RUNS)
    return target


def read_run_sidecar(experiment_dir: Path, run_id: str) -> dict:
    """Load and return a run's sidecar dict.

    Raises
    ------
    FileNotFoundError
        If no sidecar exists for *run_id*.
    """
    target = run_sidecar_path(experiment_dir, run_id)
    if not target.is_file():
        raise FileNotFoundError(f"run sidecar not found: {target}")
    return json.loads(target.read_text())


def find_existing_runs(experiment_dir: Path) -> list[Path]:
    """Return every ``.hpc/runs/<id>.json`` file, newest-first by mtime."""
    runs = _runs_dir(experiment_dir)
    if not runs.exists():
        return []
    hits = [p for p in runs.iterdir() if p.is_file() and p.suffix == ".json"]
    hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return hits


def find_run_by_cmd_sha(experiment_dir: Path, cmd_sha: str) -> Path | None:
    """Return the newest sidecar matching *cmd_sha*, or ``None`` if absent.

    Compares the full cmd_sha string. Iterates newest-first so a fresh
    resume detection picks the most recent matching run.
    """
    if not cmd_sha:
        return None
    for path in find_existing_runs(experiment_dir):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("cmd_sha") == cmd_sha:
            return path
    return None


def prune_old_runs(experiment_dir: Path, keep: int = MAX_RUNS) -> list[Path]:
    """Evict oldest sidecars past the retention cap. Returns deleted paths."""
    if keep < 0:
        raise ValueError("keep must be non-negative")
    hits = find_existing_runs(experiment_dir)
    if len(hits) <= keep:
        return []
    deleted: list[Path] = []
    for path in hits[keep:]:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            continue
    return deleted
