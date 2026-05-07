"""``validate-self-qos-limit`` primitive — pre-submit self-DOS check.

Catches the lesson-6 bug class: the user submits a 100-task array
that hits ``QOSMaxJobsPerUser`` and not only blocks the new
submission but drags their own fair-share score, stalling existing
pendings. Cheaper to refuse pre-submit than to find out 5 minutes
into the queue.

Pure local validator. The caller (slash command) fetches the
SSH-bound data — ``squeue --user`` count, ``sacctmgr show qos``
limit — and passes it in. The primitive returns findings without
SSH side effects, which keeps validate-campaign side-effect-free at
the framework boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._internal._primitive import primitive
from claude_hpc._schema_models.validate_campaign import ValidatorFinding
from claude_hpc._schema_models.validate_self_qos_limit import (
    ValidateSelfQosLimitResult,
    ValidateSelfQosLimitSpec,
)

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATOR = "validate-self-qos-limit"


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce validate-self-qos-limit --spec <path>",
    agent_facing=True,
)
def validate_self_qos_limit(
    experiment_dir: Path,  # noqa: ARG001 — convention: every atom takes experiment_dir
    *,
    spec: ValidateSelfQosLimitSpec,
) -> ValidateSelfQosLimitResult:
    """Compare predicted-total-pending against the QOS cap.

    Three regimes:

    * predicted >= cap → ``error``: the submit would self-DOS.
    * predicted >= cap * warn_at_pct → ``warning``: close to limit;
      surface to operator.
    * predicted < cap * warn_at_pct → no findings.

    The agent loop's recommended fix on error: pace the array by
    splitting into multiple smaller submissions, or wait for existing
    pendings to clear. Either path preserves fair-share.
    """
    predicted_total = spec.current_user_pending_count + spec.new_array_size
    cap = spec.qos_max_jobs_per_user
    warn_threshold = int(round(cap * spec.warn_at_pct))

    if predicted_total >= cap:
        finding = ValidatorFinding(
            validator=_VALIDATOR,
            severity="error",
            code="qos_max_jobs_exceeded",
            message=(
                f"Submitting {spec.new_array_size} tasks would push the user "
                f"to {predicted_total} pending jobs, at or above the QOS cap "
                f"of {cap}. The submission would self-DOS and drag fair-share."
            ),
            suggested_fix=(
                f"Split the array into smaller submissions of <= "
                f"{max(1, cap - spec.current_user_pending_count - 1)} tasks, "
                "or wait for existing pendings to clear before submitting."
            ),
            evidence={
                "current_user_pending_count": spec.current_user_pending_count,
                "new_array_size": spec.new_array_size,
                "predicted_total": predicted_total,
                "qos_max_jobs_per_user": cap,
            },
        )
        return ValidateSelfQosLimitResult(findings=[finding])

    if predicted_total >= warn_threshold:
        finding = ValidatorFinding(
            validator=_VALIDATOR,
            severity="warning",
            code="qos_max_jobs_near_limit",
            message=(
                f"Submitting {spec.new_array_size} tasks would push the user "
                f"to {predicted_total} pending jobs ({predicted_total / cap:.0%} "
                f"of the {cap}-job QOS cap)."
            ),
            suggested_fix=(
                "Consider splitting if any other campaign would submit before "
                "these clear."
            ),
            evidence={
                "current_user_pending_count": spec.current_user_pending_count,
                "new_array_size": spec.new_array_size,
                "predicted_total": predicted_total,
                "qos_max_jobs_per_user": cap,
                "fraction_of_cap": predicted_total / cap,
            },
        )
        return ValidateSelfQosLimitResult(findings=[finding])

    return ValidateSelfQosLimitResult(findings=[])
