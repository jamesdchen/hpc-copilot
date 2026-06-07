"""Deterministic auto-resume decision core (#294 PR2 / Layer 2).

Pure functions — NO cluster I/O — so the safety-critical gate is exhaustively
testable in isolation. The actual cluster resubmit lives in the composite that
consumes a ``decide_auto_resume`` verdict; this module only decides.

Why this is safe by construction
--------------------------------
The dispatcher already records the resumable signal: its SIGTERM handler writes
a per-task ``preempt`` mark (and exits 130) on preemption / walltime. OOM
(exit 137) and executor errors carry no such mark. So "resumable kill" == that
existing mark — we only ever auto-resume on an *explicit preemption signal*, and
every other failure (OOM, a real bug, manual qdel) escalates to the operator /
agent. No fragile per-scheduler reason parsing is needed.

Three hard gates, all of which must pass to auto-resume:

1. ``policy_on`` — ``auto_resume_on_kill`` is opt-in, default OFF. A run that
   didn't opt in is never auto-resubmitted (zero blast radius for everyone else).
2. there is at least one *preempted* (resumable) task — OOM / error failures
   escalate instead of looping.
3. ``count < cap`` — the resume CAP is the ultimate backstop: even total
   misclassification can only waste ``cap`` resubmits before it escalates.
"""

from __future__ import annotations

import dataclasses
from typing import Any

__all__ = [
    "AutoResumeDecision",
    "resumable_task_ids",
    "decide_auto_resume",
    "decide_auto_resume_from_ids",
]


@dataclasses.dataclass(frozen=True)
class AutoResumeDecision:
    """The verdict: either auto-resume *task_ids*, or escalate with a reason."""

    action: str  # "resume" | "escalate"
    task_ids: tuple[int, ...]
    reason: str


def resumable_task_ids(sidecar: dict[str, Any]) -> list[int]:
    """Task ids the scheduler PREEMPTED (resumable), read from the run sidecar.

    A task is resumable iff its per-task sidecar entry carries the dispatcher's
    ``preempt`` mark (written by the SIGTERM handler on preemption / walltime).
    OOM and executor-error failures carry no such mark and are never returned —
    the conservative posture that keeps auto-resume off the failure modes that
    would just repeat.
    """
    tasks = sidecar.get("tasks")
    if not isinstance(tasks, dict):
        return []
    out: list[int] = []
    for key, entry in tasks.items():
        if isinstance(entry, dict) and isinstance(entry.get("preempt"), dict):
            try:
                out.append(int(key))
            except (TypeError, ValueError):
                continue
    return sorted(out)


def decide_auto_resume(
    sidecar: dict[str, Any],
    *,
    policy_on: bool,
    count: int,
    cap: int,
) -> AutoResumeDecision:
    """Decide whether to auto-resume a killed run — the single safety boundary.

    Returns a ``"resume"`` verdict (with the preempted task ids) only when all
    three gates pass: the run opted in (*policy_on*), at least one task was
    preempted, and the run is under its resume *cap*. Every other case is an
    ``"escalate"`` verdict carrying the reason, so a human / the agent decides
    (OOM mitigation, a real bug, or "cap reached — investigate").

    This sidecar-shaped entry point reads the resumable ids out of the per-task
    ``preempt`` marks. The composite uses :func:`decide_auto_resume_from_ids`
    instead so it can feed the *cluster-authoritative* preempted set (the
    monitor's local sidecar is not refreshed with the dispatcher's marks).
    """
    return decide_auto_resume_from_ids(
        resumable_task_ids(sidecar), policy_on=policy_on, count=count, cap=cap
    )


def decide_auto_resume_from_ids(
    resumable_ids: list[int] | tuple[int, ...],
    *,
    policy_on: bool,
    count: int,
    cap: int,
) -> AutoResumeDecision:
    """Same three-gate decision as :func:`decide_auto_resume`, but taking the
    resumable (preempted) task ids directly.

    The composite sources *resumable_ids* from the cluster-authoritative
    failure classification (``fetch_failures``' ``preempted_task_ids``), which
    is both available without a refreshed local sidecar AND reflects the
    *current* attempt's exit reason — so a task that was preempted, resumed,
    then OOM-killed is classified ``system_oom`` (absent here) and correctly
    escalates instead of re-resuming a stale ``preempt`` mark.
    """
    ids = tuple(sorted(int(i) for i in resumable_ids))
    if not policy_on:
        return AutoResumeDecision("escalate", ids, "auto_resume_on_kill not enabled")
    if not ids:
        return AutoResumeDecision(
            "escalate", (), "no preempted (resumable) tasks — not a resumable kill"
        )
    if int(count) >= int(cap):
        return AutoResumeDecision("escalate", ids, f"auto-resume cap reached ({count}/{cap})")
    return AutoResumeDecision("resume", ids, "preempted tasks present and under resume cap")
