"""Closed-loop calibration for the adversarial scheduler attack.

Two responsibilities:

1. **Walltime drift detection** (:func:`compute_walltime_drift`,
   :func:`recommend_safety_mult_adjustment`) — measures how often
   recent jobs are landing in the cliff zone (``elapsed / requested >=
   0.95``) and recommends a per-cluster ``safety_mult`` adjustment. If
   we're seeing too many cliff-kills, the planner's walltime ask is
   too tight and the multiplier should loosen; if we're systematically
   leaving headroom, the multiplier can tighten.

2. **House-edge tracking** (:func:`compute_house_edge`,
   :func:`record_prediction_sidecar`,
   :func:`read_prediction_sidecar`) — compares the planner's
   ``predicted_eta_sec`` (from ``sbatch --test-only``) against the
   actual ``Submit→Start`` delta observed at job completion. Validates
   that the lattice probe is finding real backfill windows rather than
   phantom ones, and surfaces when SLURM's predictions are
   systematically optimistic or pessimistic.

These functions read the same per-(profile, cluster) sample file that
``runtime_prior`` writes; they don't introduce a separate store. Drift
operates on existing fields (``elapsed_sec``,
``walltime_requested_sec``, ``exit_code``); house-edge needs the new
optional fields ``predicted_eta_sec``, ``submitted_at_iso``,
``started_at`` plumbed through ``append_sample``.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from claude_hpc._internal._time import parse_iso_utc, utcnow_iso
from claude_hpc.orchestrator.runtime_prior import coerce_pos_int as _coerce_pos_int

__all__ = [
    "WalltimeDrift",
    "HouseEdge",
    "compute_walltime_drift",
    "recommend_safety_mult_adjustment",
    "compute_house_edge",
    "compute_house_edge_by_gpu_type",
    "record_prediction_sidecar",
    "read_prediction_sidecar",
    "prediction_sidecar_path",
]

# ─── walltime drift ────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class WalltimeDrift:
    """Aggregate cliff-zone statistics for a (profile, cluster) sample set.

    A *cliff event* is a sample where the job hit walltime: ``exit_code
    != 0`` and ``elapsed_sec / walltime_requested_sec >= 0.95``. A
    *near-miss* is the same ratio with ``exit_code == 0`` — the job
    barely finished. We weight near-misses at 0.5 of a cliff event
    because they're weaker evidence (the job did succeed) but still
    indicate the walltime ask was perilously tight.
    """

    n_recent: int  # samples considered (filtered by recency + has-walltime)
    n_cliff_events: int  # full cliff-kills (TIMEOUT or similar)
    n_near_misses: int  # successful but elapsed/requested >= 0.90
    weighted_cliff_rate: float  # (cliff + 0.5*near_miss) / n_recent
    median_utilization: float | None  # median elapsed/requested over n_recent


def compute_walltime_drift(
    samples: list[dict[str, Any]],
    *,
    cliff_ratio: float = 0.95,
    near_miss_ratio: float = 0.90,
    max_samples: int = 100,
) -> WalltimeDrift:
    """Aggregate the cliff-event signal across recent samples.

    *samples* should already be filtered by ``(profile, cluster)`` and
    ideally by ``cmd_sha``; this function only enforces the
    has-walltime-and-elapsed filter. The newest *max_samples* are used
    so a long-running campaign doesn't have its drift signal washed out
    by ancient history.
    """
    eligible: list[tuple[float, int]] = []  # (utilization, exit_code)
    for s in samples:
        try:
            elapsed = int(s.get("elapsed_sec") or 0)
            requested = int(s.get("walltime_requested_sec") or 0)
            exit_code = int(s.get("exit_code") or 0)
        except (TypeError, ValueError):
            continue
        if elapsed <= 0 or requested <= 0:
            continue
        eligible.append((elapsed / requested, exit_code))

    # Newest-first ordering: assume samples list is already in append
    # order (oldest first) and slice off the tail.
    if len(eligible) > max_samples:
        eligible = eligible[-max_samples:]

    n_recent = len(eligible)
    if n_recent == 0:
        return WalltimeDrift(
            n_recent=0,
            n_cliff_events=0,
            n_near_misses=0,
            weighted_cliff_rate=0.0,
            median_utilization=None,
        )

    cliff = sum(1 for u, ec in eligible if u >= cliff_ratio and ec != 0)
    near = sum(
        1 for u, ec in eligible if u >= near_miss_ratio and not (u >= cliff_ratio and ec != 0)
    )
    weighted = (cliff + 0.5 * near) / n_recent
    utils = sorted(u for u, _ in eligible)
    median = utils[len(utils) // 2]
    return WalltimeDrift(
        n_recent=n_recent,
        n_cliff_events=cliff,
        n_near_misses=near,
        weighted_cliff_rate=weighted,
        median_utilization=round(median, 3),
    )


def recommend_safety_mult_adjustment(
    drift: WalltimeDrift,
    *,
    base_safety_mult: float = 1.30,
    cliff_threshold: float = 0.05,
    loosen_per_5pct_over: float = 0.10,
    floor_safety_mult: float = 1.05,
    ceiling_safety_mult: float = 2.00,
    min_samples_for_adjustment: int = 10,
) -> tuple[float, str]:
    """Recommend a per-cluster ``safety_mult`` based on drift.

    Algorithm:

    - Below *min_samples_for_adjustment* recent samples: don't trust the
      signal yet; return *base_safety_mult* unchanged.
    - At ``weighted_cliff_rate <= cliff_threshold`` (5% by default):
      already in the safe zone, no adjustment.
    - Above the threshold: loosen by *loosen_per_5pct_over* for each
      5% over the threshold. So a 15% cliff rate adds 0.20 to the
      multiplier (10% over × 2 increments × 0.10).
    - Below the threshold by a large margin AND the median utilization
      is <0.5: tighten slightly (we're systematically over-asking).

    Returns ``(adjusted_mult, rationale)``. Rationale is human-readable
    so the planner report can surface *why* the multiplier shifted.
    """
    if drift.n_recent < min_samples_for_adjustment:
        return base_safety_mult, (
            f"insufficient drift signal (n={drift.n_recent} < "
            f"{min_samples_for_adjustment}); using base {base_safety_mult:.2f}"
        )

    rate = drift.weighted_cliff_rate
    if rate > cliff_threshold:
        increments = (rate - cliff_threshold) / 0.05
        adjusted = base_safety_mult + increments * loosen_per_5pct_over
        adjusted = min(adjusted, ceiling_safety_mult)
        return round(adjusted, 3), (
            f"cliff rate {rate:.2%} > {cliff_threshold:.0%}; "
            f"loosened {base_safety_mult:.2f}→{adjusted:.2f} "
            f"({drift.n_cliff_events} cliff + {drift.n_near_misses} near-miss "
            f"in last {drift.n_recent})"
        )
    # Tighten if we're systematically over-asking. Conservative: only
    # tighten when the median is well below the cliff.
    if drift.median_utilization is not None and drift.median_utilization < 0.5 and rate == 0.0:
        adjusted = max(base_safety_mult - 0.10, floor_safety_mult)
        if adjusted < base_safety_mult:
            return round(adjusted, 3), (
                f"median utilization {drift.median_utilization:.0%} with zero "
                f"cliff events in last {drift.n_recent}; tightened "
                f"{base_safety_mult:.2f}→{adjusted:.2f}"
            )
    return base_safety_mult, (
        f"cliff rate {rate:.2%} ≤ {cliff_threshold:.0%}; using base {base_safety_mult:.2f}"
    )


# ─── house edge: predicted vs. actual queue time ──────────────────────────


@dataclasses.dataclass(frozen=True)
class HouseEdge:
    """Aggregate calibration of `--test-only` predictions vs. observed queue.

    ``delta_sec = actual_queue_sec - predicted_eta_sec``.

    - Positive mean ⇒ scheduler was optimistic; we waited longer than predicted.
    - Negative mean ⇒ scheduler was pessimistic; we got in faster.
    - ``calibration_ratio`` ≈ 1.0 means probes are well-calibrated.
    """

    n_with_prediction: int
    mean_delta_sec: float | None
    median_delta_sec: float | None
    p95_delta_sec: float | None
    calibration_ratio: float | None  # mean(actual / predicted)


def compute_house_edge(samples: list[dict[str, Any]]) -> HouseEdge:
    """Aggregate predicted-vs-actual queue-time deltas across samples.

    Samples must carry ``predicted_eta_sec`` (from the planner) and
    both ``submitted_at_iso`` and ``started_at`` (or ``started_at_iso``
    — we accept either for forward compatibility) so we can compute the
    actual queue wait. Ignores samples missing any of the three.
    """
    deltas: list[float] = []
    ratios: list[float] = []
    for s in samples:
        predicted = _coerce_pos_int(s.get("predicted_eta_sec"))
        actual = _actual_queue_sec(s)
        if predicted is None or actual is None:
            continue
        deltas.append(actual - predicted)
        if predicted > 0:
            ratios.append(actual / predicted)

    n = len(deltas)
    if n == 0:
        return HouseEdge(
            n_with_prediction=0,
            mean_delta_sec=None,
            median_delta_sec=None,
            p95_delta_sec=None,
            calibration_ratio=None,
        )
    sorted_deltas = sorted(deltas)
    mean = sum(deltas) / n
    median = sorted_deltas[n // 2]
    p95_idx = max(0, min(n - 1, int(round(0.95 * (n - 1)))))
    p95 = sorted_deltas[p95_idx]
    cal = (sum(ratios) / len(ratios)) if ratios else None
    return HouseEdge(
        n_with_prediction=n,
        mean_delta_sec=round(mean, 1),
        median_delta_sec=round(median, 1),
        p95_delta_sec=round(p95, 1),
        calibration_ratio=round(cal, 3) if cal is not None else None,
    )


def compute_house_edge_by_gpu_type(
    samples: list[dict[str, Any]],
    *,
    min_samples: int = 5,
) -> dict[str, HouseEdge]:
    """Bucket *samples* by ``gpu_type`` and compute :class:`HouseEdge` per bucket.

    The single-bucket :func:`compute_house_edge` pools across every
    constraint class, which papers over exactly the asymmetry the
    forecaster needs to respect: ``--test-only`` is systematically
    pessimistic on "inferior" resources (fewer GPUs, weaker types) that
    quietly slot into backfill, and optimistic on the contended
    flagship pools. Per-GPU-type buckets surface that asymmetry.

    Buckets with fewer than *min_samples* paired (predicted, actual)
    observations are dropped — a 2-sample bucket would let one freak
    wait dominate the ratio. Callers consuming the return value should
    treat a missing key as "no calibration; trust raw ETA".
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        gpu_type = s.get("gpu_type")
        if not isinstance(gpu_type, str) or not gpu_type:
            continue
        by_type.setdefault(gpu_type, []).append(s)

    out: dict[str, HouseEdge] = {}
    for gpu_type, group in by_type.items():
        edge = compute_house_edge(group)
        if edge.n_with_prediction >= min_samples and edge.calibration_ratio is not None:
            out[gpu_type] = edge
    return out


def _actual_queue_sec(sample: dict[str, Any]) -> float | None:
    """Extract Submit→Start delta in seconds. Permissive on field names."""
    submitted = sample.get("submitted_at_iso") or sample.get("submitted_at")
    started = sample.get("started_at_iso") or sample.get("started_at")
    if not submitted or not started:
        return None
    try:
        s_dt = _parse_iso(submitted)
        st_dt = _parse_iso(started)
    except ValueError:
        return None
    delta = (st_dt - s_dt).total_seconds()
    return delta if delta >= 0 else None


_parse_iso = parse_iso_utc


# ─── prediction sidecar I/O ────────────────────────────────────────────────
#
# When the planner picks a `recommended_tuple`, we write a tiny JSON
# sidecar at `.hpc/runs/<run_id>.predicted_eta.json` so the monitor (or
# any consumer that ingests samples post-completion) can plumb the
# prediction back into `append_sample`. This keeps the planner and the
# monitor decoupled — neither depends on a shared in-memory state.


def prediction_sidecar_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the prediction-sidecar path for a run."""
    if not run_id:
        raise ValueError("run_id must be non-empty")
    return Path(experiment_dir).resolve() / ".hpc" / "runs" / f"{run_id}.predicted_eta.json"


def record_prediction_sidecar(
    experiment_dir: Path,
    run_id: str,
    *,
    predicted_eta_sec: int,
    constraint: str,
    walltime_sec: int,
    mem_mb: int,
    cpus: int,
    submitted_at_iso: str | None = None,
) -> Path:
    """Write the planner's prediction sidecar atomically.

    The schema is intentionally narrow — just what the monitor needs to
    reconstruct the ``predicted_eta_sec`` argument to ``append_sample``.
    Idempotent: a re-call with the same ``run_id`` overwrites cleanly.
    """
    path = prediction_sidecar_path(experiment_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "predicted_eta_sec": int(predicted_eta_sec),
        "constraint": constraint,
        "walltime_sec": int(walltime_sec),
        "mem_mb": int(mem_mb),
        "cpus": int(cpus),
        "submitted_at_iso": submitted_at_iso or utcnow_iso(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)
    return path


def read_prediction_sidecar(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Read the prediction sidecar, or ``None`` if missing/corrupt."""
    path = prediction_sidecar_path(experiment_dir, run_id)
    try:
        text = path.read_text()
    except (FileNotFoundError, OSError):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    return doc
