"""Runtime priors — quantile rollups of past task runtimes per GPU type.

Each successful task contributes a sample to
``<repo>/.hpc/runtimes/<profile>.<cluster>.json``::

    {
      "profile": "<name>",
      "cluster": "<name>",
      "schema_version": 1,
      "samples": [
        {"run_id": "...", "task_id": 0, "gpu_type": "a100", "node": "d11-07",
         "cmd_sha": "...", "started_at": "...", "ended_at": "...",
         "elapsed_sec": 4150, "exit_code": 0, "peak_gpu_mem_mb": 18432,
         "host_allocmem_pct_at_start": 0.34, "concurrent_user_count_at_start": 2,
         "walltime_requested_sec": 10800},
         ...
      ]
    }

Two responsibilities:

1. **Append samples** as tasks complete (``append_sample``). The file is
   atomically written under flock to handle concurrent writers (a
   monitor session ingesting completed tasks while another submit is in
   flight).
2. **Roll up quantiles** by GPU type for the planner (``roll_up_quantiles``).
   Returns ``{quantiles: {gpu_type: {p50, p95, p99, n_samples}},
   needs_canary: bool}`` — ``needs_canary`` flips True when the file is
   empty or every gpu_type bucket has zero samples after filtering by
   ``cmd_sha`` (when supplied).

This module's responsibilities are distinct from
``hpc_mapreduce.job.throughput``: throughput.py turns a single
duration estimate + cluster constraints into a wave-packed plan, while
this module *produces* the duration estimate from history.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA_VERSION",
    "runtime_path",
    "append_sample",
    "read_samples",
    "roll_up_quantiles",
]

import json
import os
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

SCHEMA_VERSION: int = 1

# Per-(profile, cluster) sample-count cap. Override via HPC_MAX_RUNTIME_SAMPLES.
# A long campaign easily produces hundreds of thousands of tasks; the prior is
# advisory not audit, so trimming oldest-first is safe.
MAX_SAMPLES: int = int(os.environ.get("HPC_MAX_RUNTIME_SAMPLES", "10000"))


def runtime_path(experiment_dir: Path, profile: str, cluster: str) -> Path:
    """Return the runtime-priors file path for ``(profile, cluster)``.

    Resolves *experiment_dir* to an absolute path so writers and readers
    invoked from different working directories see the same file.
    """
    if not profile:
        raise ValueError("profile must be non-empty")
    if not cluster:
        raise ValueError("cluster must be non-empty")
    safe_profile = profile.replace("/", "_")
    return (
        Path(experiment_dir).resolve()
        / ".hpc"
        / "runtimes"
        / f"{safe_profile}.{cluster}.json"
    )


def _empty_doc(profile: str, cluster: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "cluster": cluster,
        "schema_version": SCHEMA_VERSION,
        "samples": [],
    }


def _read_doc(path: Path, profile: str, cluster: str) -> dict[str, Any]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return _empty_doc(profile, cluster)
    except OSError:
        return _empty_doc(profile, cluster)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return _empty_doc(profile, cluster)
    if not isinstance(doc, dict):
        return _empty_doc(profile, cluster)
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("profile", profile)
    doc.setdefault("cluster", cluster)
    if not isinstance(doc.get("samples"), list):
        doc["samples"] = []
    return doc


def _with_locked_doc(
    path: Path,
    profile: str,
    cluster: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Read-modify-write inside a single flock so concurrent writers
    serialize. The read happens *inside* the lock to prevent the
    classic "two writers each see stale state, one's append is lost"
    race.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_doc(path, profile, cluster)
        new_doc = mutate(existing)
        tmp = tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            encoding="utf-8",
        )
        try:
            json.dump(new_doc, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        except BaseException:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        finally:
            if not tmp.closed:
                tmp.close()
        return new_doc
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def append_sample(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    run_id: str,
    task_id: int,
    gpu_type: str,
    node: str,
    elapsed_sec: int,
    exit_code: int = 0,
    cmd_sha: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    peak_gpu_mem_mb: int | None = None,
    host_allocmem_pct_at_start: float | None = None,
    concurrent_user_count_at_start: int | None = None,
    walltime_requested_sec: int | None = None,
) -> Path:
    """Append a single runtime sample. Returns the file path written.

    Idempotent on ``(run_id, task_id)``: a duplicate call replaces the
    existing record rather than appending a second copy. This protects
    against monitor-replay scenarios.

    Concurrency: the read-filter-append-write happens inside a single
    flock so two concurrent writers cannot lose each other's appends.

    Bounded growth: the samples list is capped at :data:`MAX_SAMPLES`
    (default 10k, override via ``HPC_MAX_RUNTIME_SAMPLES``). Oldest-
    first eviction — the priors are advisory, not audit-grade.
    """
    path = runtime_path(experiment_dir, profile, cluster)
    sample = {
        "run_id": run_id,
        "task_id": int(task_id),
        "gpu_type": gpu_type,
        "node": node,
        "cmd_sha": cmd_sha,
        "started_at": started_at,
        "ended_at": ended_at,
        "elapsed_sec": int(elapsed_sec),
        "exit_code": int(exit_code),
        "peak_gpu_mem_mb": peak_gpu_mem_mb,
        "host_allocmem_pct_at_start": host_allocmem_pct_at_start,
        "concurrent_user_count_at_start": concurrent_user_count_at_start,
        "walltime_requested_sec": walltime_requested_sec,
    }

    def _mutate(doc: dict[str, Any]) -> dict[str, Any]:
        samples = [
            s
            for s in doc.get("samples", [])
            if not (s.get("run_id") == run_id and s.get("task_id") == int(task_id))
        ]
        samples.append(sample)
        if len(samples) > MAX_SAMPLES:
            samples = samples[-MAX_SAMPLES:]
        doc["samples"] = samples
        return doc

    _with_locked_doc(path, profile, cluster, _mutate)
    return path


def read_samples(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    cmd_sha: str | None = None,
    only_successful: bool = True,
) -> list[dict[str, Any]]:
    """Return the (optionally filtered) sample list."""
    doc = _read_doc(runtime_path(experiment_dir, profile, cluster), profile, cluster)
    samples: list[dict[str, Any]] = list(doc["samples"])
    if only_successful:
        samples = [s for s in samples if int(s.get("exit_code", 0)) == 0]
    if cmd_sha is not None:
        samples = [s for s in samples if s.get("cmd_sha") == cmd_sha]
    return samples


def _quantile(values: list[int], q: float) -> int:
    """Inclusive linear-interpolated quantile. Permissive on degenerate inputs."""
    if not values:
        return 0
    s = sorted(values)
    if len(s) == 1:
        return int(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return int(round(s[lo] * (1 - frac) + s[hi] * frac))


def roll_up_quantiles(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    cmd_sha: str | None = None,
    quantiles: tuple[float, ...] = (0.5, 0.95, 0.99),
) -> dict[str, Any]:
    """Group samples by ``gpu_type`` and compute quantile distributions.

    Returns::

        {
          "profile": "...", "cluster": "...",
          "now_iso": "...",
          "needs_canary": bool,
          "quantiles": {"a100": {"p50": 4200, "p95": 5400, "p99": 6500,
                                  "n_samples": 12, "min_sec": ..., "max_sec": ...}, ...},
          "total_samples": int,
          "filtered_by_cmd_sha": <cmd_sha or null>,
        }

    ``needs_canary`` is True when no qualifying samples exist for any
    GPU type after filtering — the planner uses this to short-circuit
    into a 1-task canary submit before scoring candidates.
    """
    samples = read_samples(
        experiment_dir, profile=profile, cluster=cluster, cmd_sha=cmd_sha, only_successful=True
    )
    by_gpu: dict[str, list[int]] = {}
    for s in samples:
        gpu = s.get("gpu_type") or ""
        if not gpu:
            continue
        try:
            elapsed = int(s.get("elapsed_sec", 0))
        except (TypeError, ValueError):
            continue
        if elapsed <= 0:
            continue
        by_gpu.setdefault(gpu, []).append(elapsed)

    out_quantiles: dict[str, dict[str, int]] = {}
    for gpu, vals in by_gpu.items():
        entry: dict[str, int] = {}
        for q in quantiles:
            label = f"p{int(round(q * 100))}"
            entry[label] = _quantile(vals, q)
        entry["n_samples"] = len(vals)
        entry["min_sec"] = int(min(vals))
        entry["max_sec"] = int(max(vals))
        try:
            entry["mean_sec"] = int(round(statistics.mean(vals)))
        except statistics.StatisticsError:
            entry["mean_sec"] = entry.get("p50", 0)
        out_quantiles[gpu] = entry

    return {
        "profile": profile,
        "cluster": cluster,
        "now_iso": datetime.now(timezone.utc).isoformat(),
        "needs_canary": len(out_quantiles) == 0,
        "quantiles": out_quantiles,
        "total_samples": len(samples),
        "filtered_by_cmd_sha": cmd_sha,
    }
