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
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.lifecycle.lifecycle import LifecycleState
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.monitor_flow import MonitorFlowSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.aggregate.combine import combine_wave
from hpc_agent.ops.monitor.reconcile import mark_terminal
from hpc_agent.ops.monitor.status import record_status
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import runs_dir
from hpc_agent.state.runs import read_run_sidecar

from pathlib import Path

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


def _status_fingerprint(status: dict[str, Any]) -> str:
    """Return a stable hash of the polled status dict.

    Any change in task counts, scheduler-state flips, new waves, etc.
    flips the fingerprint and resets the adaptive backoff. We serialize
    with ``sort_keys=True`` and ``default=str`` so heterogeneous (and
    nested-dict) values like the ``waves`` block hash deterministically
    without us having to enumerate which keys matter. blake2b is fast
    and collision-resistant enough for an equality oracle.
    """
    try:
        payload = json.dumps(status, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        # Pathological payload — fall back to a per-call unique value so
        # we never spuriously declare "unchanged" on an opaque diff.
        payload = repr(status).encode("utf-8", errors="replace")
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


@dataclass
class _LoopState:
    """Mutable per-call state accumulated across ticks."""

    ticks: int = 0
    last_summary: dict[str, Any] = field(default_factory=dict)
    last_combined_waves: list[int] = field(default_factory=list)
    last_failed_waves: list[int] = field(default_factory=list)
    combiner_attempts: dict[int, int] = field(default_factory=dict)


def _tick_log_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the path the slash-command surface writes its tick log to.

    Sharing the file across surfaces lets ``/monitor-hpc summary`` work
    regardless of whether monitoring was driven by repeated slash-command
    invocations or by one long monitor-flow call.
    """
    return runs_dir(experiment_dir) / f"{run_id}.monitor.jsonl"


# _flock_append was removed in favour of routing the tick-log append
# through hpc_agent._kernel.extension.telemetry's monitor-jsonl sink, which owns
# the flock-guarded writer pattern (see _append_tick below).


def _append_tick(
    experiment_dir: Path,
    run_id: str,
    *,
    summary: dict[str, Any],
    diff_from_prev: dict[str, list[int]],
    actions: list[dict[str, Any]],
    lifecycle_state: str,
    next_tick_seconds: float | None,
) -> None:
    """Append one JSONL record to ``<run_id>.monitor.jsonl`` (best-effort).

    Holds an exclusive flock for the duration of the append so a
    concurrent slash-command writer can\'t interleave bytes mid-line.
    """
    record = {
        "tick_id": utcnow_iso(),
        "run_id": run_id,
        "summary": summary,
        "diff_from_prev": diff_from_prev,
        "preflight": "ok",
        "actions": actions,
        "lifecycle_state": lifecycle_state,
        "next_tick_seconds": next_tick_seconds,
        "console_emitted": False,
    }
    path = _tick_log_path(experiment_dir, run_id)
    # B7: Route the JSONL append through hpc_agent._kernel.extension.telemetry,
    # which owns the flock-guarded writer pattern. Telemetry's
    # monitor-jsonl sink ignores HPC_TELEMETRY_SINK because this caller
    # is the canonical producer.
    try:
        from hpc_agent._kernel.extension.telemetry import record as _telemetry_record

        _telemetry_record(
            "tick",
            record,
            sink="monitor-jsonl",
            monitor_jsonl_path=path,
        )
    except Exception:  # noqa: BLE001 — never crash the loop on telemetry
        # Tick log writes must never crash the loop. The journal record
        # is the primary state; this is observability.
        pass


def _newly_complete_waves(
    *,
    last_status: dict[str, Any],
    wave_map: dict[str, list[int]] | None,
    already_combined: set[int],
) -> list[int]:
    """Identify waves whose every task reports complete and aren't yet combined.

    The cluster-side reporter optionally emits a ``waves`` block in
    ``last_status`` when the sidecar carried a ``wave_map``. We trust
    that: when ``waves[N].complete == waves[N].total``, wave ``N`` is
    done. Falls back to "no wave_map → no combining" silently.
    """
    waves_block = last_status.get("waves")
    if not isinstance(waves_block, dict):
        return []
    # Restrict to waves the local wave_map declared so a cluster-side
    # reporter that picks up unexpected wave numbers (e.g. from a stale
    # status report, or after a fresh resubmission added new groups) can't
    # trigger combine_wave on waves the framework doesn't track.
    declared_waves: set[int] | None = None
    if wave_map is not None:
        declared_waves = set()
        for k in wave_map:
            try:
                declared_waves.add(int(k))
            except (TypeError, ValueError):
                continue
    out: list[int] = []
    for k, counts in waves_block.items():
        try:
            wave_num = int(k)
        except (TypeError, ValueError):
            continue
        if wave_num in already_combined:
            continue
        if declared_waves is not None and wave_num not in declared_waves:
            continue
        if not isinstance(counts, dict):
            continue
        # Coerce to int explicitly so a missing/None counter doesn't
        # falsy-skip a legitimate (e.g. total=5, complete=5) match, and
        # require total > 0 explicitly so empty waves don't loop until
        # walltime budget.
        try:
            total = int(counts.get("total") or 0)
            complete = int(counts.get("complete") or 0)
        except (TypeError, ValueError):
            continue
        if total > 0 and complete == total:
            out.append(wave_num)
    return sorted(out)


def _read_partial_ok(experiment_dir: Path, run_id: str) -> bool:
    """Read the partial_ok sibling marker written by submit-flow.

    Returns True iff ``<exp>/.hpc/runs/<run_id>.partial_ok`` exists.
    The marker is a sibling of the run sidecar (intentionally not a
    sidecar field) so the sidecar's frozen schema does not need to bump
    for this opt-in flag. See ``submit_flow.partial_ok``.
    """
    from hpc_agent.state.runs import run_sidecar_path

    marker = run_sidecar_path(experiment_dir, run_id).with_suffix(".partial_ok")
    return marker.is_file()


def _write_failed_task_ids(
    experiment_dir: Path,
    run_id: str,
    *,
    failed_task_ids: list[int],
    classifier_codes: list[str] | None = None,
    wave: int | None = None,
) -> None:
    """Persist the failure ledger consulted by aggregate-flow.

    Writes ``<exp>/.hpc/runs/<run_id>.failed.json`` with the shape
    documented in the D2b primitive doc — kept on disk (not in the
    sidecar) so aggregate-flow can read it without a sidecar parse.

    Routed through :func:`atomic_write_json` so a concurrent reader
    (aggregate-flow scanning the ledger) never observes a partial JSON
    write — without this, a Python-level ``write_text`` produces a
    truncate-then-write sequence that can land mid-payload.
    """
    from hpc_agent.infra.io import atomic_write_json
    from hpc_agent.state.runs import run_sidecar_path

    target = run_sidecar_path(experiment_dir, run_id).with_suffix(".failed.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "failed_task_ids": sorted(set(int(t) for t in failed_task_ids)),
        "wave": wave,
        "classifier_codes": list(classifier_codes or []),
    }
    atomic_write_json(target, payload)


def _ingest_runtime_at_terminal(experiment_dir: Path, *, record: Any) -> int:
    """Pull `_combiner/wave_*.runtime.json` from the cluster and ingest.

    The runtime-prior pipeline normally runs from `aggregate_flow`. This
    hook lets `monitor_flow` feed the warm-axis-picker even when the
    user never invokes `/aggregate-hpc` (e.g. they read metrics on the
    cluster directly, or only care about pass/fail). Best-effort:
    failures are swallowed — monitor's job is lifecycle, not priors.

    Pull is filtered to just the runtime sidecars (~1 file per wave,
    typically <100KB total) — cheap relative to a full `_combiner/`
    pull. Idempotent: re-running on the same run is safe because
    `append_sample` dedups on `(run_id, task_id)`.

    The pull lands under a :class:`tempfile.TemporaryDirectory` so a
    long-running monitor that ticks N runs to terminal does not leak N
    ``hpc_runtime_pull_*`` dirs under ``$TMPDIR``.
    """
    import tempfile

    from hpc_agent.infra.remote import rsync_pull
    from hpc_agent.state.runs import read_run_sidecar
    from hpc_agent.state.runtime_prior import ingest_runtime_samples_from_combiner_dir

    try:
        with tempfile.TemporaryDirectory(prefix="hpc_runtime_pull_") as local_dir_str:
            local_dir = Path(local_dir_str)
            result = rsync_pull(
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                remote_subdir="_combiner",
                local_dir=str(local_dir),
                include=["wave_*.runtime.json"],
            )
            if result.returncode != 0:
                return 0
            cmd_sha = None
            with contextlib.suppress(FileNotFoundError, OSError, json.JSONDecodeError):
                cmd_sha = read_run_sidecar(experiment_dir, record.run_id).get("cmd_sha")
            return ingest_runtime_samples_from_combiner_dir(
                local_dir,
                experiment_dir=experiment_dir,
                profile=record.profile,
                cluster=record.cluster,
                cmd_sha=cmd_sha,
            )
    except (OSError, TimeoutError):
        return 0


def _is_terminal(
    last_status: dict[str, Any],
    total_tasks: int,
    *,
    partial_ok: bool = False,
) -> tuple[str | None, str | None]:
    """Inspect counts and return (lifecycle_state, escalation_reason).

    Returns ``(None, None)`` when still in flight.

    With ``partial_ok=True``, the wave is classified ``complete`` as
    soon as no work is left and at least one task succeeded. Only a
    zero-success wave is classified ``failed`` under partial-ok.
    """
    complete = int(last_status.get("complete", 0))
    running = int(last_status.get("running", 0))
    pending = int(last_status.get("pending", 0))
    failed = int(last_status.get("failed", 0))

    if complete >= total_tasks:
        return (LifecycleState.COMPLETE, None)
    if running == 0 and pending == 0 and failed > 0:
        if partial_ok and complete > 0:
            # Partial success: at least one task done, no work left.
            return (LifecycleState.COMPLETE, "partial_ok_with_failures")
        # No work left and at least one failure. MVP doesn't auto-resubmit;
        # surface the failure for the caller to handle.
        return (LifecycleState.FAILED, "failed_tasks_no_auto_recover_in_mvp")
    return (None, None)


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
                complete_escalation = (
                    "combine_failed_waves:waves="
                    + ",".join(str(w) for w in state.last_failed_waves)
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
