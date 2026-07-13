"""Live terminal UI for ``/status`` (opt-in ``--tui`` path).

A thin wrapper around :func:`hpc_agent.execution.mapreduce.reduce.status.report_status_from_tasks`
that polls the cluster on a fixed cadence and renders the result with Rich.
The JSON / cron path in ``status.py`` is unchanged; the TUI is imported
lazily so a user without ``rich`` installed pays zero cost for the normal
``/status`` flow.

Invoke directly::

    python -m hpc_agent.execution.mapreduce.reduce.tui --run-id <run_id> \\
        --job-ids 12345,12346 --poll-interval 30

Keybinds (single-keystroke, non-blocking read on stdin):

- ``r`` force an immediate refresh
- ``f`` toggle focus on the failing-tasks panel
- ``l`` open the currently focused task's error log via ``ssh``
- ``q`` quit

If stdin is not a TTY (e.g. redirected output), the keybind reader is
skipped and the UI just auto-refreshes until Ctrl-C.

Design note: nothing in this file is imported by the default ``/status``
JSON path.  ``status.py`` does not know this module exists.
"""

from __future__ import annotations

__all__ = ["run_tui"]

import argparse
import contextlib
import json
import os
import select
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

# termios/tty drive raw-terminal keypress mode and exist only on POSIX. The
# module must still *import* on Windows (the design contract — see the
# test_module_imports_without_rich check), so import them optionally and fall
# through to a no-op keypress reader when they're absent. Under TYPE_CHECKING
# we import them unconditionally so mypy keeps the precise typeshed stubs.
if TYPE_CHECKING:
    import termios
    import tty
else:
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - exercised only on non-POSIX
        termios = None
        tty = None

from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _UiState:
    """Mutable state that survives across refresh ticks."""

    # ``time.monotonic`` (not wall-clock ``time.time``) so the elapsed
    # display + the poll-interval check stay sane across NTP corrections
    # / suspend-resume / manual clock changes — wall-clock can step
    # backwards, leading to negative elapsed strings and stalled polls.
    start_ts: float = field(default_factory=time.monotonic)
    focused_failing: bool = False
    last_report: dict | None = None
    last_per_task_dict: dict | None = None
    # Task id (string, 0-based HpcTaskId, matches report["tasks"] keys) of the
    # currently focused failing task, or None when focus is off.
    focused_task_id: str | None = None


def _load_per_task_dict(per_task_dict_path: Path) -> dict:
    """Load & parse the synthetic per-task dict written next to the run sidecar.

    Raises ``FileNotFoundError`` if absent.
    """
    if not per_task_dict_path.is_file():
        raise FileNotFoundError(f"per-task dict not found: {per_task_dict_path}")
    data: dict = json.loads(per_task_dict_path.read_text(encoding="utf-8"))
    return data


def _fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _classify_failures(report: dict, per_task_dict: dict) -> dict[str, int]:
    """Bucket failing/unknown tasks by :func:`classify_failure` category.

    Lazy-imports classify so we don't pay for log reads when no tasks are
    failing.  Logs are read lazily via the ``err_log_paths`` mapping
    populated by :mod:`hpc_agent.execution.mapreduce.reduce.status`; any unreadable log
    is silently bucketed as ``"unknown"``.
    """
    from hpc_agent.execution.mapreduce.reduce.classify import classify_failure

    err_paths = report.get("err_log_paths") or {}
    counts: dict[str, int] = {}
    tasks = report.get("tasks") or {}
    for tid, info in tasks.items():
        status = info.get("status")
        if status not in {"failed", "unknown"}:
            continue
        path = err_paths.get(tid)
        if not path:
            counts["unknown"] = counts.get("unknown", 0) + 1
            continue
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")[-8000:]
        except OSError:
            counts["unknown"] = counts.get("unknown", 0) + 1
            continue
        cat = classify_failure(text)
        counts[cat] = counts.get(cat, 0) + 1
    _ = per_task_dict  # accepted for symmetry; not needed today
    return counts


