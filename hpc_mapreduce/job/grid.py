"""Grid expansion and task manifest generation.

Pure computation — no I/O, only stdlib imports.

Manifest schema history:

* v1: initial format — ``schema_version``, ``total_tasks``, ``grid_size``,
  ``grid_keys``, and per-task ``cmd`` / ``result_dir`` / ``params`` /
  (optional) ``period``.
* v2: adds ``cmd_sha`` on every task (first 16 hex chars of the SHA-256 of
  the task's ``cmd`` string).  Provides a stable identifier for each task's
  command that observers (``/monitor``, status tools) can use to detect
  drift between the manifest and what actually ran.  The on-cluster
  dispatcher accepts both v1 and v2 for back-compat.
"""

from __future__ import annotations

import calendar
import hashlib
import itertools
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from math import prod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "expand_grid",
    "run_id",
    "build_task_manifest",
    "total_tasks",
    "expand_backtest",
    "attach_wave_map",
    "resolve_git_sha",
    "validate_result_dir_template",
]

# Placeholder names in ``result_dir`` templates that are resolved per-run
# (constant across every task in a manifest).  Grid-point keys vary per task.
_RUN_LEVEL_PLACEHOLDERS: frozenset[str] = frozenset({"run_id", "date", "git_sha"})

# Regex used to extract ``{name}`` placeholders from ``result_dir`` templates.
# Matches simple ``{identifier}`` — no format specs, no nested braces.  This is
# deliberately strict so users get a clear error for unsupported template
# features rather than silent behaviour.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Version marker embedded in every manifest produced by ``build_task_manifest``.
# Bump whenever the manifest shape changes in a way that on-cluster dispatch
# code must reject.  The dispatcher (hpc_mapreduce/map/dispatch.py) hardcodes
# its own expected value as a literal; keep the two in sync.
MANIFEST_SCHEMA_VERSION = 2


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

        periods.append(
            {
                start_key: cursor.isoformat(),
                end_key: period_end.isoformat(),
            }
        )

        cursor = next_cursor

    return periods


def resolve_git_sha(repo_path: str | Path | None = None) -> str:
    """Return the short (7-char) git SHA of ``HEAD`` in *repo_path*.

    Falls back to the literal string ``"nogit"`` when ``git`` is unavailable,
    the path is not a git repository, or the subprocess fails for any other
    reason.  This is intentionally permissive — ``result_dir`` templating
    should never hard-fail because the experiment lives outside a git repo.

    Parameters
    ----------
    repo_path:
        Directory in which to run ``git rev-parse HEAD``.  Defaults to the
        current working directory.
    """
    cwd = str(repo_path) if repo_path is not None else None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "nogit"
    if result.returncode != 0:
        return "nogit"
    sha = result.stdout.strip()
    if not sha:
        return "nogit"
    return sha[:7]


def _extract_placeholders(template: str) -> list[str]:
    """Return the names of every ``{name}`` placeholder in *template*.

    Preserves source order and keeps duplicates; callers that need a unique
    set can wrap the result in :class:`set`.
    """
    return _PLACEHOLDER_RE.findall(template)


def validate_result_dir_template(
    template: str,
    grid: dict[str, list],
) -> None:
    """Validate that every ``{name}`` in *template* can be resolved per-task.

    A placeholder is valid iff it is one of the run-level names
    (``run_id``, ``date``, ``git_sha``) or a grid key that appears in every
    grid point.  Since :func:`expand_grid` produces grid points with keys
    equal to ``grid.keys()``, this reduces to membership in ``grid``.

    Raises
    ------
    ValueError
        If any referenced placeholder is missing from both the run-level
        set and *grid*.  The error message lists the valid names and the
        missing one.
    """
    referenced = _extract_placeholders(template)
    valid_grid_keys = set(grid.keys())
    missing: list[str] = []
    for name in referenced:
        if name in _RUN_LEVEL_PLACEHOLDERS:
            continue
        if name in valid_grid_keys:
            continue
        if name not in missing:
            missing.append(name)
    if missing:
        valid = sorted(_RUN_LEVEL_PLACEHOLDERS | valid_grid_keys)
        raise ValueError(
            f"result_dir template {template!r} references unknown "
            f"placeholder(s) {missing!r}. Valid placeholders are: {valid}"
        )


