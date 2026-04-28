# hpc-shim-key: PLACEHOLDER
"""HPC date-window shim — concrete example: calendar-aware period split.

This is a worked example for the date-window adapter pattern: the user's
executor accepts ``--start`` / ``--end`` (date or datetime ISO strings), but
the framework hands out ``--chunk-id`` / ``--total-chunks``. The shim
translates a chunk index into a contiguous date/datetime window drawn from
the [START, END] span, stepping in CHUNK_DUR increments.

For the blank skeleton or the row-index split, use ``shim_template.py`` or
``chunking_shim.py`` instead.

The runtime contract (see ``hpc_mapreduce/map/shim.py`` for the cache side):

* The shim is a standalone ``.py`` file.
* Invoked as ``python <shim> --chunk-id N --total-chunks M -- <downstream cmd>``.
* ``translate()`` returns extra CLI args appended to the downstream command.
* Exit code of the downstream process is propagated verbatim.
"""

from __future__ import annotations

import argparse
import calendar
import subprocess
import sys
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------- #
# Configuration — the LLM fills these in at submit time.
# --------------------------------------------------------------------------- #
START = "2020-01-01"
END = "2024-12-31"
CHUNK_DUR = "6M"  # supported: <int>m / <int>h / <int>H / <int>d / <int>D / <int>M / <int>y / <int>Y
START_ARG = "--start"
END_ARG = "--end"
# --------------------------------------------------------------------------- #

# Date-window periods are deterministic from the constants above, so no
# runtime cache is needed (unlike the row-split shim).
_CACHE_FILE = None


def _add_months(d: date | datetime, months: int) -> date | datetime:
    """Add *months* calendar months to *d*, clamping to the valid day range."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    if isinstance(d, datetime):
        return datetime(
            year, month, day, d.hour, d.minute, d.second, d.microsecond, tzinfo=d.tzinfo
        )
    return date(year, month, day)


def _parse_duration(duration_str: str) -> tuple[int, str]:
    """Parse a duration string like '6M', '30D', '2h', '30m' into (amount, suffix).

    Case-sensitive: 'M' = months, 'm' = minutes, 'h'/'H' = hours,
    'D'/'d' = days, 'Y'/'y' = years.
    """
    raw_suffix = duration_str[-1]
    amount = int(duration_str[:-1])
    if raw_suffix == "m":
        return amount, "MIN"
    if raw_suffix in ("h", "H"):
        return amount, "H"
    if raw_suffix in ("d", "D"):
        return amount, "D"
    if raw_suffix == "M":
        return amount, "M"
    if raw_suffix in ("y", "Y"):
        return amount, "Y"
    raise ValueError(f"Unsupported duration suffix: {raw_suffix!r}")


def _periods() -> list[tuple[str, str]]:
    """Walk [START, END] in CHUNK_DUR steps; return [(start_iso, end_iso), ...].

    Sub-daily suffixes ('m', 'h', 'H') parse with ``datetime.fromisoformat``
    and end each period one second before the next cursor.  Date-level
    suffixes ('d', 'D', 'M', 'y', 'Y') parse with ``date.fromisoformat`` and
    end each period one day before the next cursor.  Both are clamped to the
    overall ``END``.
    """
    amount, suffix = _parse_duration(CHUNK_DUR)
    sub_daily = suffix in ("H", "MIN")

    overall_start: date | datetime
    overall_end: date | datetime
    if sub_daily:
        overall_start = datetime.fromisoformat(START)
        overall_end = datetime.fromisoformat(END)
    else:
        overall_start = date.fromisoformat(START)
        overall_end = date.fromisoformat(END)

    periods: list[tuple[str, str]] = []
    cursor = overall_start

    while cursor <= overall_end:
        if suffix == "MIN":
            next_cursor = cursor + timedelta(minutes=amount)
        elif suffix == "H":
            next_cursor = cursor + timedelta(hours=amount)
        elif suffix == "D":
            next_cursor = cursor + timedelta(days=amount)
        elif suffix == "M":
            next_cursor = _add_months(cursor, amount)
        elif suffix == "Y":
            next_cursor = _add_months(cursor, amount * 12)
        else:
            raise ValueError(f"Unsupported duration suffix: {suffix!r}")

        if sub_daily:
            period_end = min(next_cursor - timedelta(seconds=1), overall_end)
        else:
            period_end = min(next_cursor - timedelta(days=1), overall_end)

        periods.append((cursor.isoformat(), period_end.isoformat()))
        cursor = next_cursor

    return periods


def translate(chunk_id: int, total_chunks: int) -> list[str]:
    """Return CLI args to append to the downstream executor command.

    Computes the full period list from the module constants, asserts the
    caller's ``total_chunks`` matches, and returns the
    ``[START_ARG, start_iso, END_ARG, end_iso]`` pair for ``chunk_id``.
    """
    periods = _periods()
    assert total_chunks == len(periods), (
        f"total_chunks={total_chunks} but shim has {len(periods)} periods"
    )
    start_iso, end_iso = periods[chunk_id]
    return [START_ARG, start_iso, END_ARG, end_iso]


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