def _failing_tail(report: dict, limit: int = 10) -> list[tuple[str, str]]:
    """Return the last *limit* failing tasks as ``(tid, one-line diagnostic)``.

    Diagnostic is the last non-empty stderr line of the task's err log, or
    ``""`` if no log is available.
    """
    err_paths = report.get("err_log_paths") or {}
    tasks = report.get("tasks") or {}
    out: list[tuple[str, str]] = []
    for tid, info in tasks.items():
        if info.get("status") not in {"failed", "unknown"}:
            continue
        path = err_paths.get(tid)
        diag = ""
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
                for line in reversed(text.splitlines()):
                    line = line.strip()
                    if line:
                        diag = line[:120]
                        break
            except OSError:
                diag = "(log unreadable)"
        out.append((tid, diag))
    # Sort by int task id, then keep the last `limit`.
    with contextlib.suppress(ValueError):
        out.sort(key=lambda kv: int(kv[0]))
    return out[-limit:]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _render(state: _UiState, report: dict, per_task_dict: dict, poll_interval: int) -> Any:
    """Build the Rich renderable tree for the current snapshot."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, TextColumn
    from rich.table import Table
    from rich.text import Text

    from hpc_agent.execution.mapreduce.reduce.status import rollup_by_grid_point

    # Header -----------------------------------------------------------------
    run_id = per_task_dict.get("run_id") or per_task_dict.get("project") or "(unknown)"
    cluster = per_task_dict.get("cluster") or "(unknown)"
    scheduler = report.get("scheduler", "?")
    wall = _fmt_elapsed(time.monotonic() - state.start_ts)

    summary = report.get("summary") or {}
    header_tbl = Table.grid(padding=(0, 2))
    header_tbl.add_column(style="bold cyan")
    header_tbl.add_column()
    header_tbl.add_row("run_id", str(run_id))
    header_tbl.add_row("cluster", str(cluster))
    header_tbl.add_row("scheduler", str(scheduler))
    header_tbl.add_row("wall-clock", wall)
    header_tbl.add_row(
        "summary",
        f"complete={summary.get('complete', 0)} "
        f"running={summary.get('running', 0)} "
        f"pending={summary.get('pending', 0)} "
        f"failed={summary.get('failed', 0)} "
        f"unknown={summary.get('unknown', 0)}",
    )

    # Per-grid-point rollup table -------------------------------------------
    rollup = rollup_by_grid_point(report, per_task_dict)
    rollup_tbl = Table(title="Per grid-point", show_lines=False, expand=True)
    rollup_tbl.add_column("grid point", overflow="fold")
    rollup_tbl.add_column("queued", justify="right")
    rollup_tbl.add_column("running", justify="right")
    rollup_tbl.add_column("done", justify="right", style="green")
    rollup_tbl.add_column("failed", justify="right", style="red")
    for gp, buckets in sorted(rollup.items()):
        rollup_tbl.add_row(
            gp,
            str(buckets.get("pending", 0)),
            str(buckets.get("running", 0)),
            str(buckets.get("complete", 0)),
            str(buckets.get("failed", 0) + buckets.get("unknown", 0)),
        )

    # Wave progress bars ----------------------------------------------------
    wave_map = per_task_dict.get("wave_map") or {}
    wave_progress: Any
    if wave_map:
        wave_progress = Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            expand=True,
        )
        tasks = report.get("tasks") or {}
        # wave_map keys can be str or int, values are lists of task-id strings.
        for wave_key in sorted(wave_map.keys(), key=lambda k: int(k) if str(k).isdigit() else 0):
            members = wave_map[wave_key] or []
            total = len(members)
            # wave_map members and the report's tasks keys are both 0-based
            # HpcTaskId — look up directly, no shift.
            done = 0
            for raw_tid in members:
                try:
                    tid_key = str(int(raw_tid))
                except (TypeError, ValueError):
                    continue
                if tasks.get(tid_key, {}).get("status") == "complete":
                    done += 1
            wave_progress.add_task(f"wave {wave_key}", total=total, completed=done)
    else:
        wave_progress = Text("(no wave_map in sidecar)", style="dim")

    # Failure classification -----------------------------------------------
    fail_counts = _classify_failures(report, per_task_dict)
    fail_tbl = Table(title="Failure classification", expand=True)
    fail_tbl.add_column("category")
    fail_tbl.add_column("count", justify="right")
    if fail_counts:
        for cat in sorted(fail_counts):
            fail_tbl.add_row(cat, str(fail_counts[cat]))
    else:
        fail_tbl.add_row("(none)", "0")

    # Failing-task tail ----------------------------------------------------
    tail = _failing_tail(report, limit=10)
    tail_tbl = Table(title="Recent failing tasks", expand=True)
    tail_tbl.add_column("task_id", justify="right")
    tail_tbl.add_column("diagnostic", overflow="fold")
    if tail:
        from rich.markup import escape as _rich_escape

        # Bind focus BEFORE drawing rows so the first frame after the
        # user presses 'f' shows a focused row instead of an unmarked one
        # that only becomes visible on the next refresh.
        if state.focused_failing and state.focused_task_id is None:
            state.focused_task_id = tail[-1][0]
        for tid, diag in tail:
            is_focused = state.focused_failing and str(tid) == str(state.focused_task_id)
            prefix = "> " if is_focused else ""
            # Escape diagnostic text — raw log content can contain
            # bracketed sequences ([red]Error[/red], etc.) that Rich
            # would otherwise interpret as markup or trip MarkupError.
            tail_tbl.add_row(
                prefix + str(tid),
                _rich_escape(diag) if diag else "(no diagnostic)",
            )
    else:
        tail_tbl.add_row("-", "(no failures)")

    # Footer: resource usage ------------------------------------------------
    ru = report.get("resource_usage") or {}
    footer_txt = (
        f"cpu-hours: {ru.get('cpu_hours', 0):.2f}   "
        f"gpu-hours: {ru.get('gpu_hours', 0):.2f}   "
        f"tasks_counted: {ru.get('tasks_counted', 0)}   "
        f"(refresh every {poll_interval}s — press 'r' now, 'q' quit, 'f' focus, 'l' open log)"
    )

    return Group(
        Panel(header_tbl, title="/status", border_style="cyan"),
        rollup_tbl,
        Panel(wave_progress, title="Waves", border_style="blue"),
        fail_tbl,
        tail_tbl,
        Panel(Text(footer_txt, style="bold"), border_style="magenta"),
    )


# ---------------------------------------------------------------------------
# Keyboard polling (non-blocking, TTY only)
# ---------------------------------------------------------------------------


class _RawStdin:
    """Context manager that puts stdin in cbreak so we can read one char at a time.

    Falls through to a no-op if stdin isn't a TTY (e.g. output redirected).
    """

    def __init__(self) -> None:
        self._fd: int | None = None
        self._old: list[Any] | None = None

    def __enter__(self) -> _RawStdin:
        try:
            fd = sys.stdin.fileno()
        except (ValueError, OSError):
            return self
        if not os.isatty(fd):
            return self
        if termios is None or tty is None:  # non-POSIX: no raw mode, stay a no-op
            return self
        self._fd = fd
        try:
            self._old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except termios.error:
            self._fd = None
            self._old = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None and self._old is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def poll(self, timeout: float) -> str | None:
        """Return one pending keystroke, or None after *timeout* seconds."""
        if self._fd is None:
            time.sleep(timeout)
            return None
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(self._fd, 1).decode("utf-8", errors="ignore")
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Log-open helper
# ---------------------------------------------------------------------------


def _open_log(ssh_target: str | None, log_path: str, live: Any) -> None:
    """Spawn ``ssh <target> less <log>`` in a subshell, pausing the Live view.

    If no *ssh_target* is configured, fall back to local ``less`` so that a
    plain file path still opens cleanly.
    """
    if ssh_target:
        # When ssh forwards positional args to a remote shell they are
        # joined and re-parsed there. A log path with whitespace or shell
        # metachars would mis-parse (or, in the worst case, injection-
        # execute). Quote the path explicitly with shlex.quote.
        import shlex as _shlex

        from hpc_agent.infra.ssh_options import ssh_argv

        # Route through the ssh_argv seam so this viewer uses the native
        # Windows OpenSSH binary + the ControlMaster override like every
        # other ssh call (#158), not a Git-Bash-shadowed bare "ssh".
        argv: list[str] = [*ssh_argv("ssh"), ssh_target, f"less {_shlex.quote(log_path)}"]
    else:
        argv = ["less", log_path]
    # Stop the Live refresh before handing the TTY over to less.
    live.stop()
    try:
        subprocess.call(argv)
    except FileNotFoundError:
        pass
    finally:
        live.start(refresh=True)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_tui(
    per_task_dict_path: str | Path,
    *,
    job_ids: list[str] | None = None,
    poll_interval: int = 30,
    scheduler: str | None = None,
    log_dir: str = "",
    scratch_dir: str = "",
    job_name: str = "",
    slurm_cluster: str | None = None,
    sge_user: str | None = None,
    file_glob: str = "*",
    ssh_target: str | None = None,
) -> int:
    """Run the Rich-based live monitor.

    Imports ``rich`` lazily so callers without the ``tui`` extra installed
    can still import :mod:`hpc_agent.execution.mapreduce.reduce` without side effects.
    Returns an integer exit code suitable for ``sys.exit``.
    """
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError as exc:  # pragma: no cover - exercised only w/o extra
        print(
            "rich is required for --tui; install with `pip install 'hpc-agent[tui]'`",
            file=sys.stderr,
        )
        print(f"(import error: {exc})", file=sys.stderr)
        return 2

    from hpc_agent.execution.mapreduce.reduce.status import report_status_from_tasks

    per_task_dict_path = Path(per_task_dict_path)
    try:
        per_task_dict = _load_per_task_dict(per_task_dict_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"per-task dict parse error: {exc}", file=sys.stderr)
        return 2

    state = _UiState(last_per_task_dict=per_task_dict)
    console = Console()

    def _poll() -> dict:
        # Reload the per-task dict each tick — it's cheap and resilient to edits.
        try:
            mf = _load_per_task_dict(per_task_dict_path)
        except (FileNotFoundError, json.JSONDecodeError):
            mf = state.last_per_task_dict or {}
        state.last_per_task_dict = mf
        rep = report_status_from_tasks(
            mf,
            job_ids or [],
            scheduler=scheduler,
            file_glob=file_glob,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            job_name=job_name,
            slurm_cluster=slurm_cluster,
            sge_user=sge_user,
        )
        state.last_report = rep
        return rep

    initial = _poll()

    with (
        _RawStdin() as keys,
        Live(
            _render(state, initial, state.last_per_task_dict or {}, poll_interval),
            console=console,
            refresh_per_second=4,
            screen=False,
        ) as live,
    ):
        last_poll = time.monotonic()
        while True:
            # Poll keys in short slices so the UI feels responsive even when
            # the scheduler side-call is slow.
            slice_s = min(1.0, max(0.1, poll_interval / 10))
            key = keys.poll(slice_s)
            if key == "q":
                break
            if key == "r":
                live.update(
                    _render(state, _poll(), state.last_per_task_dict or {}, poll_interval),
                    refresh=True,
                )
                last_poll = time.monotonic()
                continue
            if key == "f":
                state.focused_failing = not state.focused_failing
                if not state.focused_failing:
                    state.focused_task_id = None
                live.update(
                    _render(
                        state,
                        state.last_report or initial,
                        state.last_per_task_dict or {},
                        poll_interval,
                    ),
                    refresh=True,
                )
                continue
            if key == "l":
                tid = state.focused_task_id
                rep = state.last_report or {}
                err_paths = rep.get("err_log_paths") or {}
                log_path = err_paths.get(str(tid)) if tid is not None else None
                if log_path:
                    _open_log(ssh_target, log_path, live)
                continue

            if time.monotonic() - last_poll >= poll_interval:
                live.update(
                    _render(state, _poll(), state.last_per_task_dict or {}, poll_interval),
                    refresh=True,
                )
                last_poll = time.monotonic()

    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live terminal UI for /status. Requires rich (pip install hpc-agent[tui]).",
    )
    parser.add_argument(
        "--run-id",
        required=True,
        help="Run ID — locates the sidecar at .hpc/runs/<run_id>.json.",
    )
    parser.add_argument("--job-ids", default="", help="Comma-separated scheduler job IDs")
    parser.add_argument("--job-name", default="")
    # Free string: the orchestrator supplies an already-validated backend name
    # (the wire ``Scheduler`` type gates it), and the profile engine rejects an
    # unknown family downstream. No closed enum to mirror here (#337).
    parser.add_argument("--scheduler", default=None)
    parser.add_argument("--poll-interval", type=int, default=30)
    parser.add_argument("--log-dir", default="")
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--slurm-cluster", default=None)
    parser.add_argument("--sge-user", default=None)
    parser.add_argument("--file-glob", default="*")
    parser.add_argument(
        "--ssh-target",
        default=None,
        help="user@host for log-open keybind; optional",
    )
    args = parser.parse_args(argv)

    # Materialize the synthetic per-task dict from the sidecar +
    # .hpc/tasks.py and write it to a stable path next to the sidecar
    # so the existing poll loop (which reloads on each tick) can re-read
    # it cheaply.
    from pathlib import Path as _P

    from hpc_agent.execution.mapreduce.reduce.status import (
        _build_per_task_dict_from_sidecar,
        resolve_report_tasks_module,
    )

    sidecar_path = _P(".hpc") / "runs" / f"{args.run_id}.json"
    if not sidecar_path.is_file():
        print(f"run sidecar not found: {sidecar_path}", file=sys.stderr)
        return 2
    try:
        # Route through the hardened reader so v1-shape sidecars get the
        # v2 backfill (task_count, result_dir_template) the downstream
        # _build_per_task_dict_from_sidecar requires.
        from hpc_agent.state.runs import read_run_sidecar

        sidecar = read_run_sidecar(_P("."), args.run_id)
    except Exception as exc:
        print(f"failed to read sidecar: {exc}", file=sys.stderr)
        return 2
    # Frozen-manifest-vs-tasks.py resolved by the SAME shared accessor the status
    # reporter uses (#29): the TUI no longer unconditionally imports/replays
    # tasks.py — a frozen manifest skips the import (no phantom optuna/pbt trial
    # at launch), a foreign/heavy tasks.py degrades instead of wedging, and the
    # import fires only when the template genuinely needs resolve() kwargs.
    tasks_module, frozen_trial_params, _, tasks_py_missing = resolve_report_tasks_module(sidecar)
    if tasks_py_missing is not None:
        print(f"tasks.py not found: {tasks_py_missing}", file=sys.stderr)
        return 2
    try:
        per_task_dict = _build_per_task_dict_from_sidecar(
            sidecar, tasks_module, trial_params=frozen_trial_params
        )
    except Exception as exc:
        print(f"failed to build per-task dict: {exc}", file=sys.stderr)
        return 2
    # Atomic write: a SIGINT mid-write would otherwise leave a
    # truncated JSON file, which the next TUI launch reads as `{}` and
    # silently shows every task as `unknown`. ``atomic_write_json``
    # uses a mkstemp-randomised tmp name so two concurrent TUI launches
    # against the same run can't collide on a shared ``.tmp`` sibling,
    # and adds a fsync for crash durability.
    per_task_dict_path = sidecar_path.with_suffix(".per-task-dict.json")
    from hpc_agent.infra.io import atomic_write_json as _atomic_write_json

    _atomic_write_json(per_task_dict_path, per_task_dict)

    job_ids = [j for j in args.job_ids.split(",") if j.strip()]
    return run_tui(
        per_task_dict_path,
        job_ids=job_ids,
        poll_interval=args.poll_interval,
        scheduler=args.scheduler,
        log_dir=args.log_dir,
        scratch_dir=args.scratch_dir,
        job_name=args.job_name,
        slurm_cluster=args.slurm_cluster,
        sge_user=args.sge_user,
        file_glob=args.file_glob,
        ssh_target=args.ssh_target,
    )


if __name__ == "__main__":
    raise SystemExit(_main())
