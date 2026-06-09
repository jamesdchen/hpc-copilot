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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = ["consecutive_terminal_failures"]

# A terminal iteration that did not reach ``complete`` is a failed iteration
# for circuit-breaker purposes. Mirrors ``vocabulary.TERMINAL_STATUSES`` minus
# ``complete`` (the same set ``journal._RESUBMITTABLE_TERMINAL_STATUSES`` uses).
_FAILED_TERMINAL: frozenset[str] = frozenset({"failed", "abandoned"})


def consecutive_terminal_failures(runs: list[RunRecord]) -> dict[str, Any]:
    """Count the trailing run of consecutive failed campaign iterations.

    *runs* is the campaign's journal records **oldest-first** (as
    :func:`hpc_agent.state.index.find_runs_by_campaign` returns them). We
    walk newest→oldest over the terminal iterations: each ``failed`` /
    ``abandoned`` extends the streak, the first ``complete`` ends it, and
    ``in_flight`` (not yet terminal) is skipped so a just-submitted retry
    doesn't reset a real failing streak before it has a verdict.

    Returns ``{"count": int, "run_ids": [newest-first], "last_status": str
    | None}`` — ``count`` is the number of consecutive terminal failures
    at the tail, ``run_ids`` the failing runs (newest-first), and
    ``last_status`` the most recent terminal iteration's status (or
    ``None`` when no iteration has reached a terminal state yet).
    """
    count = 0
    failing_run_ids: list[str] = []
    last_terminal_status: str | None = None
    for record in reversed(runs):  # newest-first
        status = getattr(record, "status", None)
        if status == "in_flight":
            # Not yet terminal — carries no verdict, so it neither breaks
            # nor extends the streak.
            continue
        if last_terminal_status is None:
            last_terminal_status = status
        if status in _FAILED_TERMINAL:
            count += 1
            failing_run_ids.append(str(getattr(record, "run_id", "")))
        else:
            # A terminal non-failure (``complete``) ends the streak.
            break
    return {
        "count": count,
        "run_ids": failing_run_ids,
        "last_status": last_terminal_status,
    }
