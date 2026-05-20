"""``validate-walltime-against-history`` primitive — historical-prior + playbook check.

Three rule families:

1. **Walltime vs. quantile.** Reads runtime priors via
   :func:`hpc_agent.state.runtime_prior.roll_up_quantiles`; for every
   ``walltime_rules`` entry in ``.hpc/playbook.yaml``, fires a finding
   when ``requested_walltime_sec < quantile`` (default rule: warn
   when requested < p95).
2. **Known-bad combinations.** Reads ``known_bad_combinations`` from
   the playbook; fires a finding when ``(gpu_type, workload_tag)``
   matches a recorded entry (e.g. V100 + attn-fp32 unstable).
3. **Cold-start.** No historical samples for (profile, cluster, gpu)
   → emits an info finding so the agent knows the lack of warning is
   "no data," not "all clear."

Configurable per-project via ``.hpc/playbook.yaml``; the framework
ships zero hardcoded rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._internal.playbook import (
    KnownBadCombination,
    Playbook,
    WalltimeRule,
    load_playbook,
)
from hpc_agent._internal.primitive import primitive
from hpc_agent._schema_models.validators.validate_walltime_against_history import (
    ValidateWalltimeAgainstHistoryResult,
    ValidateWalltimeAgainstHistorySpec,
)
from hpc_agent._schema_models.workflows.validate_campaign import ValidatorFinding

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATOR = "validate-walltime-against-history"

# Default rule when ``.hpc/playbook.yaml`` declares no walltime_rules.
# Mirrors the lesson: walltime below p95 of similar runs is the strongest
# correlate of in-flight TIMEOUT.
_DEFAULT_WALLTIME_RULES: tuple[WalltimeRule, ...] = (
    WalltimeRule(
        below_quantile=0.95,
        severity="warning",
        message="Requested walltime is below the historical p95.",
    ),
)


def _quantile_label(q: float) -> str:
    """Map a quantile to the rollup's column name (matches
    ``roll_up_quantiles`` output: 'p50', 'p95', 'p99', etc.)."""
    return f"p{int(round(q * 100))}"


def _walltime_findings(
    rollup: dict[str, Any],
    *,
    gpu_type: str | None,
    requested: int,
    rules: tuple[WalltimeRule, ...],
) -> list[ValidatorFinding]:
    findings: list[ValidatorFinding] = []
    quantiles_by_gpu = rollup.get("quantiles") or {}
    if gpu_type is None or gpu_type not in quantiles_by_gpu:
        return findings
    bucket = quantiles_by_gpu[gpu_type]
    for rule in rules:
        label = _quantile_label(rule.below_quantile)
        threshold = bucket.get(label)
        if not isinstance(threshold, int) or threshold <= 0:
            continue
        if requested >= threshold:
            continue
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity=rule.severity,
                code="walltime_below_quantile",
                message=(
                    f"{rule.message} "
                    f"requested={requested}s; {label}={threshold}s "
                    f"(n_samples={bucket.get('n_samples', 0)}, gpu={gpu_type})"
                ),
                suggested_fix=(
                    f"Increase requested walltime to >= {threshold}s "
                    f"(historical {label} for {gpu_type} on this profile/cluster)."
                ),
                evidence={
                    "requested_walltime_sec": requested,
                    "quantile_label": label,
                    "quantile_sec": threshold,
                    "n_samples": bucket.get("n_samples", 0),
                    "gpu_type": gpu_type,
                },
            )
        )
    return findings


def _known_bad_findings(
    *,
    gpu_type: str | None,
    workload_tags: list[str],
    rules: tuple[KnownBadCombination, ...],
) -> list[ValidatorFinding]:
    if gpu_type is None or not workload_tags:
        return []
    findings: list[ValidatorFinding] = []
    for rule in rules:
        if rule.gpu == gpu_type and rule.workload_tag in workload_tags:
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity=rule.severity,
                    code="known_bad_combination",
                    message=(
                        f"({gpu_type!r}, {rule.workload_tag!r}) is recorded "
                        f"as known-bad in playbook.yaml: {rule.reason}"
                    ),
                    suggested_fix=(
                        f"Either pick a different GPU type or remove the "
                        f"{rule.workload_tag!r} workload tag for this campaign."
                    ),
                    evidence={
                        "gpu_type": gpu_type,
                        "workload_tag": rule.workload_tag,
                    },
                )
            )
    return findings


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def validate_walltime_against_history(
    experiment_dir: Path,
    *,
    spec: ValidateWalltimeAgainstHistorySpec,
) -> ValidateWalltimeAgainstHistoryResult:
    """Cross-reference requested walltime against runtime priors + playbook.

    Returns findings; empty list = pass.

    Common ``code`` values:

    * ``playbook_parse_error`` — ``.hpc/playbook.yaml`` is malformed.
    * ``cold_start_no_history`` — no samples for (profile, cluster, gpu);
      info-level, no walltime check possible.
    * ``walltime_below_quantile`` — requested below a configured quantile
      (default: p95).
    * ``known_bad_combination`` — (gpu, workload_tag) matches a playbook
      entry; severity inherited from the rule.
    """
    findings: list[ValidatorFinding] = []
    try:
        playbook: Playbook = load_playbook(experiment_dir)
    except ValueError as exc:
        return ValidateWalltimeAgainstHistoryResult(
            findings=[
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="playbook_parse_error",
                    message=str(exc),
                    suggested_fix="Fix .hpc/playbook.yaml syntax.",
                    evidence={"path": str(experiment_dir / ".hpc" / "playbook.yaml")},
                )
            ]
        )

    walltime_rules = playbook.walltime_rules or _DEFAULT_WALLTIME_RULES

    from hpc_agent.state.runtime_prior import roll_up_quantiles  # noqa: PLC0415 — lazy

    rollup = roll_up_quantiles(experiment_dir, profile=spec.profile, cluster=spec.cluster)

    if rollup.get("needs_canary") and not (rollup.get("quantiles") or {}):
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="info",
                code="cold_start_no_history",
                message=(
                    f"No runtime samples for ({spec.profile!r}, {spec.cluster!r}); "
                    "walltime quantile check skipped. The submission will produce "
                    "the first samples."
                ),
                evidence={"profile": spec.profile, "cluster": spec.cluster},
            )
        )

    findings.extend(
        _walltime_findings(
            rollup,
            gpu_type=spec.gpu_type,
            requested=spec.requested_walltime_sec,
            rules=walltime_rules,
        )
    )
    findings.extend(
        _known_bad_findings(
            gpu_type=spec.gpu_type,
            workload_tags=list(spec.workload_tags),
            rules=playbook.known_bad_combinations,
        )
    )
    return ValidateWalltimeAgainstHistoryResult(findings=findings)
