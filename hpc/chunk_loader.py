"""Aggregate per-chunk metric sidecars.

Standalone module — stdlib only, no external dependencies.
"""

from __future__ import annotations

__all__ = [
    "aggregate_metrics",
]

import json
from pathlib import Path


def aggregate_metrics(result_dir: str | Path, total_chunks: int) -> dict:
    """Aggregate per-chunk metrics JSON sidecars into a single summary.

    Computes a weighted mean of each metric key across chunks, weighted by
    ``n_samples`` when present.  The ``n_samples`` key itself is summed.
    Missing or corrupt sidecar files are silently skipped.

    Parameters
    ----------
    result_dir : str or Path
        Directory containing ``metrics_chunk_{id}.json`` files.
    total_chunks : int
        Expected number of chunks (1-indexed: 1 .. total_chunks).

    Returns
    -------
    dict
        Flat dict of aggregated metrics.  Empty dict if no sidecars found.
    """
    rdir = Path(result_dir)
    entries: list[dict] = []

    for cid in range(1, total_chunks + 1):
        path = rdir / f"metrics_chunk_{cid}.json"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                entries.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue

    if not entries:
        return {}

    all_keys = {k for e in entries for k in e}
    weights = [e.get("n_samples", 1) for e in entries]
    result: dict = {}

    for key in sorted(all_keys):
        if key == "n_samples":
            result["n_samples"] = sum(e.get("n_samples", 0) for e in entries)
            continue
        pairs = [(e[key], w) for e, w in zip(entries, weights, strict=True) if key in e]
        if not pairs:
            continue
        w_total = sum(w for _, w in pairs)
        result[key] = sum(v * w for v, w in pairs) / w_total if w_total else 0.0

    return result
