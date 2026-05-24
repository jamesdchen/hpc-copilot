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

from typing import TYPE_CHECKING, Any, Literal

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.validators.validate_executor_signatures import (
    ValidateExecutorSignaturesSpec,
)
from hpc_agent._wire.validators.validate_input_dataset import (
    ValidateInputDatasetSpec,
)
from hpc_agent._wire.validators.validate_stochastic_marker import (
    ValidateStochasticMarkerSpec,
)
from hpc_agent._wire.validators.validate_walltime_against_history import (
    ValidateWalltimeAgainstHistorySpec,
)
from hpc_agent._wire.workflows.validate_campaign import (
    ValidateCampaignReport,
    ValidateCampaignSpec,
    ValidatorFinding,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.validate.executor_signatures import validate_executor_signatures
from hpc_agent.ops.validate.input_dataset import validate_input_dataset
from hpc_agent.ops.validate.stochastic_marker import validate_stochastic_marker
from hpc_agent.ops.validate.walltime_against_history import (
    validate_walltime_against_history,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _aggregate_overall(findings: list[ValidatorFinding]) -> Literal["pass", "warn", "fail"]:
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
        validate_stochastic_marker,
        validate_walltime_against_history,
    ],
    side_effects=[],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Validate a campaign spec end-to-end (executor signatures, dataset, "
            "QoS limits, walltime history, stochastic marker)."
        ),
        spec_arg=True,
        spec_model=ValidateCampaignSpec,
        schema_ref=SchemaRef(input="validate_campaign"),
        experiment_dir_arg=True,
    ),
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

    def _safe_run(name: str, fn: Callable[[], Any]) -> None:
        """Run one inner validator and synthesize a finding on raise.

        Inner validators are supposed to surface problems via
        ``findings``, but any of them can also raise (e.g. dataset path
        not found, executor module not importable, sshare fetch failure).
        A raise should not skip subsequent validators — the composed
        contract is "run every applicable validator and aggregate." Wrap
        every call so an internal exception lands as a structured
        ``validator_crashed`` finding and the next validator still runs.
        """
        validators_run.append(name)
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — validator boundary
            findings.append(
                ValidatorFinding(
                    validator=name,
                    severity="error",
                    code="validator_crashed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            )
            return
        findings.extend(result.findings)

    # Note on the local rebindings below: mypy does not preserve
    # ``if x is not None`` type-narrowing into a lambda closure (lambda
    # bodies are treated as deferred even when ``_safe_run`` invokes
    # them synchronously). Each branch captures the narrowed value into
    # a local first so the lambda references a definitely-non-None name.
    if spec.executor_module and spec.executor_function:
        executor_module = spec.executor_module
        executor_function = spec.executor_function
        _safe_run(
            "validate-executor-signatures",
            lambda: validate_executor_signatures(
                experiment_dir,
                spec=ValidateExecutorSignaturesSpec(
                    executor_module=executor_module,
                    executor_function=executor_function,
                ),
            ),
        )

    # Run the dataset validator whenever ``dataset_path + dataset_loader``
    # are both supplied. ``dataset_row_indices`` is now optional all the
    # way down: ``None`` and ``[]`` both mean "loader smoke-test only,
    # no row-level checks". Previously the ``is not None`` gate silently
    # skipped the validator when the user wanted a smoke-test.
    if spec.dataset_path and spec.dataset_loader:
        dataset_path = spec.dataset_path
        dataset_loader = spec.dataset_loader
        _safe_run(
            "validate-input-dataset",
            lambda: validate_input_dataset(
                experiment_dir,
                spec=ValidateInputDatasetSpec(
                    dataset_path=dataset_path,
                    loader=dataset_loader,
                    row_indices=spec.dataset_row_indices or [],
                    required_non_null_cols=spec.dataset_required_non_null_cols,
                ),
            ),
        )

    if spec.requested_walltime_sec is not None:
        requested_walltime_sec = spec.requested_walltime_sec
        _safe_run(
            "validate-walltime-against-history",
            lambda: validate_walltime_against_history(
                experiment_dir,
                spec=ValidateWalltimeAgainstHistorySpec(
                    profile=spec.profile,
                    cluster=spec.cluster,
                    requested_walltime_sec=requested_walltime_sec,
                    gpu_type=spec.gpu_type,
                    workload_tags=spec.workload_tags,
                ),
            ),
        )

    # Closed-loop campaign check: only fires when both campaign_id and
    # expected_cmd_sha are supplied. Catches the silent-dedup bug for
    # stochastic strategies that re-pick the same params.
    if spec.campaign_id and spec.expected_cmd_sha:
        campaign_id = spec.campaign_id
        expected_cmd_sha = spec.expected_cmd_sha
        _safe_run(
            "validate-stochastic-marker",
            lambda: validate_stochastic_marker(
                experiment_dir,
                spec=ValidateStochasticMarkerSpec(
                    campaign_id=campaign_id,
                    expected_cmd_sha=expected_cmd_sha,
                ),
            ),
        )

    return ValidateCampaignReport(
        overall=_aggregate_overall(findings),
        findings=findings,
        validators_run=validators_run,
    )
