"""Load and stitch per-chunk result CSVs.

Standalone module — only os, glob, re, pandas.
"""

__all__ = [
    "load_and_stitch_chunks",
    "save_summary",
]

import glob
import os
import re

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
