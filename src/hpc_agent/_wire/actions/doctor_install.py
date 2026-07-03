"""Pydantic models for the ``doctor-install`` verb (§5 dead-man's switch).

``doctor-install`` puts the detection-only ``doctor`` watchdog onto the OS
scheduler (Windows Task Scheduler / POSIX ``crontab``) so a missed driver-tick
deadline is caught *out of session* — the watch-the-watcher recursion bottoms
out at the OS scheduler (design §5). It is **opt-in**: never auto-installed. The
scheduled task runs ``hpc-agent doctor`` every ``interval_minutes`` against a
fixed experiment dir, reading a durable spec written under the journal home; the
scheduled scan raises an OS notification when it finds a stalled/orphaned run
(``notify=true`` baked into the durable spec). Detection only — it NEVER
restarts or re-arms anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DoctorInstallSpec(BaseModel):
    """Input spec for the ``doctor-install`` verb."""

    model_config = ConfigDict(extra="forbid", title="doctor-install input spec")

    interval_minutes: int = Field(
        default=15,
        ge=1,
        description=(
            "How often (minutes) the OS scheduler runs `hpc-agent doctor`. "
            "Cheap local filesystem scan; 15 is a sane default."
        ),
    )
    uninstall: bool = Field(
        default=False,
        description=(
            "Remove the scheduled doctor task for this experiment dir instead of "
            "installing it. Idempotent: removing an absent task is a no-op."
        ),
    )
    notify: bool = Field(
        default=True,
        description=(
            "Bake `notify=true` into the durable doctor spec so the scheduled "
            "scan raises an OS notification (never just prints JSON nobody reads) "
            "when it finds a stalled/orphaned run. Notify only — never acts."
        ),
    )


class DoctorInstallResult(BaseModel):
    """Shape of the ``data`` field on a ``doctor-install`` envelope."""

    model_config = ConfigDict(extra="forbid", title="doctor-install output data")

    status: Literal["installed", "already_installed", "uninstalled", "not_installed"] = Field(
        description=(
            "installed — a new scheduled task was created; already_installed — a "
            "task with the same name was already present (no duplicate); "
            "uninstalled — an existing task was removed; not_installed — uninstall "
            "requested but nothing was scheduled."
        )
    )
    platform: Literal["windows", "posix"] = Field(
        description="Which scheduler backend handled the request (Task Scheduler vs crontab)."
    )
    task_name: str = Field(
        description="Scheduler task name / cron marker, `hpc-agent-doctor-<repo_hash>`."
    )
    command: str = Field(
        description="The exact non-interactive command the scheduler runs each interval."
    )
    interval_minutes: int = Field(description="The scan cadence the task was (or would be) set to.")
    spec_path: str = Field(
        description="Durable doctor spec the scheduled command reads (under the journal home)."
    )
    notify: bool = Field(
        description="Whether the durable spec carries notify=true (scheduled scan alerts on stalls)."
    )
