"""Grid expansion and task manifest generation.

Pure computation — no I/O, only stdlib imports.
"""

from __future__ import annotations

import itertools
import re
from math import prod

__all__ = ["expand_grid", "run_id", "build_task_manifest", "total_tasks"]


def expand_grid(grid: dict[str, list]) -> list[dict[str, str]]:
    """Cartesian product of all grid values, preserving key insertion order."""
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    return [
        {k: str(v) for k, v in zip(keys, combo, strict=True)}
        for combo in itertools.product(*values)
    ]


def run_id(params: dict[str, str]) -> str:
    """Deterministic string ID from param values, joined by ``_``."""
    raw = "_".join(params.values())
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def build_task_manifest(
    run_cmd: str,
    grid: dict[str, list],
    result_dir_template: str,
    chunking: dict | None = None,
) -> dict:
    """Build a task manifest from a grid and optional chunking config.

    Parameters
    ----------
    run_cmd:
        Base command string (e.g. ``"python3 -m my_experiment.train"``).
    grid:
        ``param_name -> list_of_values``.
    result_dir_template:
        String with ``{run_id}`` placeholder.
    chunking:
        Optional dict with ``total`` (int), ``chunk_arg`` (str, default
        ``"--chunk-id"``), ``total_arg`` (str, default ``"--total-chunks"``).
    """
    points = expand_grid(grid)
    chunks_per_point = chunking["total"] if chunking else 1
    chunk_arg = chunking.get("chunk_arg", "--chunk-id") if chunking else None
    total_arg = chunking.get("total_arg", "--total-chunks") if chunking else None
    n_tasks = len(points) * chunks_per_point

    tasks: dict[str, dict] = {}
    for i in range(n_tasks):
        gi = i // chunks_per_point
        ci = i % chunks_per_point
        params = points[gi]

        parts = [run_cmd]
        for k, v in params.items():
            parts.append(f"--{k} {v}")
        if chunking:
            parts.append(f"{chunk_arg} {ci}")
            parts.append(f"{total_arg} {chunks_per_point}")

        entry: dict = {
            "cmd": " ".join(parts),
            "result_dir": result_dir_template.format(run_id=run_id(params)),
            "params": dict(params),
        }
        if chunking:
            entry["chunk_id"] = ci
        tasks[str(i)] = entry

    return {
        "total_tasks": n_tasks,
        "grid_size": len(points),
        "chunks_per_point": chunks_per_point,
        "grid_keys": list(grid.keys()),
        "tasks": tasks,
    }


def total_tasks(grid: dict[str, list], chunks: int = 1) -> int:
    """Product of all grid dimension sizes times *chunks*."""
    return prod(len(v) for v in grid.values()) * chunks
