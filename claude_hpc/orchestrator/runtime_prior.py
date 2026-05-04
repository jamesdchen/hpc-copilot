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
``claude_hpc.orchestrator.throughput``: throughput.py turns a single
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
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._io import atomic_locked_update
from claude_hpc._internal._time import parse_iso_utc_or_none, utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA_VERSION: int = 1

# Per-(profile, cluster) sample-count cap. Override via HPC_MAX_RUNTIME_SAMPLES.
# A long campaign easily produces hundreds of thousands of tasks; the prior is
# advisory not audit, so trimming oldest-first is safe.
MAX_SAMPLES: int = int(os.environ.get("HPC_MAX_RUNTIME_SAMPLES", "10000"))


def runtime_path(experiment_dir: Path, profile: str, cluster: str) -> Path:
    """Return the runtime-priors file path for ``(profile, cluster)``.

    Forwarder for ``RepoLayout(experiment_dir).runtime_prior(profile,
    cluster)``. The layout class resolves *experiment_dir* and
    sanitizes ``/`` in *profile*; both behaviors are preserved here.
    """
    from claude_hpc._internal.layout import RepoLayout

    return RepoLayout(experiment_dir).runtime_prior(profile, cluster)


def _empty_doc(profile: str, cluster: str) -> dict[str, Any]:
    return {
        "profile": profile,
        "cluster": cluster,
        "schema_version": SCHEMA_VERSION,
        "samples": [],
    }


def _normalise(doc: dict[str, Any] | None, profile: str, cluster: str) -> dict[str, Any]:
    """Coerce a parsed runtime-prior doc (or ``None``) to a well-shaped
    dict with the required schema fields.
    """
    if not isinstance(doc, dict):
        return _empty_doc(profile, cluster)
    doc.setdefault("schema_version", SCHEMA_VERSION)
    doc.setdefault("profile", profile)
    doc.setdefault("cluster", cluster)
    if not isinstance(doc.get("samples"), list):
        doc["samples"] = []
    return doc


