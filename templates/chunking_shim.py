"""HPC chunking shim — concrete example: range split on row count.

This is a worked example for the most common adapter pattern: the user's
executor accepts ``--start`` / ``--end`` (integer row indices), but the
framework hands out ``--chunk-id`` / ``--total-chunks``. The shim translates.

For a blank skeleton — e.g. a date-window split or a file-list split —
start from ``shim_template.py`` instead and copy only the pieces you need.

The runtime contract (see ``hpc_mapreduce/map/shim.py`` for the cache side):

* The shim is a standalone ``.py`` file.
* Invoked as ``python <shim> --chunk-id N --total-chunks M -- <downstream cmd>``.
* ``translate()`` returns extra CLI args appended to the downstream command.
* Exit code of the downstream process is propagated verbatim.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_CACHE_FILE = "_shim_cache.json"


def translate(chunk_id: int, total_chunks: int) -> list[str]:
    """Return CLI args to append to the downstream executor command.

    This is the experiment-specific part. Adapt to your data pipeline.
    Examples:
      - Range split: determine total_rows, compute --start/--end   (shown below)
      - File split:  list files, select subset for this chunk      (TODO)
      - Date split:  compute date window from chunk index          (TODO)
    """
    # TODO: edit below if your executor uses flags other than --start/--end.
    total_items = _cached_total_items()
    base = total_items // total_chunks
    remainder = total_items % total_chunks
    start = base * chunk_id + min(chunk_id, remainder)
    end = start + base + (1 if chunk_id < remainder else 0)
    return ["--start", str(start), "--end", str(end)]


def _cached_total_items() -> int:
    """Compute and cache the total item count for splitting."""
    if _CACHE_FILE and os.path.isfile(_CACHE_FILE):
        with open(_CACHE_FILE) as f:
            cache = json.load(f)
        if "total_items" in cache:
            return int(cache["total_items"])

    total = _compute_total_items()

    if _CACHE_FILE:
        cache = {}
        if os.path.isfile(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                cache = json.load(f)
        cache["total_items"] = total
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, _CACHE_FILE)
    return total


def _compute_total_items() -> int:
    """Determine the total number of items to split across chunks.

    TODO: replicate your executor's data pipeline up to the point where the
    array length is known, then return that length. Typical implementations:

        from lib.loading import load_raw_data
        return len(load_raw_data("data/all30min"))

    Raising ``NotImplementedError`` here is intentional — a fresh shim is
    not useful until this function is filled in.
    """
    raise NotImplementedError("Fill in your data pipeline here")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate chunk_id/total_chunks for HPC dispatch",
    )
    parser.add_argument("--chunk-id", type=int, required=True)
    parser.add_argument("--total-chunks", type=int, required=True)
    args, downstream = parser.parse_known_args()

    if downstream and downstream[0] == "--":
        downstream = downstream[1:]
    if not downstream:
        parser.error("no downstream command provided after --")

    extra_args = translate(args.chunk_id, args.total_chunks)

    cmd = downstream + extra_args
    print(f"[shim] chunk {args.chunk_id}/{args.total_chunks} -> {' '.join(extra_args)}")
    sys.exit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
