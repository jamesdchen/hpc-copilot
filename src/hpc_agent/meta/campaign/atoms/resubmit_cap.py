"""Campaign loop-safety: cap total per-task resubmit attempts.

``runner.DEFAULT_AUTO_RETRY_POLICY`` caps how many times a task is
auto-retried *within a single run* — but that counter resets every time
the campaign loop submits a fresh run. A task slot that needs a retry
every iteration therefore burns resubmit after resubmit without any one
run ever hitting its within-run cap. This is the campaign-level extension
the budget governor (#224) asks for: a ceiling on the **total** resubmit
attempts any one task slot accrues across all of the campaign's runs.

The signal is derived from existing journal state — no new persistence.
Each ``RunRecord.retries`` maps ``task_id -> {attempts, ...}`` where
``attempts`` counts resubmits (a clean first submit records no entry), so
summing ``attempts`` per ``task_id`` across the campaign's runs yields the
campaign-wide resubmit count for that slot. ``campaign-advance`` emits the
``stop_resubmit_cap`` terminal decision when the worst slot meets the cap —
which now **defaults to** :data:`DEFAULT_MAX_TASK_RESUBMITS` so the loud-fail
backstop fires even when the manifest is silent (human-amplification design §5:
"same task resubmitted >2× → stop and surface"), overridable by an explicit
``--max-task-resubmits`` or a manifest value.

**On the grain.** Task ids restart at 0 in each run, so summing by id
folds the slot-N of every iteration together. For a runaway that keeps
re-running the same failing work that is exactly right; for a parameter
sweep where slot 0 is a different config each iteration it over-counts
slightly — but a loop-safety ceiling erring toward *halting sooner* is the
safe direction for a budget governor, and the per-slot breakdown is
returned so the agent can see precisely where the pressure is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

__all__ = ["DEFAULT_MAX_TASK_RESUBMITS", "max_task_resubmits"]

# Framework-default per-task campaign resubmit backstop. ``campaign-advance``
# applies this when neither an explicit ``--max-task-resubmits`` nor a manifest
# value (``stop_criteria.max_task_resubmits`` / ``anomaly_policy.resubmit_cap``)
# is set, so the loud-fail guard is a DEFAULT rather than opt-in. A manifest /
# CLI value always overrides it.
DEFAULT_MAX_TASK_RESUBMITS = 2


def max_task_resubmits(runs: list[RunRecord]) -> dict[str, Any]:
    """Sum per-task resubmit attempts across the campaign's runs.

    Returns ``{"count": int, "task_id": str | None, "per_task": {id:
    total}}`` — ``count`` is the largest campaign-wide resubmit total of
    any single task slot, ``task_id`` that worst slot (``None`` when no
    task has ever been resubmitted), and ``per_task`` the full breakdown
    (slots with zero resubmits are omitted to keep the payload tight).
    """
    per_task: dict[str, int] = {}
    for record in runs:
        retries = getattr(record, "retries", None) or {}
        if not isinstance(retries, dict):
            continue
        for tid, info in retries.items():
            if not isinstance(info, dict):
                continue
            attempts = int(info.get("attempts", 0) or 0)
            if attempts <= 0:
                continue
            key = str(tid)
            per_task[key] = per_task.get(key, 0) + attempts

    if per_task:
        worst_tid = max(per_task, key=lambda k: per_task[k])
        worst_count = per_task[worst_tid]
    else:
        worst_tid, worst_count = None, 0

    return {"count": worst_count, "task_id": worst_tid, "per_task": per_task}
