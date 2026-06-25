"""The single home for turning a reporter's per-task count summary into a
lifecycle verdict.

Before this module the count-to-verdict step lived inline in three places
that each re-derived "did the run finish / fail / vanish?" from the same
canonical ``TaskStatus`` summary:

* the monitor poll loop (:func:`hpc_agent.ops.monitor.terminal._is_terminal`),
* the reconcile settle path
  (:func:`hpc_agent.ops.monitor.reconcile._reconcile_one`), and
* the aggregate precondition (also via ``_is_terminal``).

Divergent inline verdicts are exactly what produced the abandoned-vs-failed
(#351) and canary-truthfulness bug cluster: three call sites disagreeing on
what a summary means. Centralizing the rule here gives one place to reason
about and one place to test the precedence.

There are deliberately **two** classifiers, because the verdict legitimately
depends on context, not just on the counts:

* :func:`classify_polling` runs **mid-flight**, while the scheduler may still
  hold live jobs. Completion is *lenient* (``complete >= total_tasks``) and is
  checked *before* failure, so a fully-complete count wins even with a stale
  ``failed`` bucket (a task that failed then succeeded on retry). "Nothing
  conclusive yet" returns ``(None, None)`` — keep polling.
* :func:`classify_settled` runs **after** reconcile has proven the scheduler
  holds nothing alive for this run and both probes ran cleanly. There is no
  "keep polling" arm; absence of any positive signal is a terminal
  ``abandoned``. Completion is *strict* (every bucket but ``complete`` is
  zero) and failure outranks absence.

The lenient-vs-strict completion divergence between the two is **intentional**
and is pinned by ``tests/ops/monitor/test_classify.py``; collapsing the two
completion predicates into one is a behavior change tracked separately, not a
silent edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hpc_agent._kernel.contract.vocabulary import LifecycleState

__all__ = [
    "all_tasks_complete",
    "run_failed",
    "classify_polling",
    "classify_settled",
    "settle",
    "SettleDecision",
    "SETTLE_REASON_COMPLETE",
    "SETTLE_REASON_FAILED",
    "SETTLE_REASON_ABANDONED",
]

# Stable machine-readable reasons for a settle verdict — the provenance an
# operator/agent reads to know WHY reconcile reached a terminal state, instead
# of re-deriving it from the raw counts. Recorded in the run's ``last_status``
# (``verdict_reason``) and carried out in the reconcile envelope.
SETTLE_REASON_COMPLETE = "all_tasks_complete"
SETTLE_REASON_FAILED = "positive_failure_evidence"
SETTLE_REASON_ABANDONED = "no_on_disk_evidence"

# The non-``complete`` buckets of the canonical 5-key ``TaskStatus`` summary
# (see ``execution/mapreduce/reduce/status._empty_summary``). A clean reporter
# always emits all five; a missing key counts as 0.
_NON_COMPLETE_KEYS = ("running", "pending", "failed", "unknown")


def all_tasks_complete(summary: dict[str, Any], total_tasks: int) -> bool:
    """True when the reporter's per-task counts *prove* the run finished.

    Ground-truth-from-the-cluster completion: every expected task is
    ``complete`` and NOTHING is failed / pending / running / unknown. This is
    the evidence that distinguishes a normal post-completion record purge
    (SGE/Slurm drop a finished job's records) from a genuine abandon. A run
    whose tasks all completed but whose scheduler records were purged is
    COMPLETE, not abandoned.

    Reads the canonical 5-key ``TaskStatus`` summary. Returns False on a
    reporter-failure summary (``{"error": ...}``) or a zero-task run: those
    carry no completion evidence and must not read as complete.
    """
    if total_tasks <= 0:
        return False
    complete = summary.get("complete")
    if not isinstance(complete, int) or complete != total_tasks:
        return False
    # Missing keys count as 0, but any positive non-complete count is
    # disqualifying evidence of non-completion.
    return all(int(summary.get(key, 0) or 0) == 0 for key in _NON_COMPLETE_KEYS)


def run_failed(summary: dict[str, Any]) -> bool:
    """True when the reporter's per-task counts *prove* a task ran AND failed.

    The symmetric counterpart to :func:`all_tasks_complete`: that one reads
    POSITIVE evidence of completion, this one reads POSITIVE evidence of
    *failure*. A reporter ``failed >= 1`` is a task that reached the cluster,
    ran, and exited non-zero (a readable ``exit_code``/traceback) — categorically
    different from a purged scratch dir where nothing is observable. Collapsing
    the two into ``abandoned`` (the pre-#351 bug) told the operator "scratch
    purged, no recovery; re-submit" for a run whose canary actually failed with
    a fixable error already on disk.

    A reporter-failure summary (``{"error": ...}``) carries no ``failed`` count,
    so this returns False — that case is routed through ``unable_to_verify``
    upstream, never here.
    """
    return int(summary.get("failed", 0) or 0) > 0


def classify_polling(
    summary: dict[str, Any],
    total_tasks: int,
    *,
    partial_ok: bool = False,
) -> tuple[str | None, str | None]:
    """Mid-flight verdict: ``(lifecycle_state, escalation_reason)``.

    Returns ``(None, None)`` while still in flight — the scheduler may still
    have running/pending work, so "no conclusion yet" is a valid answer.

    With ``partial_ok=True``, the wave is classified ``complete`` as soon as no
    work is left and at least one task succeeded; only a zero-success wave is
    ``failed`` under partial-ok.

    NOTE: completion here is *lenient* (``complete >= total_tasks``) and is
    checked BEFORE failure — unlike :func:`classify_settled`. The divergence is
    intentional (see module docstring) and pinned by tests.
    """
    complete = int(summary.get("complete", 0))
    running = int(summary.get("running", 0))
    pending = int(summary.get("pending", 0))
    failed = int(summary.get("failed", 0))

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


@dataclass(frozen=True)
class SettleDecision:
    """A settle verdict plus the provenance for why it was reached.

    ``verdict`` is the terminal lifecycle state; ``reason`` is one of the
    ``SETTLE_REASON_*`` constants naming the arm that fired; ``evidence`` is the
    count snapshot the decision was made from (so a recorded verdict is
    debuggable without re-running reconcile).
    """

    verdict: str
    reason: str
    evidence: dict[str, int]


def _evidence(summary: dict[str, Any], total_tasks: int) -> dict[str, int]:
    snap = {key: int(summary.get(key, 0) or 0) for key in ("complete", *_NON_COMPLETE_KEYS)}
    snap["total_tasks"] = int(total_tasks)
    return snap


def settle(summary: dict[str, Any], total_tasks: int) -> SettleDecision:
    """Settled verdict (with provenance) for a run the scheduler holds NOTHING
    alive for.

    Precondition (enforced by the caller): no recorded job is alive AND both
    the alive-check and the reporter probe ran cleanly. Under that precondition
    the verdict is one of three terminal states, in strict precedence of
    POSITIVE evidence first, absence last:

    1. all tasks complete (strict) -> ``complete``  (``all_tasks_complete``)
    2. any task ran and failed     -> ``failed``    (``positive_failure_evidence``)
    3. otherwise (no evidence)     -> ``abandoned`` (``no_on_disk_evidence``)

    Failure outranks absence (#351 sub-bug #4): a readable ``failed`` count is
    proof a task ran, never a vanished scratch. ``abandoned`` is reserved for
    "no on-disk evidence at all".
    """
    evidence = _evidence(summary, total_tasks)
    if all_tasks_complete(summary, total_tasks):
        return SettleDecision(LifecycleState.COMPLETE, SETTLE_REASON_COMPLETE, evidence)
    if run_failed(summary):
        return SettleDecision(LifecycleState.FAILED, SETTLE_REASON_FAILED, evidence)
    return SettleDecision(LifecycleState.ABANDONED, SETTLE_REASON_ABANDONED, evidence)


def classify_settled(summary: dict[str, Any], total_tasks: int) -> str:
    """Back-compat thin wrapper: the settle verdict without its provenance.

    Prefer :func:`settle` when you want the reason/evidence too.
    """
    return settle(summary, total_tasks).verdict
