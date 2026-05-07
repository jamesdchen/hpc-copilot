"""Pydantic models for the ``decide-monitor-arm`` query atom's wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ._shared import RunIdStrict


class DecideMonitorArmSpec(BaseModel):
    """Run-state inputs to ``claude_hpc.atoms.monitor_arm.decide_monitor_arm``.

    Drives the cron / loop / none arm decision + cadence + literal
    armed: line.
    """

    model_config = ConfigDict(extra="forbid", title="decide-monitor-arm input")

    run_id: RunIdStrict
    summary: dict[str, int] = Field(
        description="last_status['summary'] from the run journal — {complete, running, pending, failed} integer counters. Missing keys default to 0.",
    )
    total_tasks: int = Field(ge=0)
    invocation_argv: str = Field(
        description="Exact /monitor-hpc <args> string the next tick should re-invoke. Stamped into cron_create_args.prompt.",
    )
    user_invoked_via_loop: bool | None = None
    eta_sec: int | None = Field(default=None, ge=0)
    pace_unstable: bool | None = None
    queue_wait_sec: int | None = Field(default=None, ge=0)


class _CronCreateArgs(BaseModel):
    schedule: str
    prompt: str
    reason: str


class DecideMonitorArmResult(BaseModel):
    """Decision record from decide-monitor-arm.

    The slash-command epilogue copies armed_line verbatim and (when
    arm == 'cron') passes cron_create_args to CronCreate.
    """

    model_config = ConfigDict(title="decide-monitor-arm output")

    arm: Literal["cron", "loop", "none"]
    cadence_sec: int = Field(ge=0)
    reason: str
    schedule: str | None = Field(
        description="Cron expression (e.g. '*/5 * * * *') when arm=='cron'; null otherwise.",
    )
    armed_line: str = Field(
        description="Literal final-line-of-stdout the slash command must emit; matches the Stop hook's regex by construction.",
    )
    cron_create_args: _CronCreateArgs | None = Field(
        description="Ready-to-pass keyword args for the CronCreate Claude Code tool when arm=='cron'; null otherwise.",
    )
