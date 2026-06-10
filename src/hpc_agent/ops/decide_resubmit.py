"""``decide-resubmit`` primitive — auto-resubmit, complete, or escalate.

This lifts hpc-status Step 6's resubmit policy out of SKILL.md prose into
code so the agent calls one verb instead of computing ``failed_fraction``
and branching on the threshold itself. The policy lived ONLY in
``src/slash_commands/skills/hpc-status/SKILL.md`` Step 6 — there was no
implementation — which meant every status poll re-derived the same
arithmetic-plus-branch by hand, the classic prose-discipline failure mode.

Given a terminal-with-failures wave (``failed_count`` out of
``total_tasks``) and the caller's ``resubmit_failed_threshold`` (default
``0.0`` — auto-resubmit is an explicit opt-in), the decision splits three
ways:

* ``failed_fraction == 0`` → the lifecycle is actually ``complete`` — there
  is nothing to resubmit.
* ``failed_fraction <= threshold`` → ``resubmit`` the failed tasks. Only
  reachable when the caller opted into a threshold > 0 — they declared how
  much loss an automatic re-run may absorb (``safe_default`` ``None`` — no
  judgement needed).
* ``failed_fraction > threshold`` → ``escalate``. Under the default
  threshold this is every failure: auto-resubmitting can silently re-run
  the same bug, so this is **decision-as-data** — the primitive surfaces
  the choice with a ``safe_default`` of ``"investigate"`` rather than
  silently resubmitting.

Pure function over supplied evidence — no I/O. The only error is
``SpecInvalid`` when ``total_tasks < 1`` (a fraction over zero tasks is
undefined; there is nothing to have failed).
"""

from __future__ import annotations

from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = ["decide_resubmit"]


@primitive(
    name="decide-resubmit",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Decide complete / resubmit / escalate for a terminal-with-"
            "failures wave from observable evidence: failed-task count, "
            "total-task count, and the resubmit_failed_threshold. "
            "failed_fraction == 0 is actually complete; <= threshold auto-"
            "resubmits; > threshold escalates the resubmit/investigate/abandon "
            "choice (safe_default investigate) rather than wasting cluster "
            "time re-running the same bug. The default threshold is 0.0 — "
            "every failure escalates unless the caller opts into auto-"
            "resubmit by passing a threshold > 0. Replaces hpc-status "
            "Step 6 prose."
        ),
        requires_ssh=False,
        args=(
            CliArg(
                "--failed-count",
                type=int,
                required=True,
                help="Number of failed tasks (len(failed_task_ids)).",
            ),
            CliArg(
                "--total-tasks",
                type=int,
                required=True,
                help="Total tasks in the wave (record.total_tasks).",
            ),
            CliArg(
                "--resubmit-failed-threshold",
                type=float,
                default=0.0,
                help=(
                    "Auto-resubmit at or below this failed fraction. Default 0.0: "
                    "every failure escalates; pass a value > 0 to opt into "
                    "automatic resubmission."
                ),
            ),
        ),
    ),
    agent_facing=True,
)
def decide_resubmit(
    *,
    failed_count: int,
    total_tasks: int,
    resubmit_failed_threshold: float = 0.0,
) -> dict[str, Any]:
    """Decide complete / resubmit / escalate from failure evidence.

    Parameters
    ----------
    failed_count:
        ``len(failed_task_ids)`` — how many tasks landed in a terminal
        failed state.
    total_tasks:
        ``record.total_tasks`` — the wave size. Must be ``>= 1``; a failed
        fraction over zero tasks is undefined (``SpecInvalid``).
    resubmit_failed_threshold:
        The fraction at or below which a failure is auto-resubmitted. The
        boundary is **inclusive** — ``failed_fraction == threshold`` still
        resubmits. Defaults to ``0.0``: every failure escalates; a caller
        opts into auto-resubmit by declaring how much loss it may absorb.

    Returns
    -------
    Dict with ``action`` (``complete`` / ``resubmit`` / ``escalate``),
    ``failed_count``, ``total_tasks``, ``failed_fraction``, ``threshold``,
    ``safe_default`` (``"investigate"`` on escalate, else ``None``), and a
    human-readable ``rationale``.

    * ``failed_count == 0`` → ``complete`` — nothing failed.
    * ``failed_fraction <= threshold`` → ``resubmit`` — the caller opted
      into absorbing this much loss; ``safe_default`` ``None``.
    * ``failed_fraction > threshold`` → ``escalate`` — every failure under
      the default threshold; auto-resubmitting can silently re-run the same
      bug, so surface the choice with ``safe_default`` ``"investigate"``.
    """
    if total_tasks < 1:
        raise errors.SpecInvalid(
            f"total_tasks must be >= 1 to compute a failed fraction; got {total_tasks}"
        )

    failed_fraction = round(failed_count / total_tasks, 4)

    if failed_count == 0:
        return {
            "action": "complete",
            "failed_count": failed_count,
            "total_tasks": total_tasks,
            "failed_fraction": failed_fraction,
            "threshold": resubmit_failed_threshold,
            "safe_default": None,
            "rationale": "no failed tasks — the run is actually complete",
        }

    if failed_fraction <= resubmit_failed_threshold:
        return {
            "action": "resubmit",
            "failed_count": failed_count,
            "total_tasks": total_tasks,
            "failed_fraction": failed_fraction,
            "threshold": resubmit_failed_threshold,
            "safe_default": None,
            "rationale": (
                f"{failed_count}/{total_tasks} failed (failed_fraction "
                f"{failed_fraction:.0%}) at or below the "
                f"{resubmit_failed_threshold:.0%} threshold — auto-resubmit the failed tasks"
            ),
        }

    return {
        "action": "escalate",
        "failed_count": failed_count,
        "total_tasks": total_tasks,
        "failed_fraction": failed_fraction,
        "threshold": resubmit_failed_threshold,
        "safe_default": "investigate",
        "rationale": (
            f"{failed_count}/{total_tasks} failed (failed_fraction "
            f"{failed_fraction:.0%}) above the {resubmit_failed_threshold:.0%} "
            "threshold — auto-resubmitting usually wastes cluster time re-running "
            "the same bug; caller decides resubmit/investigate/abandon "
            "(safe_default investigate)"
        ),
    }
