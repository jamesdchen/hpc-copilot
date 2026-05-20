"""Wire model for the ``validate-walltime-against-history`` atom.

Three rule families:

1. **Walltime-vs-quantile** — when historical samples exist for
   ``(profile, cluster, gpu_type)``, compare ``requested_walltime_sec``
   against ``p95``; warn / error if below threshold.
2. **Known-bad combinations** — read ``.hpc/playbook.yaml``'s
   ``known_bad_combinations`` list, fire findings when the
   ``(gpu_type, workload_tag)`` pair matches a recorded "stop" rule
   (e.g. V100 + attn-fp32).
3. **Cold-start** — emit an ``info`` finding when no historical
   samples exist (cold-start campaign; the submit will produce the
   first samples). The agent still proceeds.

Configurable via ``.hpc/playbook.yaml``; absent file means defaults.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._schema_models.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateWalltimeAgainstHistorySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    requested_walltime_sec: int = Field(ge=1)
    gpu_type: str | None = None
    workload_tags: list[str] = Field(default_factory=list)


class ValidateWalltimeAgainstHistoryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