def _resolve_queue_wait_sec(
    explicit: int | None,
    started_at: str | None,
    submitted_at_iso: str | None,
) -> int | None:
    """Return the queue-wait seconds for a sample.

    Precedence:

    1. *explicit* (from the caller) wins when given. Negative values are
       rejected to None — a negative wait is meaningless.
    2. Otherwise compute ``started_at - submitted_at_iso`` if both
       timestamps are parseable ISO strings.
    3. Negative deltas (clock skew between the submitting host and the
       compute node) reject to None.
    4. Anything missing/unparseable yields None.

    Kept as a free function so the queue-wait baseline tests can exercise
    derivation independently of the full ``append_sample`` path.
    """
    if explicit is not None:
        try:
            v = int(explicit)
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None
    if not started_at or not submitted_at_iso:
        return None
    sd = parse_iso_utc_or_none(submitted_at_iso)
    st = parse_iso_utc_or_none(started_at)
    if sd is None or st is None:
        return None
    delta = (st - sd).total_seconds()
    if delta < 0:
        return None
    return int(round(delta))


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
    # B8: cross-domain manifest check. Soft-skip on mismatch — a future
    # writer with a wider schema shouldn't poison the prior; treating
    # the file as empty makes the prior re-learn from fresh samples.
    from claude_hpc._internal._version import is_compatible as _is_compat

    if isinstance(doc, dict):
        sv = doc.get("schema_version")
        if isinstance(sv, int) and not _is_compat("runtime_prior", sv):
            return _empty_doc(profile, cluster)
    return _normalise(doc, profile, cluster)


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
    peak_host_mem_mb: int | None = None,
    cpu_seconds_used: int | None = None,
    cpus_requested: int | None = None,
    predicted_eta_sec: int | None = None,
    submitted_at_iso: str | None = None,
    queue_wait_sec: int | None = None,
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
        # Footprint-shrink fields. Optional because not all schedulers /
        # ingestion paths surface MaxRSS or AveCPU (sacct does on SLURM
        # post-completion). Adversarial mode reads these via
        # `roll_up_quantiles` — empty samples just degrade the
        # recommendation back to the user-supplied default.
        "peak_host_mem_mb": peak_host_mem_mb,
        "cpu_seconds_used": cpu_seconds_used,
        "cpus_requested": cpus_requested,
        # House-edge fields. The planner's `--test-only` prediction is
        # written into a per-run sidecar at submit time
        # (`calibration.record_prediction_sidecar`); the monitor reads
        # it back and includes it here so `compute_house_edge` can
        # measure scheduler calibration without any cross-module state.
        "predicted_eta_sec": predicted_eta_sec,
        "submitted_at_iso": submitted_at_iso,
        # Queue-wait observation feeding the diurnal moving-average
        # forecaster (`queue_wait_baseline.predict_queue_wait`). When the
        # caller doesn't pass it explicitly we derive it from
        # `started_at - submitted_at_iso` if both are parseable; a
        # negative delta (clock skew) records None rather than a
        # nonsense negative.
        "queue_wait_sec": _resolve_queue_wait_sec(queue_wait_sec, started_at, submitted_at_iso),
    }

    def _mutate(raw: dict[str, Any] | None) -> dict[str, Any]:
        doc = _normalise(raw, profile, cluster)
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

    atomic_locked_update(path, _mutate)
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
    mem_by_gpu: dict[str, list[int]] = {}
    cpu_by_gpu: dict[str, list[int]] = {}
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
        # Optional footprint fields — only contribute when present.
        mem_mb = _coerce_pos_int(s.get("peak_host_mem_mb"))
        if mem_mb is not None:
            mem_by_gpu.setdefault(gpu, []).append(mem_mb)
        cpu_used = _cores_used_from_sample(s, elapsed)
        if cpu_used is not None:
            cpu_by_gpu.setdefault(gpu, []).append(cpu_used)

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

    out_mem_quantiles = _quantile_buckets(mem_by_gpu, quantiles)
    out_cpu_quantiles = _quantile_buckets(cpu_by_gpu, quantiles)

    return {
        "profile": profile,
        "cluster": cluster,
        "now_iso": utcnow_iso(),
        "needs_canary": len(out_quantiles) == 0,
        "quantiles": out_quantiles,
        # Footprint-shrink rollups. Only populated when samples carry
        # the optional `peak_host_mem_mb` / `cpu_seconds_used` fields.
        # The adversarial planner reads these to right-size --mem and
        # --cpus-per-task; empty rollup → planner falls back to the
        # user-supplied defaults.
        "mem_quantiles_mb": out_mem_quantiles,
        "cpu_cores_quantiles": out_cpu_quantiles,
        "total_samples": len(samples),
        "filtered_by_cmd_sha": cmd_sha,
    }


def coerce_pos_int(x: Any) -> int | None:
    """Coerce *x* to a positive int or return None. Permissive on garbage.

    Shared with :mod:`claude_hpc.orchestrator.calibration` so both modules
    consume the runtime-prior sample dicts through a single coercion.
    """
    if x is None:
        return None
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# Back-compat alias for in-module callers; remove once they migrate.
_coerce_pos_int = coerce_pos_int


def _cores_used_from_sample(s: dict[str, Any], elapsed_sec: int) -> int | None:
    """Estimate effective core count from ``cpu_seconds_used / elapsed_sec``.

    Rounds up so a workload that averaged 2.3 cores comes back as 3 — we
    must size for the actual peak need, not the time-average. Returns
    None when the sample lacks the input or the math degenerates.
    """
    cpu_sec = _coerce_pos_int(s.get("cpu_seconds_used"))
    if cpu_sec is None or elapsed_sec <= 0:
        return None
    import math as _math

    cores = _math.ceil(cpu_sec / max(1, elapsed_sec))
    return max(1, int(cores))


def _quantile_buckets(
    by_gpu: dict[str, list[int]],
    quantiles: tuple[float, ...],
) -> dict[str, dict[str, int]]:
    """Bucket-and-quantile a per-GPU integer series. DRY helper for mem/cpu."""
    out: dict[str, dict[str, int]] = {}
    for gpu, vals in by_gpu.items():
        if not vals:
            continue
        entry: dict[str, int] = {}
        for q in quantiles:
            label = f"p{int(round(q * 100))}"
            entry[label] = _quantile(vals, q)
        entry["n_samples"] = len(vals)
        entry["min"] = int(min(vals))
        entry["max"] = int(max(vals))
        out[gpu] = entry
    return out
