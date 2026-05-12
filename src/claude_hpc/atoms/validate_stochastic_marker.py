"""``validate-stochastic-marker`` primitive — pre-submit closed-loop dedup check.

Catches the bug class where a stochastic strategy (Optuna,
random-search, PBT) re-picks the same params across iterations,
making the new iteration's ``cmd_sha`` identical to a prior
iteration's. Without a unique-per-iteration discriminator field
(idiomatic: ``_optuna_trial_number``) in ``tasks.resolve()``'s
output, ``submit-flow`` would dedupe the new iteration silently and
the campaign collapses to a single iteration.

Pure local validator: scans the experiment's ``.hpc/runs/`` sidecars
for entries tagged with the same ``campaign_id`` and compares
``cmd_sha``. Caller (typically the slash command or
``validate-campaign`` workflow) computes the about-to-submit run's
``cmd_sha`` and passes it in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._internal.primitive import primitive
from claude_hpc._schema_models.validators.validate_stochastic_marker import (
    ValidateStochasticMarkerResult,
    ValidateStochasticMarkerSpec,
)
from claude_hpc._schema_models.workflows.validate_campaign import ValidatorFinding

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATOR = "validate-stochastic-marker"


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def validate_stochastic_marker(
    experiment_dir: Path,
    *,
    spec: ValidateStochasticMarkerSpec,
) -> ValidateStochasticMarkerResult:
    """Detect cmd_sha collision against prior iterations of *spec.campaign_id*.

    Reads ``find_existing_runs(experiment_dir)`` and filters to
    sidecars whose ``campaign_id`` matches *spec.campaign_id*; any
    sidecar with ``cmd_sha == spec.expected_cmd_sha`` is a silent-
    dedup collision and produces an ``error`` finding.

    The recommended fix in the finding's ``suggested_fix`` is to add
    a unique-per-iteration field (e.g. ``_optuna_trial_number``)
    inside ``tasks.resolve()``'s output so each iteration's
    ``cmd_sha`` differs even when the strategy re-picks the same
    params.
    """
    from claude_hpc.state.runs import find_existing_runs, read_run_sidecar

    matched_prior_run_ids: list[str] = []
    for sidecar_path in find_existing_runs(experiment_dir):
        # ``find_existing_runs`` returns Path objects to per-run JSON
        # sidecars; the run_id is the file stem. Read each and compare
        # campaign_id + cmd_sha.
        run_id = sidecar_path.stem
        try:
            sidecar = read_run_sidecar(experiment_dir, run_id)
        except FileNotFoundError:
            # Race: sidecar pruned between listing and read. Skip.
            continue
        if sidecar.get("campaign_id") != spec.campaign_id:
            continue
        if sidecar.get("cmd_sha") == spec.expected_cmd_sha:
            matched_prior_run_ids.append(run_id)

    findings: list[ValidatorFinding] = []
    if matched_prior_run_ids:
        # Sort newest-first so the most recent collision is at the top.
        matched_prior_run_ids = sorted(matched_prior_run_ids, reverse=True)
        sample_run_id = matched_prior_run_ids[0]
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="stochastic_marker_missing",
                message=(
                    f"This submit's cmd_sha {spec.expected_cmd_sha[:8]!s} "
                    f"matches prior iteration {sample_run_id!r} of campaign "
                    f"{spec.campaign_id!r} ({len(matched_prior_run_ids)} prior "
                    "iteration(s) total share this cmd_sha). submit-flow "
                    "would dedupe silently and the campaign would collapse to "
                    "a single iteration."
                ),
                suggested_fix=(
                    "Add a unique-per-iteration field (e.g. "
                    "``_optuna_trial_number`` or ``_iteration_index``) inside "
                    "tasks.resolve()'s output dict so each iteration's "
                    "cmd_sha differs even when the strategy picks repeat "
                    "params. Path A (manual params) campaigns don't need this "
                    "because the param tuple itself differs per iteration."
                ),
                evidence={
                    "campaign_id": spec.campaign_id,
                    "expected_cmd_sha": spec.expected_cmd_sha,
                    "matched_prior_run_ids": matched_prior_run_ids,
                    "n_collisions": len(matched_prior_run_ids),
                },
            )
        )

    return ValidateStochasticMarkerResult(
        findings=findings,
        matched_prior_run_ids=matched_prior_run_ids,
    )
