"""``monitor-flow``: workflow atom that polls a run to terminal.

Pairs with :func:`hpc_mapreduce.job.submit_flow.submit_flow` to give
higher-level workflows (campaigns, sweeps) a clean composition path:
``submit-flow → monitor-flow → next iteration``. Both atoms expose the
same envelope shape, so the campaign loop's per-iteration code is just
two ``hpc-mapreduce <verb> --spec foo.json`` invocations.

What it does
------------
Internal poll loop:

1. ``runner.record_status(...)`` — fresh status from the cluster, refresh
   the journal's ``last_status``.
2. Detect newly-complete waves (cross-reference the per-task status against
   the sidecar's ``wave_map``). For each, invoke ``runner.combine_wave(...)``;
   on first failure, retry with ``force=True``; beyond that, mark as
   escalated and stop combining.
3. Append one tick record to ``.hpc/runs/<run_id>.monitor.jsonl`` —
   same schema as the slash-command ``/monitor-hpc`` writes, so the
   summary mode reads both.
4. Check for terminal conditions:
   - All tasks complete → ``runner.mark_terminal(complete)``, return.
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
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_mapreduce._primitive import SideEffect, primitive
from hpc_mapreduce._time import utcnow_iso
from hpc_mapreduce.lifecycle import LifecycleState
from hpc_mapreduce.job.runs import read_run_sidecar
from slash_commands import errors, runner, session
from slash_commands.runner import mark_terminal, record_status

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
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
    return session.runs_dir(experiment_dir) / f"{run_id}.monitor.jsonl"


@contextlib.contextmanager
def _flock_append(target: Path):
    """Hold an exclusive flock on a sibling ``.lock`` while yielding.

    Mirrors :func:`slash_commands.session._locked` so the slash-command
    surface (which appends to the same ``.monitor.jsonl`` file) and this
    workflow atom serialize their writes. Without flock, a concurrent
    slash-command poll and an in-process monitor_flow tick can interleave
    a partial JSON line and produce a torn record.

    Best-effort on platforms without ``fcntl`` (Windows): degrades to a
    no-op so the workflow primitive remains importable.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if _fcntl is None:
        yield
        return
    lock = target.with_suffix(target.suffix + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        os.close(fd)


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
    # B7: Route the JSONL append through hpc_mapreduce.telemetry, which
    # owns the flock-guarded writer pattern. The local _flock_append /
    # legacy fallback below remain as the on-disk shape -- the only
    # change is that the writer call goes through the canonical sink.
    # Telemetry's monitor-jsonl sink ignores HPC_TELEMETRY_SINK because
    # this caller is the canonical producer.
    try:
        from hpc_mapreduce.telemetry import record as _telemetry_record

        _telemetry_record(
            "tick", record,
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
    out: list[int] = []
    for k, counts in waves_block.items():
        try:
            wave_num = int(k)
        except (TypeError, ValueError):
            continue
        if wave_num in already_combined:
            continue
        if not isinstance(counts, dict):
            continue
        total = counts.get("total")
        complete = counts.get("complete")
        if total and complete and total == complete:
            out.append(wave_num)
    return sorted(out)


def _read_partial_ok(experiment_dir: Path, run_id: str) -> bool:
    """Read the partial_ok sibling marker written by submit-flow.

    Returns True iff ``<exp>/.hpc/runs/<run_id>.partial_ok`` exists.
    The marker is a sibling of the run sidecar (intentionally not a
    sidecar field) so the sidecar's frozen schema does not need to bump
    for this opt-in flag. See ``submit_flow.partial_ok``.
    """
    from hpc_mapreduce.job.runs import run_sidecar_path

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
    """
    from hpc_mapreduce.job.runs import run_sidecar_path

    target = run_sidecar_path(experiment_dir, run_id).with_suffix(".failed.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "failed_task_ids": sorted(set(int(t) for t in failed_task_ids)),
        "wave": wave,
        "classifier_codes": list(classifier_codes or []),
    }
    target.write_text(json.dumps(payload, indent=2, sort_keys=True))


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
    composes=[record_status, mark_terminal],
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (refreshes last_status)"),
    ],
    error_codes=[errors.SshUnreachable, errors.JournalCorrupt, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
)
def monitor_flow(
    *,
    experiment_dir: Path,
    run_id: str,
    poll_interval_seconds: float = 60.0,
    wall_clock_budget_seconds: float = 86400.0,
    auto_combine_waves: bool = True,
    combiner_max_retries: int = 1,
    file_glob: str = "*",
    _sleep: Any = time.sleep,
    _now: Any = time.monotonic,
) -> MonitorFlowResult:
    """Poll *run_id* to terminal-or-budget; auto-combine waves; emit one result.

    Idempotent in the sense that re-invoking after a terminal return is
    a no-op (the journal record already carries the terminal state and
    each poll is itself idempotent).

    Parameters ``_sleep`` and ``_now`` are injected for testability;
    production callers leave them at the defaults.
    """
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(
            f"no journal record for {run_id!r}; cannot monitor an unknown run"
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

    while True:
        state.ticks += 1
        elapsed = _now() - started

        # Poll.
        record = runner.record_status(
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
                attempt = state.combiner_attempts.get(wave, 0) + 1
                state.combiner_attempts[wave] = attempt
                ok, _stdout, stderr = runner.combine_wave(
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
                        state.combiner_attempts[wave] = 10**9  # never retry again

        # Terminal check.
        terminal, esc_reason = _is_terminal(
            last_status,
            int(record.total_tasks),
            partial_ok=_read_partial_ok(experiment_dir, run_id),
        )
        if terminal == LifecycleState.COMPLETE:
            runner.mark_terminal(experiment_dir, run_id, status=LifecycleState.COMPLETE)
            _append_tick(
                experiment_dir,
                run_id,
                summary=last_status,
                diff_from_prev=diff,
                actions=actions,
                lifecycle_state=LifecycleState.COMPLETE,
                next_tick_seconds=None,
            )
            return MonitorFlowResult(
                run_id=run_id,
                lifecycle_state=LifecycleState.COMPLETE,
                last_status=last_status,
                combined_waves=state.last_combined_waves,
                failed_waves=state.last_failed_waves,
                ticks=state.ticks,
                elapsed_seconds=elapsed,
                escalation_reason=None,
            )
        if terminal == LifecycleState.FAILED:
            _append_tick(
                experiment_dir,
                run_id,
                summary=last_status,
                diff_from_prev=diff,
                actions=actions,
                lifecycle_state=LifecycleState.FAILED,
                next_tick_seconds=None,
            )
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

        # Still in flight; record the tick and keep watching.
        _append_tick(
            experiment_dir,
            run_id,
            summary=last_status,
            diff_from_prev=diff,
            actions=actions,
            lifecycle_state="in_flight",
            next_tick_seconds=poll_interval_seconds,
        )
        _sleep(poll_interval_seconds)
