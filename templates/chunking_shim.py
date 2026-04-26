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
import tempfile

_CACHE_FILE = "_shim_cache.json"


def translate(chunk_id: int, total_chunks: int) -> list[str]:
    """Return CLI args to append to the downstream executor command.

    This is the experiment-specific part. The arithmetic here is generic
    range-splitting and stays as-is; the *data-aware* piece —
    ``_cached_total_items()`` → ``_compute_total_items()`` below — must
    reuse a loader from your experiment repo's ``lib/`` rather than inline
    a parallel data-loading path.

    Adapt the flag names if your executor doesn't use ``--start`` / ``--end``.
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
        # Use a per-process tempfile so concurrent array tasks on a shared
        # filesystem don't race on the same `_shim_cache.json.tmp` name.
        cache_dir = os.path.dirname(_CACHE_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix="_shim_cache.", suffix=".tmp", dir=cache_dir)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, _CACHE_FILE)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return total


def _compute_total_items() -> int:
    """Determine the total number of items to split across chunks.

    **Import the loader from your experiment repo. Do not re-implement it.**
    Whatever function your executors already call to materialise the dataset
    — typically something in ``lib/loading.py``, ``lib/data.py``, or
    ``utils/io.py`` — is the one this shim must call too. Re-implementing it
    here is the most common shim bug: it drifts silently from the executor's
    real data pipeline and the chunk boundaries no longer match.

    The expected pattern is one or two lines:

        from lib.loading import load_raw_data
        return len(load_raw_data("data/all30min"))

    Inspect ``executors/`` (or ``src/``, ``scripts/``) to find the exact
    loader your executors use, then call it the same way here. Inline a
    custom counter only if no such loader exists in the experiment repo —
    and even then, prefer a tiny helper added to ``lib/`` over inline code
    in this shim, so future executors can share it.

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
