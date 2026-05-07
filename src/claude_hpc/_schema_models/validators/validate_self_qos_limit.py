"""Wire model for the ``validate-self-qos-limit`` atom.

Catches the lesson-6 self-DOS bug class: submitting a large task array
that pushes the user past their QOS's ``MaxJobsPerUser`` cap, which
not only blocks the new submission but drags the user's fair-share
score and stalls existing pendings.

Pure local validator — caller fetches the SSH-bound data (current
pending-job count, QOS limit) and passes it in. Keeps validate-
campaign side-effect-free at the framework boundary; the slash
command's pre-flight step does the SSH probe.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from claude_hpc._schema_models.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateSelfQosLimitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    new_array_size: int = Field(ge=1, description="Tasks the new submission would add.")
    current_user_pending_count: int = Field(
        ge=0, description="Existing pending jobs the user has on this cluster/QOS."
    )
    qos_max_jobs_per_user: int = Field(
        ge=1, description="The QOS's MaxJobsPerUser cap (sacctmgr show qos)."
    )
    warn_at_pct: float = Field(
        default=0.7,
        gt=0.0,
        lt=1.0,
        description=(
            "Warn when (existing + new) >= warn_at_pct * cap. Default 0.7 — at "
            "70%% the next normal-sized array will likely trip the limit."
        ),
    )


class ValidateSelfQosLimitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
