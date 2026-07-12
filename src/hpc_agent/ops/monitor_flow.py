"""``monitor-flow``: workflow atom that polls a run to terminal.

Pairs with :func:`hpc_agent.ops.submit_flow.submit_flow` to give
higher-level workflows (campaigns, sweeps) a clean composition path:
``submit-flow → monitor-flow → next iteration``. Both atoms expose the
same envelope shape, so the campaign loop's per-iteration code is just
two ``hpc-agent <verb> --spec foo.json`` invocations.

What it does
------------
Internal poll loop:

1. ``record_status(...)`` — fresh status from the cluster, refresh
   the journal's ``last_status``.
2. Detect newly-complete waves (cross-reference the per-task status against
   the sidecar's ``wave_map``). For each, invoke ``combine_wave(...)``;
   on first failure, retry with ``force=True``; beyond that, mark as
   escalated and stop combining.
3. Append one tick record to ``.hpc/runs/<run_id>.monitor.jsonl`` —
   same schema as the slash-command ``/monitor-hpc`` writes, so the
   summary mode reads both.
4. Check for terminal conditions:
   - All tasks complete → ``mark_terminal(complete)``, return.
   - Failures and no work left → return ``failed`` with an escalation
     reason (MVP does not auto-resubmit; the slash-command surface or
     the next workflow atom decides what to do).
5. Sleep ``poll_interval_seconds``, repeat.

Wall-clock budget bounds the loop: when exceeded, return ``timeout``
without marking the run terminal — cluster jobs continue running and
the caller may re-invoke to keep watching.

What it intentionally does NOT do (in MVP)
------------------------------------------
- Auto-resubmit failed tasks. The slash-command ``/monitor-hpc`` does
  this with category-driven resource overrides; folding that into
  monitor-flow requires a backend abstraction parallel to submit-flow's,
  plus the failure-classification policy. Tracked separately.
- Decision logic about whether a run is "stalled" — this is judgment.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.clusters import resolve_ssh_target
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.announce import read_announcements
from hpc_agent.ops.monitor.classify import unresolved_unknown
from hpc_agent.ops.monitor.harvest_guard import _circuit_wait_sec, harvest_on_terminal
from hpc_agent.ops.monitor.reconcile import mark_terminal
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.monitor.terminal import (
    _ingest_runtime_at_terminal,
    _is_terminal,
)
from hpc_agent.ops.monitor.tick_log import _append_tick, _status_fingerprint
from hpc_agent.ops.monitor.waves import _newly_complete_waves, _read_partial_ok
from hpc_agent.ops.resolve_and_recover_flow import maybe_resolve_and_recover
from hpc_agent.state.journal import is_kill_confirmed, load_run, update_run_status
from hpc_agent.state.runs import read_run_sidecar

__all__ = ["monitor_flow", "MonitorFlowResult"]


@dataclass(frozen=True)
class MonitorFlowResult:
    """Return shape of :func:`monitor_flow`."""

    run_id: str
    lifecycle_state: str  # one of: complete, failed, abandoned, timeout
    last_status: dict[str, Any]
    combined_waves: list[int]
    failed_waves: list[int]
    ticks: int
    elapsed_seconds: float
    escalation_reason: str | None = None

    def to_envelope_data(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "lifecycle_state": self.lifecycle_state,
            "last_status": dict(self.last_status),
            "combined_waves": list(self.combined_waves),
            "failed_waves": list(self.failed_waves),
            "ticks": self.ticks,
            "elapsed_seconds": self.elapsed_seconds,
            "escalation_reason": self.escalation_reason,
        }


#: Sentinel value used in ``_LoopState.combiner_attempts`` to mark a
#: wave as permanently given-up after ``combiner_max_retries`` failures.
#: Any value much larger than ``combiner_max_retries`` works; ``10**9``
#: is large enough to be unreachable in practice without ambiguity.
_COMBINER_GIVE_UP_SENTINEL: int = 10**9


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to *default* on unset/invalid.

    Mirrors :func:`hpc_agent.infra.remote._env_int`'s fail-safe contract
    (a typo must not silently disable the floor) but accepts fractional
    seconds. Negative values fall back too — a poll floor can't be < 0.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


#: Minimum seconds between status polls — the connection-pacing floor
#: (#3). Mirrors AiiDA's ``minimum_job_poll_interval``: a hard lower
#: bound applied to the caller's ``poll_interval_seconds`` so no spec /
#: campaign can poll faster than this and re-trigger the connection
#: storm. Env-tunable via ``HPC_STATUS_POLL_INTERVAL_SEC`` (default 10s).
#: A spec asking for a *larger* interval is honored as-is.
_MIN_POLL_INTERVAL_SECONDS: float = _env_float("HPC_STATUS_POLL_INTERVAL_SEC", 10.0)

#: Upper bound (seconds) on the adaptive poll sleep. Hot-path cost is
#: dominated by the per-poll SSH + remote-status round-trip (~0.5-1s).
#: After K consecutive unchanged polls we double the effective interval
#: up to this cap (5 minutes), reverting instantly on any state change.
#: Env-tunable via ``HPC_STATUS_POLL_MAX_SEC`` (default 300s).
_MAX_ADAPTIVE_POLL_SECONDS: float = _env_float("HPC_STATUS_POLL_MAX_SEC", 300.0)

#: Number of consecutive unchanged polls before the adaptive backoff
#: starts doubling the effective sleep. Small (2) so a long-running but
#: chatty job barely backs off, while a truly idle 4h job ramps up
#: quickly: 60s → 120 → 240 → 300 (cap) within ~10 minutes of quiet.
_UNCHANGED_POLLS_BEFORE_BACKOFF: int = 2

#: Consecutive DETERMINISTIC broken-env poll failures (reporter rc 126/127:
#: wrong/absent conda env, or ``hpc_agent`` unimportable on the login node) that
#: escalate the monitor to a LOUD reporter-unreachable TIMEOUT instead of riding
#: the full wall-clock budget. Such a fault repeats identically every poll and
#: never heals by waiting (run #7: the main-array watch rode 28+ ticks of rc=127
#: while a finished array sat unread). Mirrors verify_canary's constant.
_DETERMINISTIC_ENV_POLLS_TO_FAIL: int = 3


# The rc-126/127 broken-env split is owned by ONE definition in
# ``hpc_agent.errors`` (G7) — ``verify_canary`` consumes the same one, so the
# twin that used to live here and drift per fix is retired.
_is_deterministic_env_failure = errors.is_deterministic_env_failure


def _floor_poll_interval(requested: float) -> float:
    """Apply the connection-pacing floor to a requested poll interval.

    Returns ``max(requested, _MIN_POLL_INTERVAL_SECONDS)`` — the caller's
    interval is honored unless it's faster than the floor, in which case
    the floor wins. Also clamps the floor itself below the adaptive cap so
    a mis-set ``HPC_STATUS_POLL_INTERVAL_SEC`` > ``HPC_STATUS_POLL_MAX_SEC``
    can't make the floor exceed the ceiling.
    """
    floor = min(_MIN_POLL_INTERVAL_SECONDS, _MAX_ADAPTIVE_POLL_SECONDS)
    return max(float(requested), floor)


def _announce_status(experiment_dir: Path, run_id: str, *, record: Any) -> Any | None:
    """Announce-first status for an announce-era run, or ``None`` to use the walk.

    Crash-only-monitoring **Phase 2** (``docs/design/crash-only-monitoring.md``).
    Reads the dispatcher's per-task terminal markers in ONE bounded ssh exec (one
    ``ls``, NO per-task reporter walk) and, when the announce dir EXISTS (an
    announce-era run whose dispatcher has written ≥1 marker), persists the
    canonical 5-key ``last_status`` derived from that census and returns the
    refreshed record. The whole run lifecycle is then driven off the census — the
    cluster ANNOUNCES state, the client stops PULLING it over a long-silent
    channel that a NAT/reaper severs (run-12 findings 20/24: a 20-25 min reporter
    walk severed mid-flight left a finished run unverifiable).

    Returns ``None`` — the caller falls back to the reporter walk (DISCLOSED) —
    when the announce dir does not exist yet (a pre-announce run, or a run no task
    has finished; the dispatcher creates the dir lazily on the FIRST marker) OR on
    any ssh error. The fallback need decays as pre-announce runs age out.

    A not-yet-terminal task shows up as ``missing`` in the census and is mapped to
    ``pending`` in the 5-key summary, so the shared terminal classifier
    (:func:`classify.classify_polling`) reads a PARTIAL census as still-in-flight
    — never as "no work left" (a partial census must never settle terminal,
    mirroring reconcile's Phase-1 arm). The raw census rides along under
    ``task_announcements`` for precise progress, and ``status_source`` /
    ``verdict_source`` mark the evidence provenance for every downstream reader.
    """
    try:
        census = read_announcements(
            ssh_target=resolve_ssh_target(record),
            remote_path=record.remote_path,
            run_id=run_id,
            task_count=record.total_tasks,
        )
    except Exception:  # noqa: BLE001 — census is best-effort; fall through to the walk
        return None
    if not census.get("present"):
        return None
    complete = int(census["complete"])
    failed = int(census["failed"])
    missing = int(census["missing"])
    last_status: dict[str, Any] = {
        "complete": complete,
        "failed": failed,
        "running": 0,
        # A not-yet-terminal (unannounced) task is "pending" for the classifier
        # so a PARTIAL census stays in-flight (pending > 0 blocks both the
        # no-work-left failure arm and the unknown-escalation arm); it settles
        # terminal only when missing == 0 (every task announced).
        "pending": missing,
        "unknown": 0,
        "checked_at": utcnow_iso(),
        "verdict_source": "task_announcements",
        "status_source": "task_announcements",
        "task_announcements": {
            "announced": int(census["announced"]),
            "complete": complete,
            "failed": failed,
            "missing": missing,
        },
    }
    return update_run_status(experiment_dir, run_id, last_status=last_status)


def _stamp_watchdog(experiment_dir: Path, run_id: str, next_tick_seconds: float) -> None:
    """Stamp the §5 driver dead-man's-switch fields for this monitor poll.

    A thin re-point onto the ONE shared definition
    (:func:`hpc_agent.state.journal.stamp_watchdog_tick`) so the monitor poll
    loop and the canary poll loop (``ops.verify_canary``) cannot disagree on what
    a tick means — the finding-12 "two loops, two definitions" fix. The shared
    helper owns the now/deadline computation and the best-effort-and-loud posture
    (a stamp failure never breaks the poll loop but is warned); this wrapper only
    preserves the historical ``(experiment_dir, run_id, next_tick_seconds)``
    call shape the many call sites below use.
    """
    from hpc_agent.state.journal import stamp_watchdog_tick

    stamp_watchdog_tick(run_id, next_tick_seconds=next_tick_seconds, experiment_dir=experiment_dir)


# ``_status_fingerprint`` lives in
# :mod:`hpc_agent.ops.monitor.tick_log` alongside ``_append_tick`` and
# ``_tick_log_path``. It re-exports above so any code that reached in
# via ``monitor_flow._status_fingerprint`` keeps working.


@dataclass
class _LoopState:
    """Mutable per-call state accumulated across ticks."""

    ticks: int = 0
    last_summary: dict[str, Any] = field(default_factory=dict)
    last_combined_waves: list[int] = field(default_factory=list)
    last_failed_waves: list[int] = field(default_factory=list)
    combiner_attempts: dict[int, int] = field(default_factory=dict)
    #: Consecutive polls for which ``classify.unresolved_unknown`` held —
    #: fed to the classifier's bounded-unknown escalation arm (finding f).
    unknown_streak: int = 0


# ``_tick_log_path`` and ``_append_tick`` live in
# :mod:`hpc_agent.ops.monitor.tick_log`.


# ``_newly_complete_waves``, ``_read_partial_ok`` and
# ``_write_failed_task_ids`` live in
# :mod:`hpc_agent.ops.monitor.waves`. They re-export above so the
# legacy ``monitor_flow.<helper>`` attribute path keeps working.


# ``_ingest_runtime_at_terminal`` and ``_is_terminal`` live in
# :mod:`hpc_agent.ops.monitor.terminal`. They re-export above so any
# code that reached in via ``monitor_flow._is_terminal`` keeps working.


@primitive(
    name="monitor-flow",
    verb="workflow",
    composes=["poll-run-status", "mark-run-terminal"],
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect(
            "writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (refreshes last_status)"
        ),
    ],
    error_codes=[
        errors.SshUnreachable,
        errors.JournalCorrupt,
        errors.PreconditionFailed,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
    cli=CliShape(
        help=(
            "Workflow atom: poll a run to terminal lifecycle (or wall-clock "
            "budget); auto-combine waves as they finish; write the same "
            ".monitor.jsonl tick log /monitor-hpc writes. Pairs with "
            "submit-flow for the campaign loop composition. MVP does not "
            "auto-resubmit failed tasks."
        ),
        spec_arg=True,
        spec_model=MonitorFlowSpec,
        schema_ref=SchemaRef(input="monitor_flow"),
        experiment_dir_arg=True,
        requires_ssh=True,
        dry_run_arg=True,
        dry_run_passthrough_keys=(
            "run_id",
            "poll_interval_seconds",
            "wall_clock_budget_seconds",
            "auto_combine_waves",
        ),
    ),
    agent_facing=True,
)
def monitor_flow(
    experiment_dir: Path,
    *,
    spec: MonitorFlowSpec,
    _sleep: Any = time.sleep,
    _now: Any = time.monotonic,
) -> MonitorFlowResult:
    """Poll ``spec.run_id`` to terminal-or-budget; auto-combine waves; emit one result.

    Idempotent in the sense that re-invoking after a terminal return is
    a no-op (the journal record already carries the terminal state and
    each poll is itself idempotent).

    Parameters ``_sleep`` and ``_now`` are injected for testability;
    production callers leave them at the defaults.
    """
    # Destructure the spec into typed locals so the body reads naturally
    # and mypy/IDE see each field's narrowed type. The spec itself is
    # the wire-validated authoring SoT (schemas/monitor_flow.input.json
    # is regenerated from MonitorFlowSpec).
    run_id = spec.run_id
    # Apply the connection-pacing floor (#3): the spec's poll_interval is a
    # request, but HPC_STATUS_POLL_INTERVAL_SEC (default 10s, AiiDA-style
    # minimum_job_poll_interval) is a hard lower bound so no spec/campaign
    # can poll faster than the floor and re-trigger the connection storm.
    poll_interval_seconds = _floor_poll_interval(spec.poll_interval_seconds)
    wall_clock_budget_seconds = spec.wall_clock_budget_seconds
    auto_combine_waves = spec.auto_combine_waves
    combiner_max_retries = spec.combiner_max_retries
    file_glob = spec.file_glob

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for {run_id!r}; cannot monitor an unknown run"
        )

    # Precondition gate: a run with no scheduler job ids never reached
    # the cluster (orphan sidecar, or submit-flow aborted before qsub).
    # Polling it would loop to the wall-clock budget against nothing —
    # fail loud instead of proceeding on a stale assumption.
    if not record.job_ids:
        raise errors.PreconditionFailed(
            f"run {run_id!r} has no scheduler job ids on its journal record; "
            "submit-flow has not run through to qsub (or it left an orphan "
            "sidecar). There is nothing to monitor."
        )

    # Read the per-run sidecar (under <experiment_dir>/.hpc/runs/, not the
    # journal dir). ``read_run_sidecar`` guarantees ``wave_map`` is a dict.
    wave_map: dict[str, list[int]] | None = None
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, errors.HpcError):
        # Missing or unreadable sidecar → auto_combine_waves is a no-op.
        sidecar = None
    if sidecar is not None:
        wm = sidecar.get("wave_map") or {}
        # Empty dict counts as "no wave_map" so auto_combine_waves below
        # short-circuits exactly as it did when the read silently failed.
        if isinstance(wm, dict) and wm:
            wave_map = wm

    state = _LoopState(
        last_combined_waves=list(record.combined_waves),
        last_failed_waves=list(record.failed_waves),
    )
    started = _now()

    # Adaptive backoff: the user-supplied poll_interval_seconds is the
    # floor; we double it (capped at _MAX_ADAPTIVE_POLL_SECONDS) after
    # _UNCHANGED_POLLS_BEFORE_BACKOFF consecutive polls whose status
    # fingerprint matched the prior poll. Any state change snaps the
    # effective interval back to the floor. For a 4h idle job this
    # cuts ~480 SSH polls to ~60.
    effective_interval = float(poll_interval_seconds)
    unchanged_count = 0
    last_fingerprint: str | None = None

    terminal_cause: str | None = None
    # Consecutive deterministic broken-env poll failures (reporter rc 126/127) —
    # escalates to a reporter-unreachable TIMEOUT at the threshold instead of
    # riding the whole budget silently (run #7).
    consecutive_env_polls = 0
    # Crash-only Phase 2: disclose the pull-path fallback ONCE per call (the
    # DISCLOSURE-OR-REFUSAL bar, docs/internals/fallback-inventory.md) the first
    # tick that falls to the reporter walk because no announcements are present.
    walk_fallback_disclosed = False
    try:
        while True:
            state.ticks += 1
            elapsed = _now() - started

            # --- Crash-only Phase 2: announce-first census ---------------------
            # Prefer the ONE-readdir marker census over the per-task reporter walk
            # for the whole lifecycle wherever announcements exist (the pull leg is
            # what NAT/reapers sever at terminal — run-12 findings 20/24). An
            # announce-era run resolves its status from the census with NO walk; a
            # pre-announce run (or one no task has announced yet) falls back to the
            # walk, DISCLOSED. Kill-confirmed runs are left to reconcile's
            # kill-arm, and pure-API backends have no announce dir → straight to
            # the backend-API poll inside record_status.
            announced_record = None
            if backend_requires_ssh(record.backend) and not is_kill_confirmed(record):
                announced_record = _announce_status(experiment_dir, run_id, record=record)
            if announced_record is not None:
                # Census leg: the run's status was resolved from the ONE-readdir
                # marker census — NO per-task reporter walk. ``_announce_status``
                # already persisted ``last_status`` (source-marked).
                record = announced_record
                last_status = dict(record.last_status or {})
                # Progress legibility, census leg — same shape as the walk leg's
                # per-poll line, but sourced from the one-readdir marker count.
                logging.getLogger(__name__).info(
                    "monitor: run %s tick %d — %s/%s announced complete "
                    "(failed %s, awaiting %s) via task announcements (no walk)",
                    run_id,
                    state.ticks,
                    last_status.get("complete", 0),
                    record.total_tasks,
                    last_status.get("failed", 0),
                    last_status.get("pending", 0),
                )
            else:
                # Pull leg (fallback): no announcements present — a pre-announce
                # run, or a run no task has announced a terminal state for yet.
                # DISCLOSE the degrade to the reporter walk ONCE per call
                # (DISCLOSURE-OR-REFUSAL bar; the announce census takes over as
                # soon as the dispatcher writes its first marker). Pure-API
                # backends don't announce — no disclosure, record_status polls
                # the backend API directly.
                if backend_requires_ssh(record.backend) and not walk_fallback_disclosed:
                    walk_fallback_disclosed = True
                    logging.getLogger(__name__).info(
                        "monitor: run %s — no task announcements present; using the "
                        "status-reporter walk (a pre-announce run, or no task has "
                        "announced a terminal state yet). The cheap one-readdir "
                        "announce census takes over once the dispatcher writes markers.",
                        run_id,
                    )
                # Poll. A single transient poll fault (a reporter rc!=0 →
                # RemoteCommandFailed, or a TimeoutError — an OSError subclass —
                # after the backoff window) must NOT abort a healthy multi-hour poll
                # and kill the detached child; only the outer try/finally would catch
                # it and re-raise. Mirror the sibling canary loop
                # (ops/verify_canary.py): swallow the transient fault, note it on a
                # tick, and continue to the next poll. The loop stays bounded by the
                # wall-clock budget below, so a poller that keeps failing past the
                # budget still terminates to TIMEOUT with the guaranteed harvest.
                try:
                    record = record_status(
                        experiment_dir,
                        run_id,
                        ssh_target=resolve_ssh_target(record),
                        remote_path=record.remote_path,
                        job_ids=list(record.job_ids),
                        job_name=record.job_name,
                        file_glob=file_glob,
                    )
                except (errors.RemoteCommandFailed, OSError) as exc:
                    # Classify: a DETERMINISTIC broken-env fault (reporter rc 126/127)
                    # fails EVERY poll identically and never heals by waiting, so
                    # escalate FAST rather than ride the whole budget silently (run #7:
                    # the main watch rode 28+ ticks of rc=127 while a finished array sat
                    # unread). A transient fault resets the count and still rides the
                    # budget → TIMEOUT with the guaranteed harvest (the tolerance below).
                    if _is_deterministic_env_failure(exc):
                        consecutive_env_polls += 1
                    else:
                        consecutive_env_polls = 0
                    env_broken = consecutive_env_polls >= _DETERMINISTIC_ENV_POLLS_TO_FAIL
                    logging.getLogger(__name__).warning(
                        "monitor_flow: %s poll failure for run %s (tick %d): %s — %s",
                        "deterministic-env" if _is_deterministic_env_failure(exc) else "transient",
                        run_id,
                        state.ticks,
                        exc,
                        (
                            f"reporter UNREACHABLE after {consecutive_env_polls} "
                            "consecutive env failures — escalating"
                            if env_broken
                            else "continuing to the next poll"
                        ),
                    )
                    # Re-derive the budget so repeated transient failures still
                    # terminate (rather than spin forever skipping the check below).
                    over_budget = (_now() - started) >= wall_clock_budget_seconds
                    terminate = over_budget or env_broken
                    _append_tick(
                        experiment_dir,
                        run_id,
                        summary=dict(record.last_status or {}),
                        diff_from_prev={
                            "newly_complete": [],
                            "newly_failed": [],
                            "newly_combined_waves": [],
                        },
                        actions=[{"kind": "poll_error", "error": str(exc)}],
                        lifecycle_state=LifecycleState.TIMEOUT if terminate else "in_flight",
                        next_tick_seconds=None if terminate else effective_interval,
                    )
                    if terminate:
                        _ingest_runtime_at_terminal(experiment_dir, record=record)
                        terminal_cause = "reporter-unreachable" if env_broken else "cap-overrun"
                        escalation = (
                            (
                                f"status reporter UNREACHABLE — {consecutive_env_polls} "
                                "consecutive deterministic failures (rc 126/127: wrong/absent "
                                "conda env, or hpc_agent not importable on the login node). The "
                                "array may be running or already complete, but its status is "
                                "UNREADABLE; fix the cluster env then re-watch, or harvest "
                                f"results directly. Last poll error: {exc}"
                            )
                            if env_broken
                            else None
                        )
                        return MonitorFlowResult(
                            run_id=run_id,
                            lifecycle_state=LifecycleState.TIMEOUT,
                            last_status=dict(record.last_status or {}),
                            combined_waves=state.last_combined_waves,
                            failed_waves=state.last_failed_waves,
                            ticks=state.ticks,
                            elapsed_seconds=_now() - started,
                            escalation_reason=escalation,
                        )
                    # Live poller, transient blip: re-stamp the watchdog so a genuinely
                    # dead poller is still doctor-visible, then back off and retry.
                    _stamp_watchdog(experiment_dir, run_id, effective_interval)
                    _sleep(effective_interval)
                    continue
                except (
                    errors.SshCircuitOpen,
                    errors.SshUnreachable,
                    errors.SshSlotWaitTimeout,
                ) as exc:
                    # A NETWORK fault, not a RemoteCommandFailed/OSError: the per-host
                    # SSH circuit breaker opened (SshCircuitOpen), the host went
                    # transiently unreachable, or our own per-host slot wait timed out.
                    # None is an HpcError caught above, so without this clause a single
                    # blip that opens the breaker would kill the detached multi-hour
                    # watch (run #7: a 3×60s hoffman2 latency spike). Classify it
                    # transient: reset the deterministic-env counter, tick a poll_error,
                    # re-check the wall-clock budget, and — for a breaker-open — sleep
                    # out the remaining cooldown (never below the floor) before the next
                    # poll rather than hammering an open circuit (retry_safe=False). The
                    # loop stays bounded by the budget → TIMEOUT with the guaranteed
                    # harvest if the fault never clears. Cooldown wait mirrors
                    # harvest_guard._circuit_wait_sec.
                    consecutive_env_polls = 0
                    over_budget = (_now() - started) >= wall_clock_budget_seconds
                    logging.getLogger(__name__).warning(
                        "monitor_flow: transient ssh fault for run %s (tick %d): %s — %s",
                        run_id,
                        state.ticks,
                        exc,
                        (
                            "over budget — escalating to TIMEOUT"
                            if over_budget
                            else "waiting out the breaker cooldown, then retrying"
                        ),
                    )
                    _append_tick(
                        experiment_dir,
                        run_id,
                        summary=dict(record.last_status or {}),
                        diff_from_prev={
                            "newly_complete": [],
                            "newly_failed": [],
                            "newly_combined_waves": [],
                        },
                        actions=[{"kind": "poll_error", "error": str(exc)}],
                        lifecycle_state=LifecycleState.TIMEOUT if over_budget else "in_flight",
                        next_tick_seconds=None if over_budget else effective_interval,
                    )
                    if over_budget:
                        _ingest_runtime_at_terminal(experiment_dir, record=record)
                        terminal_cause = "cap-overrun"
                        return MonitorFlowResult(
                            run_id=run_id,
                            lifecycle_state=LifecycleState.TIMEOUT,
                            last_status=dict(record.last_status or {}),
                            combined_waves=state.last_combined_waves,
                            failed_waves=state.last_failed_waves,
                            ticks=state.ticks,
                            elapsed_seconds=_now() - started,
                            escalation_reason=None,
                        )
                    # Wait out the breaker cooldown before the next poll, floored at
                    # effective_interval. SshUnreachable / SshSlotWaitTimeout carry no
                    # deadline → _circuit_wait_sec returns None → the floor interval.
                    circuit_wait = (
                        _circuit_wait_sec(exc, now=time.time())
                        if isinstance(exc, errors.SshCircuitOpen)
                        else None
                    )
                    sleep_for = (
                        max(effective_interval, circuit_wait)
                        if circuit_wait is not None
                        else effective_interval
                    )
                    _stamp_watchdog(experiment_dir, run_id, sleep_for)
                    _sleep(sleep_for)
                    continue
                last_status = dict(record.last_status or {})
                # In-band fallback disclosure: mark that this tick's counts came
                # from the reporter walk, not the announce census (the tick log
                # is the per-poll audit trail). A pure-API poll is not a walk
                # fallback, so only tag ssh reporter walks.
                if backend_requires_ssh(record.backend):
                    last_status.setdefault("status_source", "status_reporter_walk")
                # Progress legibility (run #7: a silent healthy watch reads as "stuck"
                # and triggered false stall alarms) — one concise per-poll line.
                logging.getLogger(__name__).info(
                    "monitor: run %s tick %d — %s/%s complete (running %s, pending %s, failed %s)",
                    run_id,
                    state.ticks,
                    last_status.get("complete", 0),
                    record.total_tasks,
                    last_status.get("running", 0),
                    last_status.get("pending", 0),
                    last_status.get("failed", 0),
                )

            # Compute diff against the prior tick (for the tick log).
            prev_summary = state.last_summary
            diff: dict[str, list[int]] = {
                "newly_complete": [],
                "newly_failed": [],
                "newly_combined_waves": [],
            }
            # Tick 1 has no prior tick to diff against — leave the deltas
            # empty rather than reporting the whole baseline count as a
            # single-tick delta.
            if state.ticks > 1:
                for key in ("complete", "failed"):
                    cur = int(last_status.get(key, 0))
                    prv = int(prev_summary.get(key, 0))
                    if cur > prv:
                        diff[f"newly_{key}"] = [cur - prv]  # delta count, not task IDs
            state.last_summary = last_status

            actions: list[dict[str, Any]] = []

            # Combine newly-complete waves.
            if auto_combine_waves and wave_map:
                newly_done = _newly_complete_waves(
                    last_status=last_status,
                    wave_map=wave_map,
                    already_combined=set(state.last_combined_waves),
                )
                for wave in newly_done:
                    # Skip waves already escalated past combiner_max_retries
                    # (sentinel = 10**9). Without this, every tick would
                    # re-call combine_wave on a permanently failed
                    # wave, wasting SSH round-trips indefinitely.
                    if state.combiner_attempts.get(wave, 0) >= _COMBINER_GIVE_UP_SENTINEL:
                        continue
                    attempt = state.combiner_attempts.get(wave, 0) + 1
                    state.combiner_attempts[wave] = attempt
                    ok, _stdout, stderr = combine_wave(
                        experiment_dir,
                        run_id,
                        wave=wave,
                        ssh_target=resolve_ssh_target(record),
                        remote_path=record.remote_path,
                        force=(attempt > 1),
                    )
                    if ok:
                        actions.append({"kind": "combine_wave", "wave": wave, "attempt": attempt})
                        state.last_combined_waves = sorted({*state.last_combined_waves, wave})
                        # A wave that previously failed and now succeeds on retry
                        # must drop off ``failed_waves`` — otherwise the returned
                        # MonitorFlowResult reports the wave in BOTH lists and a
                        # downstream consumer keying off the failure ledger
                        # (escalation surfaces, campaign-loop auto-resubmit) acts
                        # on a stale failure (v3 BUG-4V3-2).
                        state.last_failed_waves = sorted(set(state.last_failed_waves) - {wave})
                        diff["newly_combined_waves"].append(wave)
                    else:
                        actions.append(
                            {
                                "kind": "combine_wave_failed",
                                "wave": wave,
                                "attempt": attempt,
                                "stderr_tail": (stderr or "").strip()[-500:],
                            }
                        )
                        state.last_failed_waves = sorted({*state.last_failed_waves, wave})
                        if attempt > combiner_max_retries:
                            # Escalate: stop combining this wave but keep
                            # watching the rest of the run. The caller's
                            # envelope will surface failed_waves.
                            state.combiner_attempts[wave] = _COMBINER_GIVE_UP_SENTINEL

            # Bounded-unknown watchdog (proving run #3, finding f): a run whose
            # remote workdir vanished mid-run can poll "unknown" indefinitely —
            # nothing alive on the scheduler, no results on disk, no failure
            # evidence. Count consecutive such polls; the classifier's
            # bounded-unknown arm escalates to a terminal ``abandoned`` anomaly
            # once the streak reaches UNKNOWN_TICKS_BEFORE_ESCALATION, instead
            # of spinning to the wall-clock budget. Any tick with live work or
            # positive evidence resets the streak.
            if unresolved_unknown(last_status, int(record.total_tasks)):
                state.unknown_streak += 1
            else:
                state.unknown_streak = 0

            # Terminal check.
            terminal, esc_reason = _is_terminal(
                last_status,
                int(record.total_tasks),
                partial_ok=_read_partial_ok(experiment_dir, run_id),
                unknown_streak=state.unknown_streak,
            )
            if terminal == LifecycleState.COMPLETE:
                mark_terminal(experiment_dir, run_id, status=LifecycleState.COMPLETE)
                _append_tick(
                    experiment_dir,
                    run_id,
                    summary=last_status,
                    diff_from_prev=diff,
                    actions=actions,
                    lifecycle_state=LifecycleState.COMPLETE,
                    next_tick_seconds=None,
                )
                _ingest_runtime_at_terminal(experiment_dir, record=record)
                # If combine_wave exhausted retries on any wave mid-flight,
                # tasks completing afterward should not silence that
                # failure. Surface ``failed_waves`` via ``escalation_reason``
                # so callers branching on it (escalation surfaces, campaign
                # auto-resubmit) see the partial-wave failure even on a
                # COMPLETE return.
                complete_escalation: str | None = None
                if state.last_failed_waves:
                    complete_escalation = "combine_failed_waves:waves=" + ",".join(
                        str(w) for w in state.last_failed_waves
                    )
                terminal_cause = "complete"
                return MonitorFlowResult(
                    run_id=run_id,
                    lifecycle_state=LifecycleState.COMPLETE,
                    last_status=last_status,
                    combined_waves=state.last_combined_waves,
                    failed_waves=state.last_failed_waves,
                    ticks=state.ticks,
                    elapsed_seconds=elapsed,
                    escalation_reason=complete_escalation,
                )
            if terminal == LifecycleState.FAILED:
                # #294 Layer-2 auto-fire (#299): when the run opted into
                # auto-resume, consult the gate BEFORE surfacing FAILED. On a
                # "resume" verdict the preempted tasks are re-submitted from
                # checkpoint and the run is live again — reload the record (extended
                # job_ids + bumped count) and keep polling instead of marking
                # terminal. On "escalate" (opt-out, OOM/error, or cap reached) fall
                # through to the normal FAILED surface, enriching the reason so the
                # escalation-as-data path (#234) carries why auto-resume declined.
                if record.auto_resume_on_kill:
                    from hpc_agent.ops.auto_resume_flow import maybe_auto_resume

                    # The status reporter folds the fresh scheduler-side preempt
                    # signal (exit 130/143 / state PREEMPTED) into last_status, so
                    # pass it straight through — the composite then needs no second
                    # round-trip. Absent (older reporter / SGE without exit codes)
                    # → the composite falls back to a log-based fetch.
                    _preempted = last_status.get("preempted_task_ids")
                    outcome = maybe_auto_resume(
                        experiment_dir,
                        run_id,
                        record=record,
                        preempted_task_ids=_preempted if isinstance(_preempted, list) else None,
                    )
                    if outcome.action == "resume":
                        actions.append(
                            {
                                "kind": "auto_resume",
                                "task_ids": list(outcome.task_ids),
                                "resubmitted": outcome.resubmitted,
                                "auto_resume_count": outcome.auto_resume_count,
                            }
                        )
                        # Reset adaptive backoff — the run state just changed
                        # materially (a fresh array is queued) so the next poll
                        # should run at the floor, not a backed-off interval.
                        unchanged_count = 0
                        last_fingerprint = None
                        effective_interval = float(poll_interval_seconds)
                        _append_tick(
                            experiment_dir,
                            run_id,
                            summary=last_status,
                            diff_from_prev=diff,
                            actions=actions,
                            lifecycle_state="in_flight",
                            next_tick_seconds=effective_interval,
                        )
                        # §5 watchdog: stamp the next-poll deadline (as on the
                        # normal in-flight tick) so a dead poller is doctor-visible.
                        _stamp_watchdog(experiment_dir, run_id, effective_interval)
                        refreshed = load_run(experiment_dir, run_id)
                        if refreshed is not None:
                            record = refreshed
                        _sleep(effective_interval)
                        continue
                    esc_reason = outcome.reason

                # #240 live wiring of the #234 deterministic resolver — the
                # resolve-and-recover composite, mirrored on the auto-resume hook
                # above (#315 stacks this on that composite). Auto-resume owns
                # ``preempted`` clusters (and on a "resume" verdict ``continue``s the
                # loop before reaching here); this composite deliberately SKIPS
                # ``preempted`` (its ``_DETERMINISTIC`` set excludes it), so the two
                # never double-handle a cluster — they partition the FAILED tick:
                # preempted → auto-resume, everything else → resolve-and-recover.
                # Opt-in OFF by default (``auto_recover_on_failure``): a run that did
                # not opt in computes the verdict-as-data and takes NO side effect
                # (no resubmit, no park), so this wiring is behavior-neutral until a
                # run opts in.
                recover_outcome = maybe_resolve_and_recover(
                    experiment_dir,
                    run_id,
                    record=record,
                )
                if recover_outcome.clusters:
                    actions.append(
                        {
                            "kind": "resolve_and_recover",
                            "run_id": recover_outcome.run_id,
                            "clusters": [
                                {
                                    "fingerprint": c.fingerprint,
                                    "error_class": c.error_class,
                                    "task_ids": list(c.task_ids),
                                    "disposition": c.disposition,
                                    "decided_by": c.decided_by,
                                    "reason": c.reason,
                                }
                                for c in recover_outcome.clusters
                            ],
                            "auto_recover_count": recover_outcome.auto_recover_count,
                        }
                    )
                # When the composite actually resubmitted a cluster (opt-in ON +
                # code verdict under cap) the run is live again — reload the record
                # (extended job_ids + bumped count) and keep polling, exactly as the
                # auto-resume "resume" branch does, rather than surfacing FAILED.
                if recover_outcome.resubmitted:
                    unchanged_count = 0
                    last_fingerprint = None
                    effective_interval = float(poll_interval_seconds)
                    _append_tick(
                        experiment_dir,
                        run_id,
                        summary=last_status,
                        diff_from_prev=diff,
                        actions=actions,
                        lifecycle_state="in_flight",
                        next_tick_seconds=effective_interval,
                    )
                    # §5 watchdog: re-stamp the next-poll deadline (as the
                    # auto-resume branch above does) so a dead poller stays
                    # doctor-visible. A recover-resubmit grew the run, so the
                    # next poll can take longer — without this re-stamp the stale
                    # (pre-resubmit) next_tick_due can false-positive a stall on a
                    # live poller.
                    _stamp_watchdog(experiment_dir, run_id, effective_interval)
                    refreshed = load_run(experiment_dir, run_id)
                    if refreshed is not None:
                        record = refreshed
                    _sleep(effective_interval)
                    continue
                # Otherwise (held / verdict-only / nothing resolvable) fall through
                # to the FAILED surface. Enrich the escalation reason the same way
                # auto-resume does, so the escalation-as-data path (#234) carries
                # why the held clusters were parked.
                if recover_outcome.held:
                    held_reason = "; ".join(
                        f"{c.error_class or c.fingerprint}: {c.reason}"
                        for c in recover_outcome.held
                    )
                    esc_reason = (
                        f"{esc_reason}; auto_recover_held: {held_reason}"
                        if esc_reason
                        else f"auto_recover_held: {held_reason}"
                    )
                mark_terminal(experiment_dir, run_id, status=LifecycleState.FAILED)
                _append_tick(
                    experiment_dir,
                    run_id,
                    summary=last_status,
                    diff_from_prev=diff,
                    actions=actions,
                    lifecycle_state=LifecycleState.FAILED,
                    next_tick_seconds=None,
                )
                _ingest_runtime_at_terminal(experiment_dir, record=record)
                terminal_cause = "failed"
                return MonitorFlowResult(
                    run_id=run_id,
                    lifecycle_state=LifecycleState.FAILED,
                    last_status=last_status,
                    combined_waves=state.last_combined_waves,
                    failed_waves=state.last_failed_waves,
                    ticks=state.ticks,
                    elapsed_seconds=elapsed,
                    escalation_reason=esc_reason,
                )

            # Any other terminal verdict. ``classify_polling`` emits exactly one
            # today: the bounded-unknown escalation to ``abandoned`` (finding f —
            # unknown_streak reached UNKNOWN_TICKS_BEFORE_ESCALATION with no live
            # work and no evidence). It flows through the SAME mark-terminal +
            # guaranteed-harvest path as complete/failed (design §5 gap (a):
            # abandoned must not skip the harvest) instead of silently falling
            # through to the budget check and polling a dead run to timeout; the
            # anomaly provenance rides out on ``escalation_reason``.
            if terminal is not None:
                mark_terminal(experiment_dir, run_id, status=terminal)
                _append_tick(
                    experiment_dir,
                    run_id,
                    summary=last_status,
                    diff_from_prev=diff,
                    actions=actions,
                    lifecycle_state=terminal,
                    next_tick_seconds=None,
                )
                _ingest_runtime_at_terminal(experiment_dir, record=record)
                terminal_cause = terminal
                return MonitorFlowResult(
                    run_id=run_id,
                    lifecycle_state=terminal,
                    last_status=last_status,
                    combined_waves=state.last_combined_waves,
                    failed_waves=state.last_failed_waves,
                    ticks=state.ticks,
                    elapsed_seconds=elapsed,
                    escalation_reason=esc_reason,
                )

            # Budget check.
            if elapsed >= wall_clock_budget_seconds:
                _append_tick(
                    experiment_dir,
                    run_id,
                    summary=last_status,
                    diff_from_prev=diff,
                    actions=actions,
                    lifecycle_state=LifecycleState.TIMEOUT,
                    next_tick_seconds=None,
                )
                _ingest_runtime_at_terminal(experiment_dir, record=record)
                terminal_cause = "cap-overrun"
                return MonitorFlowResult(
                    run_id=run_id,
                    lifecycle_state=LifecycleState.TIMEOUT,
                    last_status=last_status,
                    combined_waves=state.last_combined_waves,
                    failed_waves=state.last_failed_waves,
                    ticks=state.ticks,
                    elapsed_seconds=elapsed,
                    escalation_reason=None,
                )

            # Still in flight; update adaptive backoff and record the tick.
            # Fingerprint covers the entire status snapshot (counts, scheduler
            # state, waves block) so any change snaps us back to the floor.
            fingerprint = _status_fingerprint(last_status)
            if last_fingerprint is not None and fingerprint == last_fingerprint:
                unchanged_count += 1
                if unchanged_count >= _UNCHANGED_POLLS_BEFORE_BACKOFF:
                    effective_interval = min(effective_interval * 2.0, _MAX_ADAPTIVE_POLL_SECONDS)
            else:
                unchanged_count = 0
                effective_interval = float(poll_interval_seconds)
            last_fingerprint = fingerprint

            _append_tick(
                experiment_dir,
                run_id,
                summary=last_status,
                diff_from_prev=diff,
                actions=actions,
                lifecycle_state="in_flight",
                next_tick_seconds=effective_interval,
            )
            # §5 watchdog: stamp the deadline for the NEXT poll so a dead poller
            # (incl. a detached S3 child) is caught by the doctor via a lapse.
            _stamp_watchdog(experiment_dir, run_id, effective_interval)
            _sleep(effective_interval)

    finally:
        # Guaranteed harvest (design §5): every terminal path AND any
        # abnormal exit (exception / session-death) lands here exactly once.
        # When a clean terminal branch was reached we harvest for that cause;
        # otherwise the loop is unwinding on an exception (or a break that
        # never set a cause) and we harvest under the abnormal-exit sentinel.
        # harvest_on_terminal never raises, so a live exception propagates
        # untouched (the harvest is additive, never a mask of the cause).
        with contextlib.suppress(Exception):
            harvest_on_terminal(
                experiment_dir,
                run_id,
                terminal_cause=terminal_cause or "abnormal-exit",
                record=record,
            )
