"""Reader for ``.hpc/playbook.yaml`` — project-specific validator rules.

The playbook is the per-project escape hatch for the campaign
validator: rules that are too project-specific to live in framework
code (V100 + attn-fp32 unstable) but that the agent loop should know
about. It's read by ``validate_walltime_against_history`` and
(eventually) other validators that want project-tuned thresholds.

Schema (every section is optional):

.. code-block:: yaml

    known_bad_combinations:
      - gpu: v100
        workload_tag: attn-fp32
        severity: error
        reason: "V100 fp32 attention is numerically unstable"

    walltime_rules:
      - below_quantile: 0.95
        severity: warning
        message: "Requested walltime is below historical p95"

The reader returns ``Playbook(known_bad_combinations=[...],
walltime_rules=[...])``; an absent file or empty document yields a
defaults-empty Playbook. Malformed YAML raises — the validator
surfaces it as a single ``error`` finding rather than crashing the
whole workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class KnownBadCombination:
    gpu: str
    workload_tag: str
    severity: Literal["error", "warning", "info"]
    reason: str


@dataclass(frozen=True)
class WalltimeRule:
    below_quantile: float
    severity: Literal["error", "warning", "info"]
    message: str


@dataclass(frozen=True)
class Playbook:
    known_bad_combinations: tuple[KnownBadCombination, ...] = ()
    walltime_rules: tuple[WalltimeRule, ...] = ()


_DEFAULT_PLAYBOOK = Playbook()


def _coerce_severity(raw: Any) -> Literal["error", "warning", "info"]:
    if raw not in ("error", "warning", "info"):
        raise ValueError(f"playbook severity must be one of error/warning/info; got {raw!r}")
    return cast("Literal['error', 'warning', 'info']", raw)


def _parse_known_bad_combinations(raw: Any) -> tuple[KnownBadCombination, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"known_bad_combinations must be a list, got {type(raw).__name__}")
    out: list[KnownBadCombination] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"known_bad_combinations[{i}] must be a mapping")
        try:
            out.append(
                KnownBadCombination(
                    gpu=str(entry["gpu"]),
                    workload_tag=str(entry["workload_tag"]),
                    severity=_coerce_severity(entry["severity"]),
                    reason=str(entry.get("reason", "")),
                )
            )
        except KeyError as exc:
            raise ValueError(
                f"known_bad_combinations[{i}] missing required key {exc.args[0]!r}"
            ) from None
    return tuple(out)


def _parse_walltime_rules(raw: Any) -> tuple[WalltimeRule, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"walltime_rules must be a list, got {type(raw).__name__}")
    out: list[WalltimeRule] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"walltime_rules[{i}] must be a mapping")
        try:
            q = float(entry["below_quantile"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"walltime_rules[{i}].below_quantile must be a float") from exc
        if not 0.0 <= q <= 1.0:
            raise ValueError(f"walltime_rules[{i}].below_quantile must be in [0, 1]; got {q}")
        out.append(
            WalltimeRule(
                below_quantile=q,
                severity=_coerce_severity(entry.get("severity", "warning")),
                message=str(entry.get("message", "Requested walltime is below historical bound.")),
            )
        )
    return tuple(out)


def load_playbook(experiment_dir: Path) -> Playbook:
    """Load ``<experiment_dir>/.hpc/playbook.yaml``.

    Missing file → defaults-empty Playbook (every validator path
    short-circuits cleanly). Malformed YAML or schema violation
    raises ``ValueError`` so the calling validator can surface it
    as one ``error`` finding.
    """
    path = experiment_dir / ".hpc" / "playbook.yaml"
    if not path.is_file():
        return _DEFAULT_PLAYBOOK
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"playbook.yaml parse error: {exc}") from exc
    if raw is None:
        return _DEFAULT_PLAYBOOK
    if not isinstance(raw, dict):
        raise ValueError(f"playbook.yaml top-level must be a mapping, got {type(raw).__name__}")
    return Playbook(
        known_bad_combinations=_parse_known_bad_combinations(raw.get("known_bad_combinations")),
        walltime_rules=_parse_walltime_rules(raw.get("walltime_rules")),
    )
