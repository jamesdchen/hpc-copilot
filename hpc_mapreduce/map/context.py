"""Map-phase execution context for HPC-parallel MapReduce jobs.

Provides ``MapContext`` — a no-op locally (processes everything),
active on HPC (processes the assigned subset).  Experiment authors call
``map_context()`` and ``ctx.split()`` without thinking about
parallelisation; claude-hpc templates inject the env vars that drive it.

Typical usage in an executor::

    from hpc_mapreduce.map.context import map_context

    ctx = map_context()                         # no-op locally (0 of 1)
    my_range = ctx.split(range(train_win, N))   # full range locally
    results.to_csv(ctx.output_path())           # ./results_chunk_1.csv
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

__all__ = ["MapContext", "map_context", "collect_outputs"]


@dataclass(frozen=True)
class MapContext:
    """Immutable description of which map task this process owns."""

    chunk_id: int  # 0-indexed
    total_chunks: int
    result_dir: Path

    def split(self, items: range | int) -> range:
        """Return this task's contiguous slice of *items*.

        Parameters
        ----------
        items : range or int
            A ``range`` of work indices, or an ``int`` shorthand for
            ``range(items)``.

        Returns
        -------
        range
            The sub-range assigned to this task.  When running locally
            (chunk 0 of 1) the full range is returned unchanged.
        """
        if isinstance(items, int):
            items = range(items)
        n = len(items)
        per = n // self.total_chunks
        rem = n % self.total_chunks
        # Distribute remainder: first `rem` chunks get one extra item.
        start = per * self.chunk_id + min(self.chunk_id, rem)
        end = start + per + (1 if self.chunk_id < rem else 0)
        return range(items.start + start, items.start + end)

    def output_path(self, prefix: str = "results_chunk") -> Path:
        """Standard map output path: ``{result_dir}/{prefix}_{chunk_id+1}.csv``."""
        return self.result_dir / f"{prefix}_{self.chunk_id + 1}.csv"

    def counter_path(self) -> Path:
        """Path to this task's counter JSON file."""
        return self.result_dir / f"_counters_{self.chunk_id + 1}.json"

    def emit_counters(self, **counters: int | float | str) -> None:
        """Write map-side counters for this task.

        Implements the MapReduce map-side counter pattern: each task
        reports progress and diagnostics by writing key/value pairs to a
        JSON sidecar file.  The reducer (or monitoring tooling) can read
        these files to track execution progress without polling the main
        result CSV.

        Writes are atomic — the data is flushed to a temporary file
        first, then moved into place with :func:`os.replace`.
        """
        import json as _json

        path = self.counter_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(counters))
        os.replace(tmp, path)

    def read_counters(self) -> dict:
        """Read map-side counters previously written by :meth:`emit_counters`.

        Returns an empty dict if the counter file does not exist or
        contains corrupt JSON.
        """
        import json as _json

        path = self.counter_path()
        try:
            return _json.loads(path.read_text())
        except (OSError, ValueError):
            return {}


def map_context() -> MapContext:
    """Build a :class:`MapContext` from environment variables.

    claude-hpc job templates export ``CHUNK_ID``, ``TOTAL_CHUNKS``, and
    ``RESULT_DIR``.  When none are set (local development) the context
    defaults to chunk 0 of 1, which means "process everything".
    """
    return MapContext(
        chunk_id=int(os.environ.get("CHUNK_ID", "0")),
        total_chunks=int(os.environ.get("TOTAL_CHUNKS", "1")),
        result_dir=Path(os.environ.get("RESULT_DIR", ".")),
    )


def collect_outputs(
    result_dir: str | Path,
    pattern: str = "results_chunk_*.csv",
    date_column: str = "date",
) -> pd.DataFrame:
    """Stitch map output CSVs into a single sorted DataFrame.

    Companion to :func:`map_context` — handles the fan-in after parallel
    execution.  Returns an empty ``DataFrame`` if no matching files are found.

    Parameters
    ----------
    result_dir : str or Path
        Directory containing map output CSV files.
    pattern : str
        Glob pattern for output files (default matches ``ctx.output_path()``).
    date_column : str
        Column to parse as datetime and use as sorted index.  Ignored if the
        column does not exist in the data.
    """
    import pandas as pd

    files = sorted(Path(result_dir).glob(pattern))
    if not files:
        return pd.DataFrame()

    dfs: list[pd.DataFrame] = []
    for f in files:
        try:
            dfs.append(pd.read_csv(f))
        except (OSError, pd.errors.ParserError, UnicodeDecodeError):
            continue

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    if date_column in combined.columns:
        combined[date_column] = pd.to_datetime(combined[date_column])
        combined = combined.set_index(date_column).sort_index()
    return combined
