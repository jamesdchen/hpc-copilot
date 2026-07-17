"""Guaranteed terminal-harvest guard (design §5, "Guaranteed harvest").

Every terminal path of the monitor — completion, failure, timeout /
cap-overrun, abandoned, partial-kill — AND any abnormal exit from the
poll loop (an unhandled exception, a dead chat session) must end in a
best-effort code-harvest of whatever exists: the metrics envelope plus a
per-wave error sweep. *No path ends in silence.*

:func:`harvest_on_terminal` is that guarantee. It is:

* **best-effort** — every step is guarded independently. A cluster that
  is unreachable, a run with nothing combined yet, a corrupt sidecar all
  degrade to a *recorded* non-fatal outcome, never a raise.
* **loud** — success OR failure, it appends a durable marker line to
  ``<run_id>.harvest.jsonl`` under the journal run dir, and logs a
  warning on any partial failure. A silent swallow is a bug
  (``docs/internals/engineering-principles.md`` — a primitive fails
  loudly); a harvest that could not run records *why*, it does not
  vanish.
* **cause-preserving** — it NEVER raises and NEVER masks the terminal
  cause that led here. It is designed to be called from a ``finally``
  while an exception is in flight, so it must let that exception
  propagate untouched: it does not swallow ``KeyboardInterrupt`` /
  ``SystemExit`` (only operational ``Exception`` s from the harvest
  itself), and it returns rather than re-raising.

The metrics harvest deliberately invokes the SAME aggregate entry the
driver routes to downstream (``aggregate-flow``), so a driver that stops
after the terminal tick — the crash / session-death gap (§5 gap (d)) —
still produces a metrics + error envelope inline. It runs with
``ensure_all_combined=False``: harvest whatever already exists without
forcing a fresh cluster combine (idempotent, cheap) and without the
terminal-status precondition gate, so an abnormal mid-flight exit still
harvests partial data.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hpc_agent.errors import ScopeLocked, SshCircuitOpen
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.run_record import runs_dir

__all__ = [
    "CIRCUIT_WAIT_CAP_SEC",
    "TERMINAL_CAUSES",
    "harvest_marker_path",
    "harvest_on_terminal",
    "harvest_receipt_exists",
]

_log = logging.getLogger(__name__)

#: Longest remaining breaker cooldown the terminal harvest will wait out
#: before its single retry: the breaker's BASE cooldown (300s) plus slack.
#: A remaining cooldown past this cap means the cooldown DOUBLED — the
#: half-open probe already failed, the host is genuinely unhealthy — and the
#: guard records the failure without waiting, exactly as before
#: (2026-07-06: a 3×60s hoffman2 latency spike opened the breaker mid-harvest;
#: the guard recorded ``harvest_ok:false`` and parked a finished 20/20 run
#: even though the exception named a deadline 292s away).
CIRCUIT_WAIT_CAP_SEC = 330.0

#: Buffer past the breaker deadline so the retry lands after the cooldown —
#: a retry a hair early just fails fast again.
_CIRCUIT_WAIT_SLACK_SEC = 5.0

#: The named terminal causes the design enumerates (§5). Free-form causes
#: are still accepted — the guard never rejects a caller — but these are the
#: vocabulary a reader should expect, and ``"abnormal-exit"`` is the sentinel
#: the poll-loop ``finally`` uses when no clean terminal branch was reached.
TERMINAL_CAUSES = frozenset(
    {
        "complete",
        "failed",
        "timeout",
        "cap-overrun",
        "abandoned",
        "partial-kill",
        "abnormal-exit",
    }
)


def harvest_marker_path(experiment_dir: Path, run_id: str) -> Path:
    """Durable per-run harvest ledger (``<run_id>.harvest.jsonl``).

    Lives beside the ``<run_id>.monitor.jsonl`` tick log under the journal
    run dir. Append-only: one JSON line per harvest attempt, so re-arming a
    run (idempotent by design) accretes evidence rather than clobbering it.
    """
    return runs_dir(experiment_dir) / f"{run_id}.harvest.jsonl"


def harvest_receipt_exists(experiment_dir: Path, run_id: str) -> bool:
    """True when the harvest ledger records at least one PERFORMED harvest.

    The durable, JOURNAL-side evidence that :func:`harvest_on_terminal` was
    reached for this run. The marker is written LAST (``_write_marker``), after
    the metrics harvest + error sweep, and even a harvest that FAILED
    (``harvest_ok: false``) or hit a deliberate ``scope_locked`` skip records
    one — so a present marker proves the guaranteed harvest ran for a terminal
    state, and an ABSENT ledger proves it never did (the terminal-with-no-
    harvest gap: a session-death between ``mark_run(terminal)`` and this guard).

    This is the backstop trigger the reconcile/settle terminal arms derive
    "harvest owed" from: a run that is terminal but has NO receipt is owed a
    harvest REGARDLESS of a verdict transition, so a death in the mark→harvest
    window is re-driven on the next reconcile — and once a receipt lands the
    backstop stops re-firing (idempotent both ways; the transition gate still
    covers the normal, receipt-writing path).

    The abnormal-exit ``run_not_terminal`` clean-skip marker (the watch died
    while the run was NOT terminal — nothing was pulled) is deliberately NOT a
    receipt: it records a no-op, not a performed terminal harvest.

    Best-effort read: a missing / unreadable ledger reads as "no receipt", so
    the backstop errs toward re-harvesting (harvest_on_terminal is itself
    idempotent). Whole-line-atomic appends keep prior markers intact, so a
    crash-torn final line still leaves earlier markers readable — the scan runs
    over every line, newest-first, and returns on the first qualifying marker.
    """
    path = harvest_marker_path(experiment_dir, run_id)
    if not path.is_file():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        # A `run_not_terminal` skip is the abnormal-exit sentinel's no-op (the
        # watch died, the run wasn't terminal) — NOT a performed harvest. Every
        # other marker (a real harvest, a failed-but-recorded harvest, or a
        # deliberate scope-locked skip) means the guaranteed harvest was REACHED
        # for a terminal state, so the run is not "harvest owed".
        if parsed.get("harvest_skipped_reason") == "run_not_terminal":
            continue
        return True
    return False


def harvest_on_terminal(
    experiment_dir: Path,
    run_id: str,
    *,
    terminal_cause: str,
    record: Any | None = None,
    _aggregate: Callable[[Path, str], Any] | None = None,
    _sweep: Callable[[str, str], dict[int, list[str]]] | None = None,
    _clock: Callable[[], float] = time.time,
    _sleep: Callable[[float], object] = time.sleep,
) -> dict[str, Any]:
    """Best-effort, loud, guaranteed code-harvest at any terminal path.

    Given a run that reached ANY terminal condition (``terminal_cause`` one
    of :data:`TERMINAL_CAUSES`) or an abnormal loop exit
    (``terminal_cause="abnormal-exit"``), attempt in order, each step
    independently guarded:

    (a) the metrics harvest (invoke ``aggregate-flow``),
    (b) an error sweep (the combiner's per-wave read-error ledger),
    (c) a durable, loud marker recording what was harvested.

    Returns the marker dict (also written to disk). NEVER raises and NEVER
    masks the terminal cause. ``_aggregate`` / ``_sweep`` are injected seams
    for tests; production callers leave them at the defaults.
    """
    marker: dict[str, Any] = {
        "harvested_at": utcnow_iso(),
        "run_id": run_id,
        "terminal_cause": terminal_cause,
        "metrics_harvested": False,
        "metrics_error": None,
        "harvest_skipped_reason": None,
        "circuit_waited_sec": None,
        "aggregated_metric_keys": [],
        "escalation_reason": None,
        "error_sweep_ran": False,
        "error_sweep_error": None,
        "waves_with_errors": {},
        "harvest_ok": False,
    }
    combiner_dir: str | None = None

    # POSITIVE-EVIDENCE GATE (run-#12 finding 19): the "abnormal-exit"
    # sentinel means the WATCH died, not the run — three ssh timeouts
    # unwound the poll loop while 27 healthy jobs sat in the queue, and this
    # guard then pulled a LIVE run's results for 1800s. Under the sentinel,
    # harvest only when the JOURNAL positively records a terminal status
    # (read FRESH — the caller's record predates the unwind); otherwise
    # record a clean skip and return. The watchdog / doctor re-arm the
    # watch, and the real terminal path harvests later under its own cause.
    if terminal_cause == "abnormal-exit":
        from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES
        from hpc_agent.state.journal import load_run

        fresh: Any | None = None
        with contextlib.suppress(Exception):
            fresh = load_run(experiment_dir, run_id)
        status = getattr(fresh if fresh is not None else record, "status", None)
        if status not in TERMINAL_STATUSES:
            marker["harvest_skipped_reason"] = "run_not_terminal"
            _log.warning(
                "terminal harvest: abnormal watch exit for run %s but the journal "
                "records status %r (not terminal) — clean skip, nothing pulled; "
                "re-arm the watch (the watchdog sees the stamp gap).",
                run_id,
                getattr(status, "value", status),
            )
            _write_marker(experiment_dir, run_id, marker)
            return marker

    # (a) Metrics harvest — the same aggregate entry the driver routes to,
    #     so a crash / session-death after the terminal tick still yields a
    #     metrics envelope. Operational failures (SSH down, nothing to
    #     combine, corrupt sidecar) are recorded LOUDLY, never swallowed.
    #     KeyboardInterrupt / SystemExit are NOT caught — a user force-quit
    #     mid-harvest must still propagate.
    aggregate = _aggregate if _aggregate is not None else _default_aggregate
    scope_locked = False
    try:
        try:
            result = aggregate(experiment_dir, run_id)
        except SshCircuitOpen as exc:
            # The breaker names its own deadline, and a detached terminal
            # worker has nowhere else to be: wait out one BASE cooldown and
            # retry ONCE (the retry claims the sanctioned half-open probe
            # slot — this is not the hammering the breaker forbids). A
            # missing deadline or one past the cap (a DOUBLED cooldown: the
            # probe already failed, the host is genuinely unhealthy) records
            # the failure without waiting, as before.
            wait = _circuit_wait_sec(exc, now=_clock())
            if wait is None:
                raise
            _log.warning(
                "terminal harvest: ssh circuit open for run %s — waiting %.0fs "
                "for the breaker deadline, then retrying once",
                run_id,
                wait,
            )
            _sleep(wait)
            marker["circuit_waited_sec"] = round(wait, 1)
            result = aggregate(experiment_dir, run_id)
        marker["metrics_harvested"] = True
        agg_metrics = getattr(result, "aggregated_metrics", None) or {}
        marker["aggregated_metric_keys"] = sorted(str(k) for k in agg_metrics)
        marker["escalation_reason"] = getattr(result, "escalation_reason", None)
        cdl = getattr(result, "combiner_dir_local", None)
        combiner_dir = str(cdl) if cdl else None
    except ScopeLocked as exc:
        # A LOCKED scope is deliberate human state, not a harvest failure: the
        # scope gate refused the reduction on purpose. Record a CLEAN SKIP —
        # never ``harvest_ok:false``, never an anomaly — so the automatic
        # terminal harvest does not paint a human's lock red forever. The
        # error sweep is skipped too (nothing was harvested to sweep).
        scope_locked = True
        marker["harvest_skipped_reason"] = "scope_locked"
        _log.info(
            "terminal harvest: run %s scope-locked — clean skip (cause=%s): %s",
            run_id,
            terminal_cause,
            exc,
        )
    except Exception as exc:  # noqa: BLE001 — safety net must record, not mask
        marker["metrics_error"] = f"{type(exc).__name__}: {exc}"
        _log.warning(
            "terminal harvest: metrics harvest failed for run %s (cause=%s): %s",
            run_id,
            terminal_cause,
            marker["metrics_error"],
        )

    # (b) Error sweep — the per-wave read-error ledger the combiner recorded.
    #     Falls back to the default aggregate output dir when the metrics
    #     harvest could not report a combiner dir, so partial artifacts that
    #     already landed locally are still swept.
    sweep = _sweep if _sweep is not None else _default_sweep
    sweep_dir = combiner_dir or str(experiment_dir / "_aggregated" / run_id / "_combiner")
    if not scope_locked:
        try:
            errs = sweep(sweep_dir, run_id)
            marker["error_sweep_ran"] = True
            marker["waves_with_errors"] = {
                str(w): [str(e) for e in v] for w, v in (errs or {}).items()
            }
        except Exception as exc:  # noqa: BLE001 — safety net must record, not mask
            marker["error_sweep_error"] = f"{type(exc).__name__}: {exc}"
            _log.warning(
                "terminal harvest: error sweep failed for run %s (dir=%s): %s",
                run_id,
                sweep_dir,
                marker["error_sweep_error"],
            )

    marker["harvest_ok"] = marker["metrics_error"] is None and marker["error_sweep_error"] is None

    # (c) Durable, loud marker — no terminal path is silent.
    _write_marker(experiment_dir, run_id, marker)
    if not marker["harvest_ok"]:
        _log.warning(
            "terminal harvest for run %s (cause=%s) completed with errors: %s",
            run_id,
            terminal_cause,
            marker,
        )
    return marker


def _circuit_wait_sec(exc: SshCircuitOpen, *, now: float) -> float | None:
    """Bounded wait (seconds) until *exc*'s breaker deadline, or ``None`` to give up.

    ``None`` when the raiser attached no deadline (bare construction — tests,
    older sites) or the remaining cooldown exceeds
    :data:`CIRCUIT_WAIT_CAP_SEC` (a doubled cooldown: the half-open probe
    already failed, so waiting would ride a genuinely unhealthy host).
    """
    deadline = getattr(exc, "deadline", None)
    if deadline is None:
        return None
    remaining = max(0.0, float(deadline) - now)
    if remaining > CIRCUIT_WAIT_CAP_SEC:
        return None
    return remaining + _CIRCUIT_WAIT_SLACK_SEC


def _default_aggregate(experiment_dir: Path, run_id: str) -> Any:
    """Invoke the real ``aggregate-flow`` harvest (metrics + escalation sweep).

    Imported lazily so importing this guard (and thus the monitor hot path)
    does not drag in the aggregate stack's heavy transitive imports.
    ``ensure_all_combined=False`` → harvest whatever already combined,
    bypassing the terminal-status precondition gate and the pre-pull combine.
    """
    from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec

    # Import the top-level ``aggregate-flow`` COMPOSITE via the package alias
    # form (``from hpc_agent.ops import <module>``). This guard lives in the
    # ``monitor`` subject; the direct ``from hpc_agent.ops.aggregate_flow
    # import ...`` form trips the subject-import lint, which cannot tell a
    # top-level ops composite from a sibling-subject directory. The alias form
    # is the sanctioned spelling for a subject file reaching a top-level ops
    # module (see ``scripts/lint_subject_imports.py`` — alias-derived names are
    # checked against real subject dirs, and ``aggregate_flow`` is a module,
    # not a subject). aggregate-flow is a composite, not a monitor internal.
    from hpc_agent.ops import aggregate_flow as aggregate_flow_module

    return aggregate_flow_module.aggregate_flow(
        experiment_dir,
        spec=AggregateFlowSpec(run_id=run_id, ensure_all_combined=False),
    )


def _default_sweep(combiner_dir: str, run_id: str) -> dict[int, list[str]]:
    """Map wave → per-task read errors the combiner recorded (missing dir → {}).

    Threads *run_id* so the run-scoped ``_combiner/<run_id>/wave_*.json`` layout
    (BR-9) is swept, with the F05 foreign-run filter still applied to legacy-flat
    partials.
    """
    from hpc_agent.execution.mapreduce.reduce.metrics import collect_wave_errors

    return collect_wave_errors(combiner_dir, run_id=run_id)


def _write_marker(experiment_dir: Path, run_id: str, marker: dict[str, Any]) -> None:
    """Append one JSON line to the durable harvest ledger (best-effort, loud).

    Even the marker write is guarded: if it cannot be recorded we log
    LOUDLY rather than let the guard raise into a caller's ``finally`` and
    mask the terminal cause.
    """
    try:
        # Route through the canonical JSONL-append seam (flock + fsync +
        # sort_keys) so a torn/interleaved final line can't strand a finished
        # run's evidence. The seam CAN raise OSError; this never-raise wrapper
        # (the guard runs from a caller's ``finally``) swallows it into a log.
        append_jsonl_line(harvest_marker_path(experiment_dir, run_id), marker)
    except Exception as exc:  # noqa: BLE001 — last-resort: log, never raise
        with contextlib.suppress(Exception):
            _log.warning(
                "terminal harvest: could not write harvest marker for run %s: %s",
                run_id,
                exc,
            )
