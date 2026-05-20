"""Wire model for the ``validate-stochastic-marker`` atom.

Catches the stochastic-campaign silent-dedup bug: when a closed-loop
campaign uses Optuna / random-search / PBT and the strategy happens
to re-pick the same params on iteration N as iteration N-1, the
two iterations' ``cmd_sha`` is identical and ``submit-flow`` dedupes
silently — collapsing the campaign to a single iteration.

The fix the user is supposed to add is a unique-per-iteration
discriminator (idiomatic: ``_optuna_trial_number`` or equivalent
integer field) inside ``tasks.resolve()``'s output dict so each
iteration's ``cmd_sha`` differs even when the strategy picks repeat
params. Easy to forget; expensive to discover hours into a campaign.

This validator exists to catch the bug at submit time:

  1. Take the ``expected_cmd_sha`` of the about-to-submit run.
  2. Search every prior run sidecar tagged with the same
     ``campaign_id``.
  3. If any prior cmd_sha collides, emit an ``error`` finding
     stating that ``submit-flow`` would dedupe silently and
     suggesting the user add the marker.

Pure local validator — reads sidecars from ``.hpc/runs/`` and the
journal. No SSH, no qsub.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._schema_models._shared import RunIdLoose  # noqa: TC001
from hpc_agent._schema_models.workflows.validate_campaign import (
    ValidatorFinding,  # noqa: TC001 — Pydantic resolves the annotation at runtime
)


class ValidateStochasticMarkerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(
        min_length=1,
        pattern=r"^[A-Za-z0-9._\-]+$",
        description=(
            "Slug identifying the closed-loop campaign. Must match the "
            "slug threaded through submit-flow's spec."
        ),
    )
    expected_cmd_sha: str = Field(
        min_length=8,
        description=(
            "The cmd_sha the about-to-submit run will have, computed via "
            "``compute_cmd_sha(load_tasks_module(.hpc/tasks.py))`` BEFORE "
            "invoking submit-flow. The validator checks whether any prior "
            "sidecar tagged with the same campaign_id already carries this "
            "cmd_sha — if so, the submit would dedupe silently."
        ),
    )


class ValidateStochasticMarkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ValidatorFinding] = Field(default_factory=list)
    matched_prior_run_ids: list[RunIdLoose] = Field(
        default_factory=list,
        description=(
            "Run IDs of prior iterations that share the about-to-submit "
            "run's cmd_sha. Empty list when no collision (the typical "
            "pass case). Populated as evidence when a collision fires."
        ),
    )
