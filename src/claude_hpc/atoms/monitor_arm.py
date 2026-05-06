"""``decide-monitor-arm`` primitive — pick cron/loop/none + cadence.

Replaces the slash-command prose that walked the agent through the
adaptive-delay table at /monitor-hpc Step 5. Takes the run's current
summary + total_tasks (+ optional ETA hints / a "user invoked via
/loop" flag) and returns:

  * ``arm`` — ``cron`` / ``loop`` / ``none``
  * ``cadence_sec`` — int, the schedule's period
  * ``schedule`` — cron expression like ``*/5 * * * *`` (or None when
    arm != "cron")
  * ``armed_line`` — the literal final-line-of-stdout the slash
    command must emit (the Stop hook checks for this verbatim)
  * ``cron_create_args`` — ready-to-pass dict for the agent's
    ``CronCreate`` tool call (or None when arm != "cron")

Eliminates four /monitor-hpc failure modes at once:

  1. Picking the arm mode  → primitive output, deterministic.
  2. Picking the cadence   → primitive output, from a single table.
  3. Cron schedule string  → primitive output, no string formatting.
  4. ``armed:`` line       → primitive output, copied verbatim.

The agent's job collapses to: read the run record, call this
primitive, copy ``armed_line`` to the end of stdout, and (when
``arm == "cron"``) pass ``cron_create_args`` to ``CronCreate``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from claude_hpc import errors
from claude_hpc._internal._primitive import primitive

# Adaptive delay table — lifted from /monitor-hpc Step 5's Markdown
# table so the primitive is the single source of truth. Each row is
# evaluated in order; the first matching condition wins.
_DELAY_RULES: tuple[tuple[str, int], ...] = (
    # (condition_label, cadence_sec)
    ("eta_lt_10min", 60),
    ("eta_10_30min_stable", 180),
    ("eta_10_30min_unstable", 90),
    ("eta_gt_30min_stable", 270),
    ("queue_wait_gt_30min", 1800),  # super-cache, one miss amortized
    ("hour_scale_queue", 3600),
    ("all_pending_fallback", 1800),
    ("running_fallback", 90),
)

_TERMINAL_STATES: frozenset[str] = frozenset({"complete", "failed", "abandoned", "timeout"})


@dataclass(frozen=True)
class MonitorArm:
    """Decision record from :func:`decide_monitor_arm`."""

    arm: str  # cron|loop|none
    cadence_sec: int
    reason: str
    schedule: str | None  # cron expression or None
    armed_line: str
    cron_create_args: dict[str, str] | None

    def to_envelope_data(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "cadence_sec": self.cadence_sec,
            "reason": self.reason,
            "schedule": self.schedule,
            "armed_line": self.armed_line,
            "cron_create_args": dict(self.cron_create_args) if self.cron_create_args else None,
        }


def _seconds_to_cron(cadence_sec: int) -> str:
    """Render a cadence as a cron schedule string.

    Anything <60s or non-divisor of 60 rounds up to the next sensible
    minute granularity — cron's smallest unit is the minute. The slash
    command's adaptive table never picks <60s, so this rounding is
    invisible in practice but keeps the function safe for hand-tested
    edges.
    """
    if cadence_sec <= 60:
        return "* * * * *"
    minutes = max(1, cadence_sec // 60)
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = max(1, minutes // 60)
    return f"0 */{hours} * * *"


def _classify_state(
    *,
    summary: dict[str, int],
    total_tasks: int,
    eta_sec: int | None,
    pace_unstable: bool,
    queue_wait_sec: int | None,
) -> tuple[str, int]:
    """Apply the adaptive table top-to-bottom; return (label, cadence_sec)."""
    running = int(summary.get("running") or 0)
    pending = int(summary.get("pending") or 0)
    complete = int(summary.get("complete") or 0)
    failed = int(summary.get("failed") or 0)
    all_pending = pending == total_tasks and complete == 0 and failed == 0

    # Hour-scale queue: explicit queue_wait_sec takes precedence.
    if queue_wait_sec is not None and queue_wait_sec >= 3600:
        return "hour_scale_queue", 3600
    if queue_wait_sec is not None and queue_wait_sec >= 1800:
        return "queue_wait_gt_30min", 1800
    # ETA-driven branches.
    if eta_sec is not None:
        if eta_sec < 600:  # <10 min
            return "eta_lt_10min", 60
        if eta_sec < 1800:  # 10-30 min
            return ("eta_10_30min_unstable", 90) if pace_unstable else ("eta_10_30min_stable", 180)
        # >30 min
        if not pace_unstable:
            return "eta_gt_30min_stable", 270
        return "eta_10_30min_unstable", 90
    # No ETA — fall back on running vs all-pending.
    if all_pending:
        return "all_pending_fallback", 1800
    if running > 0:
        return "running_fallback", 90
    return "running_fallback", 90  # safe default


@primitive(
    name="decide-monitor-arm",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-mapreduce decide-monitor-arm --spec <path>",
    agent_facing=True,
)
def decide_monitor_arm(
    *,
    run_id: str,
    summary: dict[str, int],
    total_tasks: int,
    invocation_argv: str,
    user_invoked_via_loop: bool = False,
    eta_sec: int | None = None,
    pace_unstable: bool = False,
    queue_wait_sec: int | None = None,
) -> dict[str, Any]:
    """Pick the arm mode + cadence + cron args + ``armed:`` line.

    Parameters
    ----------
    run_id:
        The run being monitored — stamped into the ``armed:`` line and
        the cron prompt so terminal-state cleanup can target it.
    summary:
        ``last_status['summary']`` from the run journal —
        ``{complete, running, pending, failed, unknown}``. Missing keys
        default to 0.
    total_tasks:
        ``record.total_tasks``. Used to detect the "all pending"
        regime where one super-cache wait is the right call.
    invocation_argv:
        The exact ``/monitor-hpc <args>`` string (or
        ``hpc-mapreduce monitor-flow ...``) that should fire next
        tick. Stamped into ``cron_create_args.prompt`` so CronCreate
        re-invokes with the same flags.
    user_invoked_via_loop:
        True iff the current tick is running under `/loop` (the user
        invoked it themselves). When True we surface ``arm="loop"``
        and emit a placeholder cadence — the slash command does NOT
        register a cron because the user is already driving the
        cadence.
    eta_sec, pace_unstable, queue_wait_sec:
        Optional progress hints. ``eta_sec`` bypasses the running/
        all-pending fallback; ``pace_unstable`` flips ``stable`` rows
        of the table to ``unstable``; ``queue_wait_sec`` triggers the
        super-cache regimes.

    Returns
    -------
    Dict with ``arm``, ``cadence_sec``, ``reason``, ``schedule``,
    ``armed_line``, ``cron_create_args``. The slash-command epilogue
    copies ``armed_line`` verbatim and (when ``arm == "cron"``) passes
    ``cron_create_args`` to the ``CronCreate`` tool.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Empty run_id, total_tasks < 0, or summary is not a dict.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id must be a non-empty string")
    if not isinstance(summary, dict):
        raise errors.SpecInvalid(f"summary must be a dict, got {type(summary).__name__}")
    if int(total_tasks) < 0:
        raise errors.SpecInvalid(f"total_tasks must be >=0, got {total_tasks!r}")

    # Terminal — no arming, cancel any prior cron.
    complete = int(summary.get("complete") or 0)
    failed = int(summary.get("failed") or 0)
    running = int(summary.get("running") or 0)
    pending = int(summary.get("pending") or 0)
    is_terminal = (complete == int(total_tasks) and total_tasks > 0) or (
        failed > 0 and running == 0 and pending == 0
    )
    if is_terminal:
        decision = MonitorArm(
            arm="none",
            cadence_sec=0,
            reason=("complete" if complete == total_tasks else "failed_no_running"),
            schedule=None,
            armed_line=(
                f"armed: none run_id={run_id} cadence=0s "
                f'reason="{"complete" if complete == total_tasks else "failed_no_running"}"'
            ),
            cron_create_args=None,
        )
        return decision.to_envelope_data()

    # User invoked via /loop — they own the cadence; we just emit the
    # armed: line and skip CronCreate.
    if user_invoked_via_loop:
        decision = MonitorArm(
            arm="loop",
            cadence_sec=0,
            reason="user_invoked_via_loop",
            schedule=None,
            armed_line=(f'armed: loop run_id={run_id} cadence=0s reason="user_invoked_via_loop"'),
            cron_create_args=None,
        )
        return decision.to_envelope_data()

    # Adaptive cron arming.
    label, cadence_sec = _classify_state(
        summary=summary,
        total_tasks=int(total_tasks),
        eta_sec=eta_sec,
        pace_unstable=pace_unstable,
        queue_wait_sec=queue_wait_sec,
    )
    schedule = _seconds_to_cron(cadence_sec)
    decision = MonitorArm(
        arm="cron",
        cadence_sec=cadence_sec,
        reason=label,
        schedule=schedule,
        armed_line=(f'armed: cron run_id={run_id} cadence={cadence_sec}s reason="{label}"'),
        cron_create_args={
            "schedule": schedule,
            "prompt": invocation_argv,
            "reason": f"adaptive {label} for run_id={run_id}",
        },
    )
    return decision.to_envelope_data()
