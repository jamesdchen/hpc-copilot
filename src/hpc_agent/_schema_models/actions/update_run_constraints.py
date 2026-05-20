"""Wire model for the ``update-run-constraints`` mutate primitive.

Lesson 9: ``scontrol update jobid=N Features=X`` works post-submit
without losing age priority. The framework should expose this as a
first-class primitive so the agent loop can adjust constraints
mid-flight (e.g. add ``l40s`` to a job's Features when ``a100`` is
exhausted) without re-submitting and losing rank.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._schema_models._shared import (
    RunIdLoose,  # noqa: TC001 — Pydantic resolves the annotation at runtime
    RunIdStrict,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)

_NonEmptyStr = Annotated[str, Field(min_length=1)]


class UpdateRunConstraintsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: RunIdStrict
    add_features: list[_NonEmptyStr] = Field(
        default_factory=list,
        description="Features to add to each job (joined with the existing set).",
    )
    set_features: list[_NonEmptyStr] | None = Field(
        default=None,
        description=(
            "Replace the entire Features expression with this set (joined "
            "with the cluster's separator). Mutually exclusive with "
            "``add_features``."
        ),
    )

    @model_validator(mode="after")
    def _enforce_mutual_exclusion(self) -> UpdateRunConstraintsSpec:
        if self.add_features and self.set_features is not None:
            raise ValueError(
                "Pass exactly one of `set_features` (replace) or "
                "`add_features` (extend); they are mutually exclusive."
            )
        if not self.add_features and self.set_features is None:
            raise ValueError(
                "Pass at least one of `set_features` (replace) or "
                "`add_features` (extend); both are missing."
            )
        return self


class UpdateRunConstraintsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: RunIdLoose
    job_ids_updated: list[str] = Field(default_factory=list)
    job_ids_failed: list[str] = Field(default_factory=list)
    new_features: list[str] = Field(default_factory=list)
