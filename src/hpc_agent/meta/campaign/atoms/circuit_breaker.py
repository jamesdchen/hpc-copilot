"""Campaign loop-safety circuit breaker — halt on N consecutive failures.

An unattended campaign that resubmits each iteration can loop forever when
every iteration fails for the same systemic reason (a broken environment, a
bad cluster node pool, an entry-point that no longer imports). The budget
governor eventually catches this via spend, but a *fast* halt on consecutive
failure is the cheaper guard.

**What "canary failure" maps to here.** The framework persists no
campaign-iteration *canary* signal distinct from the iteration's run
lifecycle: a campaign iteration is one submitted run, tagged with the
``campaign_id``, and its terminal disposition lives on the journal
``RunRecord.status`` (``complete`` / ``failed`` / ``abandoned`` /
``in_flight`` — see ``_kernel.contract.vocabulary.JournalStatus``). The same
status the existing ``campaign-health`` atom counts as ``n_failed``. So the
consecutive-failure signal is derived from that existing state — NO new
persistence — by counting the trailing run of terminal non-``complete``
iterations in submit order. A ``failed`` or ``abandoned`` iteration counts;
a ``complete`` iteration resets the streak; an ``in_flight`` iteration is not
yet terminal and is skipped (it neither breaks nor extends the streak).

**Two streaks, honest attribution (provenance-review F1 twin).** Not every
``abandoned`` iteration is an experiment failure. A submit-once safe-resubmit
(``reconcile._safe_resubmit``) stamps ``submitting -> abandoned`` with
``last_status.verdict_reason == NEVER_DISPATCHED_VERDICT_REASON`` for a child
whose array NEVER entered the scheduler — a dispatch-window / control-plane
infra event, ZERO tasks ran. Counting it as a genuine iteration failure lets
sustained control-plane flapping (each refill minting a fresh run_id that
orphans) trip the breaker with a rationale that MISATTRIBUTES an infra fault
to experiment failures. So we keep TWO trailing-streak counters:

* ``count`` / ``run_ids`` — genuine **iteration** failures (``failed``, or
  ``abandoned`` WITHOUT the never-dispatched verdict_reason).
* ``never_dispatched_count`` / ``never_dispatched_run_ids`` — ``abandoned``
  runs stamped never-dispatched (a control-plane fault, no iteration ran).

The two streaks skip PAST each other (an iteration failure is neutral to the
never-dispatched streak and vice-versa — like an ``in_flight`` skip); only a
terminal ``complete`` resets BOTH. This is deliberately the SAFE reading
(review option (b), not (a)-skip): a stuck dispatch loop that alternates infra
faults with real failures still halts (whichever streak reaches the threshold
first), so loop safety is preserved — only the *attribution* is split, so the
``campaign-advance`` breaker can fire with an HONEST rationale distinguishing
"N consecutive iteration failures" from "N consecutive never-dispatched
submits (control-plane/dispatch fault)".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.vocabulary import (
    NEVER_DISPATCHED_VERDICT_REASON,
    TERMINAL_STATUSES,
)

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = ["consecutive_terminal_failures"]

# A terminal iteration that did not reach ``complete`` is a failed iteration
# for circuit-breaker purposes. Mirrors ``vocabulary.TERMINAL_STATUSES`` minus
# ``complete`` (the same set ``journal._RESUBMITTABLE_TERMINAL_STATUSES`` uses).
_FAILED_TERMINAL: frozenset[str] = frozenset({"failed", "abandoned"})


def _is_never_dispatched(record: RunRecord) -> bool:
    """True when *record* is an ``abandoned`` run a submit-once safe-resubmit
    stamped never-dispatched (its array NEVER entered the scheduler).

    Reads the durable ``last_status.verdict_reason`` reconcile persists — the
    RunRecord already carries ``last_status`` into the breaker's view, so no new
    field has to be threaded. Only ``abandoned`` runs qualify: a ``failed`` run
    is always a genuine iteration failure regardless of any stamped reason.
    """
    if str(getattr(record, "status", "")) != "abandoned":
        return False
    last_status = getattr(record, "last_status", None) or {}
    return last_status.get("verdict_reason") == NEVER_DISPATCHED_VERDICT_REASON


def consecutive_terminal_failures(runs: list[RunRecord]) -> dict[str, Any]:
    """Count the trailing runs of consecutive failed campaign iterations.

    *runs* is the campaign's journal records **oldest-first** (as
    :func:`hpc_agent.state.index.find_runs_by_campaign` returns them). We
    walk newest→oldest over the terminal iterations, maintaining TWO trailing
    streaks (see the module docstring):

    * genuine **iteration** failures — ``failed``, or ``abandoned`` that was
      NOT stamped never-dispatched;
    * **never-dispatched** submits — ``abandoned`` runs a submit-once
      safe-resubmit stamped ``NEVER_DISPATCHED_VERDICT_REASON`` (a
      control-plane/dispatch fault; no iteration executed).

    An ``in_flight`` / ``submitting`` iteration (not yet terminal) is skipped so
    a just-submitted retry doesn't reset a real streak before it has a verdict.
    The two streaks also skip PAST each other (each is neutral to the other),
    and the first terminal ``complete`` ends BOTH — so a stuck dispatch loop
    still halts whichever way it flaps while the attribution stays truthful.

    Returns::

        {
          "count": int,                       # iteration-failure streak length
          "run_ids": [newest-first],          #   its runs
          "never_dispatched_count": int,      # never-dispatched streak length
          "never_dispatched_run_ids": [...],  #   its runs (newest-first)
          "last_status": str | None,          # most-recent terminal status
        }

    ``last_status`` is ``None`` when no iteration has reached a terminal state
    yet.
    """
    count = 0
    failing_run_ids: list[str] = []
    never_dispatched_count = 0
    never_dispatched_run_ids: list[str] = []
    last_terminal_status: str | None = None
    for record in reversed(runs):  # newest-first
        status = getattr(record, "status", None)
        if status not in TERMINAL_STATUSES:
            # Not yet terminal (``in_flight`` OR ``submitting``) — carries no
            # verdict, so it neither breaks nor extends either streak. A
            # ``submitting`` orphan (U3 live flip, mid-dispatch process death)
            # must NOT be mis-read as a terminal non-failure that ends the
            # streak and silently disarms the breaker (provenance-review F1).
            continue
        if last_terminal_status is None:
            last_terminal_status = status
        if status not in _FAILED_TERMINAL:
            # A terminal non-failure (``complete``) ends BOTH streaks.
            break
        if _is_never_dispatched(record):
            # A never-dispatched abandon: extend the never-dispatched streak;
            # neutral to the iteration streak (skipped past, never a reset).
            never_dispatched_count += 1
            never_dispatched_run_ids.append(str(getattr(record, "run_id", "")))
        else:
            # A genuine iteration failure: extend the iteration streak; neutral
            # to the never-dispatched streak (skipped past, never a reset).
            count += 1
            failing_run_ids.append(str(getattr(record, "run_id", "")))
    return {
        "count": count,
        "run_ids": failing_run_ids,
        "never_dispatched_count": never_dispatched_count,
        "never_dispatched_run_ids": never_dispatched_run_ids,
        "last_status": last_terminal_status,
    }
