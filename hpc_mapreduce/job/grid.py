"""Grid expansion and task manifest generation.

Pure computation — no I/O, only stdlib imports.
"""

from __future__ import annotations

import calendar
import itertools
import re
from datetime import date, datetime, timedelta
from math import prod

__all__ = [
    "expand_grid",
    "run_id",
    "build_task_manifest",
    "total_tasks",
    "expand_backtest",
]


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


def _add_months(d: date | datetime, months: int) -> date | datetime:
    """Add *months* calendar months to *d*, clamping to valid day."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    if isinstance(d, datetime):
        return datetime(year, month, day, d.hour, d.minute, d.second)
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


def expand_backtest(backtest: dict) -> list[dict[str, str]]:
    """Convert a backtest config to a list of period dicts.

    Parameters
    ----------
    backtest:
        Dict with: start (str), end (str),
        chunk_duration (str like "6M", "1Y", "30D", "2h", "30m"),
        start_arg (str, default "--start"),
        end_arg (str, default "--end").

        Dates can be YYYY-MM-DD (date-only) or ISO datetime strings.
        Sub-daily durations (h, m) produce datetime boundaries;
        day-or-larger durations (D, M, Y) produce date boundaries.

    Returns
    -------
    List of dicts, each with keys from start_arg/end_arg stripped of dashes.
    E.g., [{"start": "2020-01-01", "end": "2020-06-30"}, ...]
    """
    duration_str = backtest["chunk_duration"]
    amount, suffix = _parse_duration(duration_str)

    start_arg = backtest.get("start_arg", "--start")
    end_arg = backtest.get("end_arg", "--end")

    # Strip leading dashes to get dict key names
    start_key = start_arg.lstrip("-")
    end_key = end_arg.lstrip("-")

    # Determine if sub-daily precision is needed
    sub_daily = suffix in ("H", "MIN")

    overall_start: date | datetime
    overall_end: date | datetime
    if sub_daily:
        overall_start = datetime.fromisoformat(backtest["start"])
        overall_end = datetime.fromisoformat(backtest["end"])
    else:
        overall_start = date.fromisoformat(backtest["start"])
        overall_end = date.fromisoformat(backtest["end"])

    periods: list[dict[str, str]] = []
    cursor = overall_start

    while cursor <= overall_end:
        # Compute next period start
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
            # End of this period is one second before next period starts
            period_end = min(next_cursor - timedelta(seconds=1), overall_end)
        else:
            # End of this period is day before next period starts
            period_end = min(next_cursor - timedelta(days=1), overall_end)

        periods.append({
            start_key: cursor.isoformat(),
            end_key: period_end.isoformat(),
        })

        cursor = next_cursor

    return periods


def build_task_manifest(
    run_cmd: str,
    grid: dict[str, list],
    result_dir_template: str,
    backtest: dict | None = None,
) -> dict:
    """Build a task manifest from a grid and optional backtest config.

    Parameters
    ----------
    run_cmd:
        Base command string (e.g. ``"python3 -m my_experiment.train"``).
    grid:
        ``param_name -> list_of_values``.
    result_dir_template:
        String with ``{run_id}`` placeholder.
    backtest:
        Optional backtest config dict. See :func:`expand_backtest`.
    """
    points = expand_grid(grid)

    if backtest:
        periods = expand_backtest(backtest)
        start_arg = backtest.get("start_arg", "--start")
        end_arg = backtest.get("end_arg", "--end")
        start_key = start_arg.lstrip("-")
        end_key = end_arg.lstrip("-")
    else:
        periods = [{}]

    tasks: dict[str, dict] = {}
    task_idx = 0
    for period in periods:
        for params in points:
            parts = [run_cmd]
            for k, v in params.items():
                parts.append(f"--{k} {v}")
            if period:
                parts.append(f"{start_arg} {period[start_key]}")
                parts.append(f"{end_arg} {period[end_key]}")

            entry: dict = {
                "cmd": " ".join(parts),
                "result_dir": result_dir_template.format(run_id=run_id(params)),
                "params": dict(params),
            }
            if period:
                entry["period"] = dict(period)

            tasks[str(task_idx)] = entry
            task_idx += 1

    n_tasks = len(tasks)

    return {
        "total_tasks": n_tasks,
        "grid_size": len(points),
        "grid_keys": list(grid.keys()),
        "tasks": tasks,
    }


def total_tasks(grid: dict[str, list], backtest: dict | None = None) -> int:
    """Product of all grid dimension sizes times number of backtest periods."""
    grid_size = prod(len(v) for v in grid.values())
    if backtest:
        return grid_size * len(expand_backtest(backtest))
    return grid_size
