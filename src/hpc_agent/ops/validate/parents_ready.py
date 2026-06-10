"""``validate-parents-ready`` primitive — pre-submit DAG readiness check.

The readiness piece of the DAG kernel (``docs/design/dag-kernel.md``):
"every parent reached an authoritative terminal lifecycle" — the
∀-parents quantifier over the per-run machinery that already exists
(sidecars for existence, the journal for lifecycle). A child submitted
before a parent is ``complete`` materializes its tasks from partial or
absent parent outputs, silently.

Compose this BEFORE a parented ``submit-flow``; the submit path itself
deliberately does not enforce readiness (same stance as
``validate-stochastic-marker`` vs the campaign loop: the workflow stays
mechanical, the gate is an independently-skippable validator).

Pure local: sidecar reads + journal ``load_run``. No SSH.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent._kernel.contract.vocabulary import JournalStatus
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.validators.validate_parents_ready import (
    ValidateParentsReadyResult,
    ValidateParentsReadySpec,
)
from hpc_agent._wire.workflows.validate_campaign import ValidatorFinding

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATOR = "validate-parents-ready"

#: Observed states that are NOT findings. Only terminal-success: a
#: ``failed`` parent's partial outputs are exactly what the check exists
#: to keep out of a child's input set.
_READY = {str(JournalStatus.COMPLETE)}


def _observe_parent(experiment_dir: Path, run_id: str) -> str:
    """Return the parent's observed state: a JournalStatus value,
    ``missing`` (no sidecar), or ``unknown`` (sidecar, no journal record)."""
    import json

    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import run_sidecar_path

    try:
        has_sidecar = run_sidecar_path(experiment_dir, run_id).is_file()
    except Exception:  # noqa: BLE001 — malformed run_id etc.: report, don't crash
        has_sidecar = False
    if not has_sidecar:
        return "missing"
    try:
        record = load_run(experiment_dir, run_id)
    except (OSError, json.JSONDecodeError):
        record = None
    if record is None:
        return "unknown"
    return str(record.status)


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def validate_parents_ready(
    experiment_dir: Path,
    *,
    spec: ValidateParentsReadySpec,
) -> ValidateParentsReadyResult:
    """Check every declared parent reached terminal-success.

    One finding per not-ready parent (codes: ``parent_run_missing``,
    ``parent_not_terminal``, ``parent_failed``), each carrying the
    observed state as evidence and a state-specific ``suggested_fix``.
    ``parent_states`` reports every parent's observed state whether or
    not it fired a finding, so a caller can render the whole frontier.
    """
    findings: list[ValidatorFinding] = []
    parent_states: dict[str, str] = {}

    for run_id in dict.fromkeys(spec.parent_run_ids):  # de-dupe, keep order
        state = _observe_parent(experiment_dir, run_id)
        parent_states[run_id] = state
        if state in _READY:
            continue
        if state == "missing":
            code = "parent_run_missing"
            message = (
                f"declared parent {run_id!r} has no sidecar under "
                ".hpc/runs/ — the dependency does not exist locally."
            )
            fix = (
                "Submit the parent first (its sidecar is written at submit "
                "time), fix the run_id in `parents`, or remove the edge."
            )
        elif state in (str(JournalStatus.FAILED), str(JournalStatus.ABANDONED)):
            code = "parent_failed"
            message = (
                f"declared parent {run_id!r} is terminal but NOT complete "
                f"(journal status: {state}) — its outputs are partial or "
                "absent, and a child submitted now would read them silently."
            )
            fix = (
                "Re-run the parent to completion first (hpc-agent "
                "resubmit-failed / a fresh submit), or drop the edge if the "
                "child no longer needs it."
            )
        else:
            # in_flight, or unknown (no journal record: possibly running on
            # another machine, or the journal was wiped).
            code = "parent_not_terminal"
            message = (
                f"declared parent {run_id!r} has not reached a terminal "
                f"lifecycle (observed: {state}) — its outputs may still be "
                "changing."
            )
            fix = (
                "Wait for the parent to finish (hpc-agent monitor-summary "
                f"--run-id {run_id}), or — if you know it finished and the "
                "journal is stale — mark it via mark-run-terminal."
            )
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code=code,
                message=message,
                suggested_fix=fix,
                evidence={"parent_run_id": run_id, "observed_state": state},
            )
        )

    return ValidateParentsReadyResult(findings=findings, parent_states=parent_states)