def build_task_manifest(
    run_cmd: str,
    grid: dict[str, list],
    result_dir_template: str,
    backtest: dict | None = None,
    max_tasks: int | None = 10_000,
    repo_path: str | Path | None = None,
) -> dict:
    """Build a task manifest from a grid and optional backtest config.

    Parameters
    ----------
    run_cmd:
        Base command string (e.g. ``"python3 -m my_experiment.train"``).
    grid:
        ``param_name -> list_of_values``.
    result_dir_template:
        Template string for the per-task ``result_dir``.  Supports the
        run-level placeholders ``{run_id}`` (deterministic ID derived from
        a task's grid-point values), ``{date}`` (UTC ``YYYY-MM-DD`` at
        manifest-build time), ``{git_sha}`` (7-char ``HEAD`` SHA of the
        experiment repo, or ``"nogit"`` on failure), plus any grid key
        (e.g. ``{model}``, ``{dataset}``) present in *grid*.
    backtest:
        Optional backtest config dict. See :func:`expand_backtest`.
    max_tasks:
        Pre-flight ceiling on the number of tasks that will be materialized.
        If the computed total exceeds this value, a :class:`ValueError` is
        raised before any tasks are built.  Pass ``None`` to disable the
        check.  Defaults to ``10_000`` — large enough for typical grids but
        small enough to catch accidental explosion (e.g. a 10-year
        hour-chunked backtest that would produce ~87k tasks).
    repo_path:
        Directory used to resolve ``{git_sha}``.  Defaults to the current
        working directory.

    Raises
    ------
    ValueError
        If ``max_tasks`` is not ``None`` and the computed total exceeds it,
        or if ``result_dir_template`` references an unknown placeholder.
    """
    validate_result_dir_template(result_dir_template, grid)

    if max_tasks is not None:
        projected = total_tasks(grid, backtest)
        if projected > max_tasks:
            raise ValueError(
                f"build_task_manifest would produce {projected} tasks "
                f"(> max_tasks={max_tasks}). Pass max_tasks=None to disable "
                f"or raise the threshold."
            )

    points = expand_grid(grid)

    if backtest:
        periods = expand_backtest(backtest)
        start_arg = backtest.get("start_arg", "--start")
        end_arg = backtest.get("end_arg", "--end")
        start_key = start_arg.lstrip("-")
        end_key = end_arg.lstrip("-")
    else:
        periods = [{}]

    # Resolve run-level placeholders once — they are constant for every
    # task in this manifest.
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    git_sha = resolve_git_sha(repo_path)

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

            format_kwargs: dict[str, str] = {
                "run_id": run_id(params),
                "date": run_date,
                "git_sha": git_sha,
                **params,
            }
            entry: dict = {
                "cmd": " ".join(parts),
                "result_dir": result_dir_template.format(**format_kwargs),
                "params": dict(params),
            }
            entry["cmd_sha"] = hashlib.sha256(entry["cmd"].encode()).hexdigest()[:16]
            if period:
                entry["period"] = dict(period)

            tasks[str(task_idx)] = entry
            task_idx += 1

    n_tasks = len(tasks)

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "total_tasks": n_tasks,
        "grid_size": len(points),
        "grid_keys": list(grid.keys()),
        "tasks": tasks,
    }


def attach_wave_map(
    manifest: dict,
    wave_map: dict[int, list[int]],
) -> dict:
    """Return a *new* manifest dict with ``wave_map`` embedded.

    Keys in the wave map are converted to strings (JSON compatibility),
    and task IDs within each wave are also converted to strings so they
    match the string-keyed ``tasks`` dict in the manifest.

    The original *manifest* dict is **not** mutated.
    """
    # Convert int keys/values to strings for JSON round-tripping
    str_map: dict[str, list[str]] = {
        str(wave): [str(tid) for tid in tids] for wave, tids in wave_map.items()
    }
    return {**manifest, "wave_map": str_map}


def total_tasks(grid: dict[str, list], backtest: dict | None = None) -> int:
    """Product of all grid dimension sizes times number of backtest periods."""
    grid_size = prod(len(v) for v in grid.values())
    if backtest:
        return grid_size * len(expand_backtest(backtest))
    return grid_size
