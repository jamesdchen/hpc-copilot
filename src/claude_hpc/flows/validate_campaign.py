"""``validate-campaign`` workflow — composer for pre-submit validation.

Wires the three atomic validators (executor signatures, input dataset,
walltime against history) into a single agent-facing report. Each
atom is independently skippable: when its required spec field is None,
the workflow skips it and notes that in ``validators_run``.

The workflow is the hook point ``submit_flow`` invokes before any
SSH / qsub side effect; an ``overall == "fail"`` report aborts submit.
There is no ``--force`` runtime override — if a rule is wrong for a
project, the response is to edit ``.hpc/playbook.yaml``
(per-rule, version-controlled) rather than override the whole layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._internal.primitive import primitive
from claude_hpc._schema_models.validators.validate_executor_signatures import (
    ValidateExecutorSignaturesSpec,
)
from claude_hpc._schema_models.validators.validate_input_dataset import (
    ValidateInputDatasetSpec,
)
from claude_hpc._schema_models.validators.validate_walltime_against_history import (
    ValidateWalltimeAgainstHistorySpec,
)
from claude_hpc._schema_models.workflows.validate_campaign import (
    ValidateCampaignReport,
    ValidateCampaignSpec,
    ValidatorFinding,
)
from claude_hpc.atoms.validate_executor_signatures import validate_executor_signatures
from claude_hpc.atoms.validate_input_dataset import validate_input_dataset
from claude_hpc.atoms.validate_walltime_against_history import (
    validate_walltime_against_history,
)

if TYPE_CHECKING:
    from pathlib import Path


def _aggregate_overall(findings: list[ValidatorFinding]) -> str:
    """Reduce findings to a single overall verdict.

    Rule: any error → "fail"; else any warning → "warn"; else "pass".
    Info-level findings never escalate the verdict.
    """
    if any(f.severity == "error" for f in findings):
        return "fail"
    if any(f.severity == "warning" for f in findings):
        return "warn"
    return "pass"


@primitive(
    name="validate-campaign",
    verb="workflow",
    composes=[
        validate_executor_signatures,
        validate_input_dataset,
        validate_walltime_against_history,
    ],
    side_effects=[],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli="hpc-agent validate-campaign --spec <path>",
    agent_facing=True,
    exit_codes=[(0, "pass-or-warn"), (1, "fail")],
)
def validate_campaign(
    experiment_dir: Path,
    *,
    spec: ValidateCampaignSpec,
) -> ValidateCampaignReport:
    """Run every applicable atomic validator and aggregate findings.

    Each atom is skipped when its required spec input is None; the
    workflow tracks which ran in ``validators_run`` so the agent can
    distinguish "no findings because nothing checked" from "no findings
    because everything passed."

    Returns a :class:`ValidateCampaignReport`. ``overall`` is ``"fail"``
    if any finding has severity ``error``; ``"warn"`` if any has
    ``warning`` (no errors); ``"pass"`` otherwise. The submit-flow hook
    aborts on ``"fail"``; ``"warn"`` proceeds with the warnings surfaced.
    """
    findings: list[ValidatorFinding] = []
    validators_run: list[str] = []

    if spec.executor_module and spec.executor_function:
        result = validate_executor_signatures(
            experiment_dir,
            spec=ValidateExecutorSignaturesSpec(
                executor_module=spec.executor_module,
                executor_function=spec.executor_function,
            ),
        )
        findings.extend(result.findings)
        validators_run.append("validate-executor-signatures")

    if spec.dataset_path and spec.dataset_loader and spec.dataset_row_indices:
        result_d = validate_input_dataset(
            experiment_dir,
            spec=ValidateInputDatasetSpec(
                dataset_path=spec.dataset_path,
                loader=spec.dataset_loader,
                row_indices=spec.dataset_row_indices,
                required_non_null_cols=spec.dataset_required_non_null_cols,
            ),
        )
        findings.extend(result_d.findings)
        validators_run.append("validate-input-dataset")

    if spec.requested_walltime_sec is not None:
        result_w = validate_walltime_against_history(
            experiment_dir,
            spec=ValidateWalltimeAgainstHistorySpec(
                profile=spec.profile,
                cluster=spec.cluster,
                requested_walltime_sec=spec.requested_walltime_sec,
                gpu_type=spec.gpu_type,
                workload_tags=spec.workload_tags,
            ),
        )
        findings.extend(result_w.findings)
        validators_run.append("validate-walltime-against-history")

    return ValidateCampaignReport(
        overall=_aggregate_overall(findings),  # type: ignore[arg-type]
        findings=findings,
        validators_run=validators_run,
    )
