"""Load and stitch per-chunk result CSVs.

Standalone module — stdlib + pandas only.
"""

from __future__ import annotations

__all__ = [
    "aggregate_metrics",
    "load_and_stitch_chunks",
    "save_summary",
]

import glob
import json
import os
import re
from pathlib import Path

import pandas as pd


def natural_sort_key(s: str) -> list:
    """Sort strings with embedded numbers logically.

    'chunk_2' < 'chunk_10' (numeric ordering, not lexicographic).
    """
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", s)]


def load_and_stitch_chunks(results_dir: str) -> pd.DataFrame:
    """Find, sort, and concatenate all results_chunk_*.csv files.

    Parameters
    ----------
    results_dir : str
        Directory containing chunk CSV files.

    Returns
    -------
    pd.DataFrame
        Concatenated results sorted by date.

    Raises
    ------
    FileNotFoundError
        If no chunk files are found.
    """
    pattern = os.path.join(results_dir, "results_chunk_*.csv")
    files = glob.glob(pattern)

    if not files:
        raise FileNotFoundError(f"No results_chunk_*.csv files found in {results_dir}")

    files.sort(key=lambda f: natural_sort_key(os.path.basename(f)))

    dfs = [pd.read_csv(f) for f in files]
    combined = pd.concat(dfs, ignore_index=True)

    if "date" in combined.columns:
        combined = combined.sort_values("date").reset_index(drop=True)

    return combined


def save_summary(df: pd.DataFrame, metrics: dict, output_path: str) -> None:
    """Save a summary CSV with metrics alongside the data shape.

    Parameters
    ----------
    df : pd.DataFrame
        The stitched results DataFrame.
    metrics : dict
        Metric name -> value mapping.
    output_path : str
        Path for the output CSV.
    """
    summary = pd.DataFrame([metrics])
    summary.insert(0, "n_rows", len(df))
    summary.to_csv(output_path, index=False)


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
