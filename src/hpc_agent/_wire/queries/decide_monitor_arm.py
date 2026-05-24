"""Pydantic models for the ``decide-monitor-arm`` query atom's wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class DecideMonitorArmSpec(BaseModel):
    """Run-state inputs to ``hpc_agent.ops.monitor.arm.decide_monitor_arm``.

    Drives the cron / loop / none arm decision + cadence.
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
    model_config = ConfigDict(extra="forbid")

    schedule: str
    prompt: str
    reason: str


class DecideMonitorArmResult(BaseModel):
    """Decision record from decide-monitor-arm.

    When arm == 'cron' the caller passes cron_create_args to CronCreate
    to schedule the next monitor tick.
    """

    model_config = ConfigDict(extra="forbid", title="decide-monitor-arm output")

    arm: Literal["cron", "loop", "none"]
    cadence_sec: int = Field(ge=0)
    reason: str
    schedule: str | None = Field(
        description="Cron expression (e.g. '*/5 * * * *') when arm=='cron'; null otherwise.",
    )
    cron_create_args: _CronCreateArgs | None = Field(
        description="Ready-to-pass keyword args for the CronCreate Claude Code tool when arm=='cron'; null otherwise.",
    )
