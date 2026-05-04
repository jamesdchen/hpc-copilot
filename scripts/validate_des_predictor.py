#!/usr/bin/env python3
"""Replay historical submits to validate DES-predictor accuracy.

For each entry in ``runtime_prior`` with a ``queue_wait_sec`` field, find
the cluster snapshot just before its ``submitted_at_iso``, run the DES
forward, and compare the predicted wait to the observed wait. Outputs a
JSON summary: number of samples, MAE, MAPE, p50/p95 of the residual.

Used as a calibration target — the residual is the signal that tells
us when to layer MULTIFACTOR priority, refine the actual-over-ask
distribution, etc. Phase 4 leaves the calibration loop deferred (see
``docs/queue-wait-predictor.md``); this script is the data source.

Usage::

    python scripts/validate_des_predictor.py --experiment-dir <path>
        --profile <name> --cluster <cluster>
        [--n-replications 16] [--limit 100] [--output -]

When the runtime-prior pool is empty (no observations yet) or no
matching cluster_history snapshots exist, the script prints an empty
summary and exits 0 — explicitly graceful so it's safe to wire into
nightly CI before users have submitted anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow ``python scripts/validate_des_predictor.py`` from a checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpc_mapreduce._time import parse_iso_utc_or_none
from hpc_mapreduce.infra.inspect import read_cluster_history
from hpc_mapreduce.job.queue_simulator import SimJob, simulate_distribution
from hpc_mapreduce.job.queue_simulator_inputs import (
    sample_arrival_stream,
    sample_residual_lifetimes,
)
from hpc_mapreduce.job.runtime_prior import read_samples


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] + frac * (xs[hi] - xs[lo])


def _find_snapshot_before(
    experiment_dir: Path, cluster: str, target_iso: str
):
    """Return the most recent snapshot whose now_iso ≤ target_iso, or None."""
    target_dt = parse_iso_utc_or_none(target_iso)
    if target_dt is None:
        return None
    best = None
    best_dt = None
    for snap in read_cluster_history(experiment_dir, cluster, limit=1000):
        snap_dt = parse_iso_utc_or_none(snap.now_iso)
        if snap_dt is None or snap_dt > target_dt:
            continue
        if best_dt is None or snap_dt > best_dt:
            best_dt = snap_dt
            best = snap
    return best


def replay(
    experiment_dir: Path,
    *,
    profile: str,
    cluster: str,
    n_replications: int = 16,
    limit: int | None = None,
    seed: int = 0,
) -> dict[str, Any]:
    """Run the replay sweep. Returns a summary dict."""
    samples = read_samples(
        experiment_dir, profile=profile, cluster=cluster,
        only_successful=False,
    )
    populated = [
        s for s in samples
        if s.get("submitted_at_iso") and s.get("queue_wait_sec") is not None
    ]
    if limit is not None:
        populated = populated[-limit:]

    rows: list[dict[str, Any]] = []
    skipped_no_snapshot = 0
    for s in populated:
        sub_iso = s["submitted_at_iso"]
        observed = float(s["queue_wait_sec"])
        snap = _find_snapshot_before(experiment_dir, cluster, sub_iso)
        if snap is None:
            skipped_no_snapshot += 1
            continue
        # Build a generic candidate; the runtime-prior sample doesn't
        # carry the original submit shape, so we use a small default.
        cand = SimJob(
            job_id=f"replay-{s.get('run_id', 'r')}",
            user="replay",
            submit_time=0.0,
            walltime_ask=float(s.get("elapsed_sec") or 3600),
            cpus=1,
            mem_mb=4_000,
        )
        out = simulate_distribution(
            snap,
            candidate=cand,
            n_replications=n_replications,
            seed=seed,
            arrival_sampler=lambda s: sample_arrival_stream(
                {}, snap_hour_of_week=0, horizon_sec=7 * 86400.0, seed=s,
            ),
            residual_sampler=lambda s: sample_residual_lifetimes(
                snap, {}, seed=s,
            ),
        )
        predicted = out.p50_wait_sec
        residual = predicted - observed
        rows.append({
            "submitted_at_iso": sub_iso,
            "observed_sec": observed,
            "predicted_p50_sec": predicted,
            "predicted_p10_sec": out.p10_wait_sec,
            "predicted_p90_sec": out.p90_wait_sec,
            "residual_sec": residual,
            "rel_residual": residual / observed if observed > 0 else None,
        })

    if not rows:
        return {
            "n_samples": 0,
            "n_skipped_no_snapshot": skipped_no_snapshot,
            "message": (
                "no replay rows produced — check that runtime_prior has "
                "queue_wait_sec entries and that cluster_history snapshots "
                "predate at least one of them"
            ),
        }

    abs_resids = [abs(r["residual_sec"]) for r in rows]
    rel_resids = [
        r["rel_residual"] for r in rows if r["rel_residual"] is not None
    ]
    return {
        "n_samples": len(rows),
        "n_skipped_no_snapshot": skipped_no_snapshot,
        "mae_sec": sum(abs_resids) / len(abs_resids),
        "mape": (
            sum(abs(rr) for rr in rel_resids) / len(rel_resids)
            if rel_resids else None
        ),
        "residual_p50_sec": _percentile(
            [r["residual_sec"] for r in rows], 0.5
        ),
        "residual_p95_sec": _percentile(
            [r["residual_sec"] for r in rows], 0.95
        ),
        "rel_residual_p50": (
            _percentile(rel_resids, 0.5) if rel_resids else None
        ),
        "rel_residual_p95": (
            _percentile(rel_resids, 0.95) if rel_resids else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--experiment-dir", required=True, type=Path)
    p.add_argument("--profile", required=True)
    p.add_argument("--cluster", required=True)
    p.add_argument("--n-replications", type=int, default=16)
    p.add_argument("--limit", type=int, default=None,
                   help="cap to the most recent N samples (for speed)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--output", default="-",
                   help="file path or '-' for stdout (default)")
    args = p.parse_args(argv)
    summary = replay(
        args.experiment_dir,
        profile=args.profile,
        cluster=args.cluster,
        n_replications=args.n_replications,
        limit=args.limit,
        seed=args.seed,
    )
    text = json.dumps(summary, indent=2, sort_keys=True)
    if args.output == "-":
        sys.stdout.write(text + "\n")
    else:
        Path(args.output).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
