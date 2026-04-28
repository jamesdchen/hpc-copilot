"""Minimal HPC shim scaffold.

Copied into an experiment repo by ``/build-executor`` in mode (c) when the
user has an existing script that does not accept ``--chunk-id`` /
``--total-chunks`` directly. Every ``# TODO:`` marker is a point the LLM (or
the user) fills in when wiring the translation to a specific script.

For the common "split a flat array by row index" case, start from
``chunking_shim.py`` instead — it has the range-split logic already
implemented and only needs ``_compute_total_items`` filled in.

Runtime contract (see ``hpc_mapreduce/map/shim.py`` for the cache side):

* Invoked as:  ``python <shim> --chunk-id N --total-chunks M -- <downstream cmd>``
* ``translate(chunk_id, total_chunks)`` returns extra CLI args appended to
  the downstream command.
* The downstream process's return code is propagated verbatim.
* The shim is a normal ``.py`` file; no hpc_mapreduce imports required.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def translate(chunk_id: int, total_chunks: int) -> list[str]:
    """Return CLI args to append to the downstream executor command.

    TODO: implement the chunk_id -> downstream-CLI mapping for your script.

    **Prefer importing helpers from your experiment repo over inlining.**
    A shim is a thin adapter; the heavy logic — loading data, listing files,
    computing date windows — almost always already exists in ``lib/`` /
    ``utils/`` or in one of your executors. Import it; do not re-implement it.

    Reuse-first shapes (pick the one that matches your script):

        # Range split using your repo's own loader:
        from lib.loading import load_raw_data           # ← reuse, don't inline
        total = len(load_raw_data("data/all30min"))
        base, rem = divmod(total, total_chunks)
        start = base * chunk_id + min(chunk_id, rem)
        end   = start + base + (1 if chunk_id < rem else 0)
        return ["--start-row", str(start), "--end-row", str(end)]

        # File-list split using your repo's own globber:
        from lib.io import list_inputs                  # ← reuse, don't inline
        files = list_inputs("data/")
        mine  = files[chunk_id::total_chunks]
        return ["--files", ",".join(map(str, mine))]

        # Date-window split using your repo's own period helper:
        from utils.dates import month_periods           # ← reuse, don't inline
        start_iso, end_iso = month_periods("2020-01-01", "2024-12-31")[chunk_id]
        return ["--start", start_iso, "--end", end_iso]

    Inline-only fallbacks (only when no helper exists in your repo):

        # Naive file-list split:
        from pathlib import Path
        files = sorted(Path("data/").glob("*.parquet"))
        mine  = files[chunk_id::total_chunks]
        return ["--files", ",".join(map(str, mine))]

        # Pass-through window id:
        return ["--window-id", str(chunk_id)]

    Keep the return value a ``list[str]`` — these args are appended after
    the downstream command that comes from ``_hpc_dispatch.py``.
    """
    _ = chunk_id, total_chunks
    raise NotImplementedError("Fill in translate() for your script's CLI")


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
