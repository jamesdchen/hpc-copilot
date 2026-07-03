"""Pydantic models for the ``watcher-install`` mutator (design §5 hybrid monitor).

``watcher-install`` installs (or uninstalls, or reports the status of) a
cluster-side heartbeat watcher that survives the laptop. The watcher form is
chosen by an **install-time probe ladder**, never encoded site policy:

  1. user ``crontab``       — if viable (present, not cron.deny-blocked);
  2. ``scrontab`` (Slurm)   — if the run's scheduler is Slurm and it is viable;
  3. a self-resubmitting minimal watcher job — submitted through the backend seam;
  4. none available         — install NOTHING and say so LOUDLY (overnight
                              blindness persists).

Request → probe → install → report. The result names the mechanism that took,
or reports ``installed: false`` with a loud reason.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict, Scheduler

WatcherAction = Literal["install", "uninstall", "status"]
WatcherMechanism = Literal["cron", "scrontab", "job", "none"]


class WatcherInstallSpec(BaseModel):
    """Input spec for the ``watcher-install`` verb."""

    model_config = ConfigDict(extra="forbid", title="watcher-install input spec")

    run_id: RunIdStrict = Field(
        description="The run to watch. Its journal record supplies ssh_target + remote_path."
    )
    action: WatcherAction = Field(
        default="install",
        description="install (default) runs the probe ladder; uninstall reverses it; "
        "status reports what is currently installed.",
    )
    scheduler: Scheduler = Field(
        description="Backend/scheduler name — gates the scrontab rung (Slurm only) and "
        "supplies the submit binary for the self-resubmitting-job rung, both through "
        "the backend seam (never a concrete-backend import).",
    )
    stale_sec: int = Field(
        default=1800,
        ge=1,
        description="Alarm threshold: the watcher raises an ALARM when the client's "
        ".hpc_last_read marker is missing or older than this many seconds.",
    )
    interval_min: int = Field(
        default=10,
        ge=1,
        description="How often the watcher fires (minutes) — the cron/scrontab cadence "
        "and the self-resubmitting job's sleep interval.",
    )


class WatcherInstallResult(BaseModel):
    """Shape of the ``data`` field on a ``watcher-install`` envelope."""

    model_config = ConfigDict(extra="forbid", title="watcher-install output data")

    run_id: RunIdStrict
    action: WatcherAction
    installed: bool = Field(
        description="Whether a cluster-side watcher is installed after this call "
        "(install: a rung took; uninstall: always false; status: whether one is present)."
    )
    mechanism: WatcherMechanism = Field(
        description="Which rung of the ladder is in effect — cron / scrontab / job / none."
    )
    reason: str = Field(
        description="Human-readable outcome. On mechanism='none' this is the LOUD "
        "'overnight blindness persists' message."
    )
    detail: str = Field(
        default="",
        description="Mechanism-specific detail: the cron marker line, the submitted "
        "job id, or the probe failures that sank each rung.",
    )
    probes: dict[str, str] = Field(
        default_factory=dict,
        description="Per-rung probe verdicts (crontab / scrontab / job), for legibility "
        "and debugging why a given rung was or was not selected.",
    )
