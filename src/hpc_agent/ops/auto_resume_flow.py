"""Auto-resume composite — turn a ``decide_auto_resume`` verdict into a resubmit.

This is the #294 *Layer-2 auto-fire* remainder (#299): the one place a
read-only monitor path is allowed to put work back on the cluster
*automatically*, with no human and no agent judgement in the loop.

It composes three already-landed pieces:

* :func:`hpc_agent.ops.recover.failures_atom.fetch_failures` — the
  *cluster-authoritative* preemption signal. Its ``preempted_task_ids`` is
  derived from the **current** attempt's exit code / stderr fingerprint
  (``runner_failures.cluster_failures_by_fingerprint`` → exit 130/143 →
  ``preempted``), so it (a) is available even when the monitor's local
  sidecar was never refreshed with the dispatcher's cluster-side
  ``preempt`` marks, and (b) reflects the latest attempt — a task that was
  preempted, resumed, then OOM-killed reclassifies as ``system_oom`` and is
  absent here, so it escalates instead of re-resuming a stale mark.
* :func:`hpc_agent.recovery.auto_resume.decide_auto_resume_from_ids` — the
  pure, exhaustively-tested safety gate. It returns a ``"resume"`` verdict
  only when all three hard gates pass (opt-in ON, at least one preempted
  task, under the resume cap); otherwise ``"escalate"`` with a reason.
* :func:`hpc_agent.ops.recover_flow.resubmit_flow` — the action. On a
  ``"resume"`` verdict we re-submit exactly the preempted task ids
  ``from_checkpoint`` and bump the run's ``auto_resume_count``.

Safety, restated (the gate enforces it; this composite never relaxes it):

* **Opt-in, default OFF** — a run whose record did not set
  ``auto_resume_on_kill`` escalates immediately.
* **Only on an explicit preemption signal** — the cluster-authoritative
  ``preempted`` classification. OOM / executor errors are absent from it
  and escalate (resuming an OOM just re-OOMs).
* **Hard cap** — ``auto_resume_count < max_auto_resumes`` is the ultimate
  backstop.

On ``"escalate"`` this is a pure no-op that surfaces the reason — the
caller routes it through the existing escalation-as-data path (#234).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.ops.recover.failures_atom import fetch_failures as _fetch_failures
from hpc_agent.ops.recover_flow import resubmit_flow as _resubmit_flow
from hpc_agent.recovery.auto_resume import decide_auto_resume_from_ids
from hpc_agent.state.journal import load_run, update_run_status

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

__all__ = ["AutoResumeOutcome", "maybe_auto_resume"]


@dataclass(frozen=True)
class AutoResumeOutcome:
    """Result of consulting (and possibly firing) the auto-resume composite.

    ``action`` mirrors the gate's verdict (``"resume"`` | ``"escalate"``).
    ``resubmitted`` is True only when a cluster resubmit actually fired —
    so a caller can distinguish "the gate said resume and we did it" from
    every escalate / no-op path. ``reason`` always carries the gate's
    rationale so an escalation can be surfaced verbatim.
    """

    action: str
    reason: str
    task_ids: tuple[int, ...] = ()
    resubmitted: bool = False
    new_job_ids: list[str] = field(default_factory=list)
    auto_resume_count: int = 0


def _authoritative_preempted_ids(
    experiment_dir: Path,
    run_id: str,
    failures_fetcher: Callable[..., dict[str, Any]],
) -> tuple[list[int] | None, str]:
    """Return (preempted_task_ids, reason) from the cluster failure report.

    Returns ``(None, reason)`` when the report could not be fetched (SSH /
    cluster error) so the composite escalates rather than crashing the
    monitor loop. ``([], "")`` means "fetched cleanly, nothing preempted".
    """
    try:
        report = failures_fetcher(experiment_dir=experiment_dir, run_id=run_id)
    except (errors.HpcError, OSError, TimeoutError) as exc:
        return None, f"could not fetch cluster failures for auto-resume: {exc}"
    ids = report.get("preempted_task_ids") if isinstance(report, dict) else None
    if not isinstance(ids, list):
        return [], ""
    out: list[int] = []
    for i in ids:
        try:
            out.append(int(i))
        except (TypeError, ValueError):
            continue
    return out, ""


def maybe_auto_resume(
    experiment_dir: Path,
    run_id: str,
    *,
    record: RunRecord | None = None,
    preempted_task_ids: list[int] | None = None,
    resubmit: Callable[..., Any] = _resubmit_flow,
    failures_fetcher: Callable[..., dict[str, Any]] = _fetch_failures,
) -> AutoResumeOutcome:
    """Consult the auto-resume gate for *run_id* and fire a resubmit if it says so.

    Sources the preempted set, calls :func:`decide_auto_resume_from_ids`
    against the run's opt-in policy + cap, and on a ``"resume"`` verdict
    re-submits exactly the preempted task ids ``from_checkpoint`` via
    *resubmit* (defaults to :func:`resubmit_flow`), then increments the run's
    ``auto_resume_count``. Every other verdict is a no-op returning the gate's
    escalation reason.

    Preempted-set sourcing — both fresh (current-attempt), never the
    never-cleared sidecar mark:

    * *preempted_task_ids* — when the monitor already has the scheduler-side
      signal off ``last_status`` (the status reporter folds in exit-130/143
      / state-PREEMPTED ids), it passes them here and we skip the round-trip.
    * Otherwise we fall back to :func:`fetch_failures` (log-fingerprint
      classification) — cross-scheduler robust (e.g. SGE, where the live
      query carries no exit code), at the cost of one terminal-time fetch.

    Both sources now speak the 0-based ``HPC_TASK_ID`` domain space directly
    — the conversion moved to the scheduler-query ingest edge (Phase 2), so
    the status reporter's ``preempted_task_ids`` and ``fetch_failures'`` are
    already in the space ``resubmit_flow`` and the dispatcher use; this
    function passes them straight through with no compensating shift.

    *record* may be supplied to avoid a redundant journal read. *resubmit*
    and *failures_fetcher* are injection seams for tests.
    """
    if record is None:
        record = load_run(experiment_dir, run_id)
    if record is None:
        return AutoResumeOutcome("escalate", f"no journal record for {run_id!r}")

    # Cheap gate first: a run that did not opt in is never auto-resubmitted,
    # and we avoid an SSH round-trip for the common (opt-out) case.
    if not record.auto_resume_on_kill:
        return AutoResumeOutcome("escalate", "auto_resume_on_kill not enabled")

    if preempted_task_ids:
        # Lean path: the monitor already carries the fresh scheduler signal.
        resumable: list[int] = [int(i) for i in preempted_task_ids]
    else:
        # No pre-supplied signal (older reporter, or a scheduler whose live
        # query has no exit code) → log-based fetch, cross-scheduler robust.
        fetched, fetch_reason = _authoritative_preempted_ids(
            experiment_dir, run_id, failures_fetcher
        )
        if fetched is None:
            return AutoResumeOutcome("escalate", fetch_reason)
        resumable = fetched

    # No base conversion here (Phase 2). Both sources — the status reporter's
    # preempted_task_ids and fetch_failures' — are already 0-based
    # ``HPC_TASK_ID``: the single ``±1`` now lives at the scheduler-query
    # ingest edge (``to_task_id``), so ``report_status*`` keys tasks by
    # ``HPC_TASK_ID``, matching ``resubmit_flow`` and the dispatcher's
    # per-task ``preempt`` marks. A compensating shift here would
    # double-convert and re-submit the wrong task.
    decision = decide_auto_resume_from_ids(
        resumable,
        policy_on=bool(record.auto_resume_on_kill),
        count=int(record.auto_resume_count),
        cap=int(record.max_auto_resumes),
    )

    if decision.action != "resume":
        # Escalate: pure no-op. The caller surfaces ``reason`` through the
        # existing escalation-as-data path (#234) — this composite never
        # parallel-submits around the gate.
        return AutoResumeOutcome(
            "escalate",
            decision.reason,
            task_ids=decision.task_ids,
            auto_resume_count=int(record.auto_resume_count),
        )

    # The gate cleared all three hard gates. Re-submit exactly the preempted
    # ids from their latest checkpoint. ``category="preempted"`` is the honest
    # label; ``bypass_preempt_throttle=True`` opts out of the manual
    # "all-preempted → back off" guard (the cap is the backstop here, #299).
    # The request_id folds in the current count so each cap-loop attempt is a
    # distinct request (two genuine preemptions of the same task set must not
    # dedup against each other). No race window to guard: the monitor only
    # invokes this when polling already classified the run terminal-FAILED, so
    # the just-resumed (pending) jobs cannot trigger a same-generation re-entry.
    failed_task_ids = list(decision.task_ids)
    request_id = f"auto_resume_{run_id}_{int(record.auto_resume_count)}"
    result = resubmit(
        experiment_dir,
        run_id,
        failed_task_ids=failed_task_ids,
        category="preempted",
        from_checkpoint=True,
        submit_to_cluster=True,
        script=record.script,
        backend=record.backend,
        job_name=record.job_name,
        job_env=dict(record.job_env),
        request_id=request_id,
        bypass_preempt_throttle=True,
    )

    deduped = bool(getattr(result, "deduped", False))
    count = int(record.auto_resume_count)
    if not deduped:
        # A real resubmit fired — bump the counter so the gate's "count < cap"
        # backstop tightens with every attempt. A deduped replay put nothing
        # new on the cluster, so it must NOT consume a cap slot.
        #
        # Fail CLOSED on a journal-write failure. The resubmit ALREADY put
        # work on the cluster, so this attempt MUST count against the cap even
        # when we cannot persist the bump. An uncaught write failure here would
        # (a) crash the monitor's terminal-FAILED tick outright and (b) leave
        # the counter un-bumped — which LOOSENS the cap (the next tick reads
        # the stale count and fires an extra resume PAST the intended ceiling),
        # the opposite of the old "can only escalate sooner" comment. So we
        # keep the bumped count for the returned outcome and log loudly rather
        # than under-count or propagate.
        count += 1
        try:
            updated = update_run_status(experiment_dir, run_id, auto_resume_count=count)
            count = int(updated.auto_resume_count)
        except (errors.HpcError, OSError) as exc:
            import logging

            logging.getLogger(__name__).error(
                "auto-resume for run_id=%s fired a resubmit but FAILED to "
                "persist the cap-counter bump (%s); counting the attempt "
                "in-memory to fail CLOSED so the resume cap cannot loosen. The "
                "journal auto_resume_count may be stale-by-one until the next "
                "successful status write.",
                run_id,
                exc,
            )

    return AutoResumeOutcome(
        "resume",
        decision.reason,
        task_ids=decision.task_ids,
        resubmitted=not deduped,
        new_job_ids=list(getattr(result, "new_job_ids", []) or []),
        auto_resume_count=count,
    )
