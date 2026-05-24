"""Tests for ``hpc_agent._kernel.lifecycle.playbook``.

Covers the four contracts:

1. Missing file → empty defaults Playbook (every validator path
   short-circuits cleanly).
2. Empty / null file → empty defaults.
3. Well-formed sections round-trip into typed dataclass instances.
4. Malformed YAML / schema violation raises ``ValueError`` with a
   descriptive message — the calling validator surfaces it as one
   error finding rather than crashing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.lifecycle.playbook import (
    KnownBadCombination,
    Playbook,
    WalltimeRule,
    load_playbook,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, body: str) -> None:
    target = tmp_path / ".hpc" / "playbook.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


# ─── empty / missing ───────────────────────────────────────────────────


def test_missing_file_returns_empty_playbook(tmp_path: Path) -> None:
    pb = load_playbook(tmp_path)
    assert pb == Playbook()
    assert pb.known_bad_combinations == ()
    assert pb.walltime_rules == ()


def test_empty_file_returns_empty_playbook(tmp_path: Path) -> None:
    _write(tmp_path, "")
    assert load_playbook(tmp_path) == Playbook()


def test_null_yaml_document_returns_empty_playbook(tmp_path: Path) -> None:
    _write(tmp_path, "null\n")
    assert load_playbook(tmp_path) == Playbook()


# ─── known_bad_combinations ────────────────────────────────────────────


def test_known_bad_combinations_round_trip(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            workload_tag: attn-fp32
            severity: error
            reason: V100 fp32 attention is numerically unstable
          - gpu: a100
            workload_tag: bf16-on-pre-ampere
            severity: warning
            reason: deprecated combo
        """,
    )
    pb = load_playbook(tmp_path)
    assert pb.known_bad_combinations == (
        KnownBadCombination(
            gpu="v100",
            workload_tag="attn-fp32",
            severity="error",
            reason="V100 fp32 attention is numerically unstable",
        ),
        KnownBadCombination(
            gpu="a100",
            workload_tag="bf16-on-pre-ampere",
            severity="warning",
            reason="deprecated combo",
        ),
    )


def test_known_bad_combinations_missing_required_key_raises(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            severity: error
        """,
    )
    with pytest.raises(ValueError, match="missing required key 'workload_tag'"):
        load_playbook(tmp_path)


def test_known_bad_combinations_invalid_severity_raises(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            workload_tag: x
            severity: catastrophic
            reason: oops
        """,
    )
    with pytest.raises(ValueError, match="severity must be one of error/warning/info"):
        load_playbook(tmp_path)


# ─── walltime_rules ────────────────────────────────────────────────────


def test_walltime_rules_round_trip(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
        walltime_rules:
          - below_quantile: 0.95
            severity: warning
            message: under p95 — increase walltime
          - below_quantile: 0.5
            severity: error
            message: under p50 — definitely too short
        """,
    )
    pb = load_playbook(tmp_path)
    assert pb.walltime_rules == (
        WalltimeRule(
            below_quantile=0.95, severity="warning", message="under p95 — increase walltime"
        ),
        WalltimeRule(
            below_quantile=0.5, severity="error", message="under p50 — definitely too short"
        ),
    )


def test_walltime_rules_default_severity_is_warning(tmp_path: Path) -> None:
    """When severity is omitted, default to warning — most rules are
    advisory, not hard blocks."""
    _write(
        tmp_path,
        """
        walltime_rules:
          - below_quantile: 0.9
            message: heads up
        """,
    )
    pb = load_playbook(tmp_path)
    assert pb.walltime_rules[0].severity == "warning"


@pytest.mark.parametrize("bad_q", [-0.1, 1.5, 99.0])
def test_walltime_rules_quantile_must_be_in_closed_unit_interval(
    tmp_path: Path, bad_q: float
) -> None:
    _write(
        tmp_path,
        f"""
        walltime_rules:
          - below_quantile: {bad_q}
            message: x
        """,
    )
    with pytest.raises(ValueError, match="must be in"):
        load_playbook(tmp_path)


@pytest.mark.parametrize("ok_q", [0.0, 0.5, 1.0])
def test_walltime_rules_quantile_endpoints_are_allowed(tmp_path: Path, ok_q: float) -> None:
    # BUG-1-12: 0.0 and 1.0 are now accepted (closed interval). Edge
    # cases historically rejected by the (0, 1)-open check.
    _write(
        tmp_path,
        f"""
        walltime_rules:
          - below_quantile: {ok_q}
            message: x
        """,
    )
    pb = load_playbook(tmp_path)
    assert pb.walltime_rules[0].below_quantile == ok_q


# ─── malformed YAML ────────────────────────────────────────────────────


def test_malformed_yaml_raises_value_error(tmp_path: Path) -> None:
    _write(tmp_path, "{this is not: yaml: at all")
    with pytest.raises(ValueError, match="parse error"):
        load_playbook(tmp_path)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    _write(tmp_path, "- a list at the top level\n- second item")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        load_playbook(tmp_path)
