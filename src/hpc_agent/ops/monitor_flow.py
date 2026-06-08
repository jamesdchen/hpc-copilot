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

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.contract.vocabulary import LifecycleState
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.reconcile import mark_terminal
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.ops.monitor.terminal import (
    _ingest_runtime_at_terminal,
    _is_terminal,
)
from hpc_agent.ops.monitor.tick_log import _append_tick, _status_fingerprint
from hpc_agent.ops.monitor.waves import _newly_complete_waves, _read_partial_ok
from hpc_agent.state.journal import load_run
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

#: Upper bound (seconds) on the adaptive poll sleep. Hot-path cost is
#: dominated by the per-poll SSH + remote-status round-trip (~0.5-1s).
#: After K consecutive unchanged polls we double the effective interval
#: up to this cap (5 minutes), reverting instantly on any state change.
_MAX_ADAPTIVE_POLL_SECONDS: float = 300.0

#: Number of consecutive unchanged polls before the adaptive backoff
#: starts doubling the effective sleep. Small (2) so a long-running but
#: chatty job barely backs off, while a truly idle 4h job ramps up
#: quickly: 60s → 120 → 240 → 300 (cap) within ~10 minutes of quiet.
_UNCHANGED_POLLS_BEFORE_BACKOFF: int = 2


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
    poll_interval_seconds = spec.poll_interval_seconds
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
    except (FileNotFoundError, OSError, json.JSONDecodeError):
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

    while True:
        state.ticks += 1
        elapsed = _now() - started

        # Poll.
        record = record_status(
            experiment_dir,
            run_id,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_ids=list(record.job_ids),
            job_name=record.job_name,
            file_glob=file_glob,
        )
        last_status = dict(record.last_status or {})

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
                    ssh_target=record.ssh_target,
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

        # Terminal check.
        terminal, esc_reason = _is_terminal(
            last_status,
            int(record.total_tasks),
            partial_ok=_read_partial_ok(experiment_dir, run_id),
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
                    refreshed = load_run(experiment_dir, run_id)
                    if refreshed is not None:
                        record = refreshed
                    _sleep(effective_interval)
                    continue
                esc_reason = outcome.reason
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
        _sleep(effective_interval)
