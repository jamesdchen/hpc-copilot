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
import os
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
# monkeypatch. Default raised from 10 to 500 (long campaigns); ``HPC_MAX_RUNS``
# env var overrides at module load.
MAX_RUNS: int = int(os.environ.get("HPC_MAX_RUNS", "500"))

# Sidecar JSON schema version. v2 adds first-class config-snapshot fields
# (resources/env/env_group/constraints/cluster/profile/campaign_id/...) so
# every successful submit captures the full config it ran under and
# subsequent commands have no need for a separate experiment-config file.
# v1 sidecars on disk continue to load via ``read_run_sidecar`` backfill.
SIDECAR_SCHEMA_VERSION: int = 2

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
            raise TypeError(f"tasks.resolve({i}) must return a dict, got {type(kwargs).__name__}")
        parts.append(json.dumps(kwargs, sort_keys=True, separators=(",", ":")))
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_tasks_py_sha(tasks_py_path: Path) -> str:
    """Return SHA-256 of ``tasks.py``'s bytes — diagnostic only."""
    return hashlib.sha256(Path(tasks_py_path).read_bytes()).hexdigest()


# v2 first-class config-snapshot fields. All optional; absent keys are
# omitted from the written sidecar and backfilled to ``None`` (or the
# empty container) on read.
_V2_CONFIG_FIELDS: tuple[str, ...] = (
    "cluster",  # str — cluster key from clusters.yaml
    "profile",  # str — label distinguishing this submission shape
    "campaign_id",  # str — closed-loop campaign tag
    "project",  # str — short project name (paths, logs)
    "remote_path",  # str — absolute path on the remote cluster
    "resources",  # dict — cpus/mem/walltime/gpus/gpu_type
    "env",  # dict — modules/conda_env
    "env_group",  # str — clusters.yaml env_group key
    "constraints",  # dict — overrides on clusters.yaml constraints
    "gpu_fallback",  # list — ordered GPU types to try
    "max_retries",  # int — auto-resubmission cap
    "runtime",  # str — "uv" or omitted
    "auto_retry",  # dict — per-category retry policy
    "aggregate_defaults",  # dict — require_outputs/expect_output/aggregate_cmd
)

# Backfill defaults for v1→v2 read. Containers default to empty so callers
# can use ``or {}`` patterns; scalars default to ``None``.
_V2_BACKFILL_DEFAULTS: dict[str, Any] = {
    "cluster": None,
    "profile": None,
    "campaign_id": None,
    "project": None,
    "remote_path": None,
    "resources": None,
    "env": None,
    "env_group": None,
    "constraints": None,
    "gpu_fallback": None,
    "max_retries": None,
    "runtime": None,
    "auto_retry": None,
    "aggregate_defaults": None,
}


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
    # ----- v2 config-snapshot fields (all optional) -----
    cluster: str | None = None,
    profile: str | None = None,
    campaign_id: str | None = None,
    project: str | None = None,
    remote_path: str | None = None,
    resources: dict[str, Any] | None = None,
    env: dict[str, Any] | None = None,
    env_group: str | None = None,
    constraints: dict[str, Any] | None = None,
    gpu_fallback: list[str] | None = None,
    max_retries: int | None = None,
    runtime: str | None = None,
    auto_retry: dict[str, Any] | None = None,
    aggregate_defaults: dict[str, Any] | None = None,
) -> Path:
    """Write the per-run sidecar JSON. Returns the path written.

    *wave_map* is optional: when present it carries the throughput
    optimizer's task-id-to-wave assignment (str-keyed for JSON
    round-tripping). *extra* is a free-form pocket for callers that want
    to record additional run-scoped metadata without bumping the schema.

    The remaining kwargs (cluster, profile, resources, …) are the v2
    config-snapshot fields. They are all optional at the call site but
    every successful ``/submit`` should populate the ones that apply, so
    subsequent commands (``/aggregate``, ``/status``, ``/resubmit``) can
    rebuild full context without consulting any external config file.
    """
    sidecar: dict[str, Any] = {
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
    # v2 fields — only write keys with non-None values to keep sidecars compact.
    v2_values: dict[str, Any] = {
        "cluster": cluster,
        "profile": profile,
        "campaign_id": campaign_id,
        "project": project,
        "remote_path": remote_path,
        "resources": resources,
        "env": env,
        "env_group": env_group,
        "constraints": constraints,
        "gpu_fallback": gpu_fallback,
        "max_retries": max_retries,
        "runtime": runtime,
        "auto_retry": auto_retry,
        "aggregate_defaults": aggregate_defaults,
    }
    for k, v in v2_values.items():
        if v is not None:
            sidecar[k] = v
    target = run_sidecar_path(experiment_dir, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(sidecar, indent=2, sort_keys=True))
    prune_old_runs(experiment_dir, keep=MAX_RUNS)
    return target


def read_run_sidecar(experiment_dir: Path, run_id: str) -> dict:
    """Load and return a run's sidecar dict.

    v1 sidecars are backfilled with v2 config-snapshot keys defaulting to
    ``None`` so callers can rely on the v2 shape regardless of when the
    sidecar was written.

    Raises
    ------
    FileNotFoundError
        If no sidecar exists for *run_id*.
    """
    target = run_sidecar_path(experiment_dir, run_id)
    if not target.is_file():
        raise FileNotFoundError(f"run sidecar not found: {target}")
    data: dict[str, Any] = json.loads(target.read_text())
    # Backfill missing v2 fields so callers see a uniform shape.
    for k, default in _V2_BACKFILL_DEFAULTS.items():
        data.setdefault(k, default)
    return data


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
