"""Tests for ``validate-self-qos-limit``.

Pure local primitive — no I/O — so tests can be parametric and dense.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.validators.validate_self_qos_limit import (
    ValidateSelfQosLimitSpec,
)
from hpc_agent.ops.validate.self_qos_limit import validate_self_qos_limit

if TYPE_CHECKING:
    from pathlib import Path


def _spec(**overrides) -> ValidateSelfQosLimitSpec:
    base = dict(
        profile="ml_ridge",
        cluster="discovery",
        new_array_size=10,
        current_user_pending_count=0,
        qos_max_jobs_per_user=100,
    )
    base.update(overrides)
    return ValidateSelfQosLimitSpec(**base)


# ─── pass regime ──────────────────────────────────────────────────────


def test_well_under_cap_no_findings(tmp_path: Path) -> None:
    out = validate_self_qos_limit(tmp_path, spec=_spec(new_array_size=5))
    assert out.findings == []


# ─── warn regime ──────────────────────────────────────────────────────


def test_at_70_pct_emits_warning(tmp_path: Path) -> None:
    """Default warn_at_pct=0.7; (70 + 0) >= 70 fires the warning."""
    out = validate_self_qos_limit(
        tmp_path,
        spec=_spec(new_array_size=70),
    )
    finding = next(f for f in out.findings if f.code == "qos_max_jobs_near_limit")
    assert finding.severity == "warning"
    assert finding.evidence["predicted_total"] == 70
    assert finding.evidence["fraction_of_cap"] == 0.7


def test_existing_pendings_count_toward_warn(tmp_path: Path) -> None:
    """Existing 60 + new 15 = 75 → at 75% of cap → warn."""
    out = validate_self_qos_limit(
        tmp_path,
        spec=_spec(current_user_pending_count=60, new_array_size=15),
    )
    finding = next(f for f in out.findings if f.code == "qos_max_jobs_near_limit")
    assert finding.severity == "warning"
    assert finding.evidence["predicted_total"] == 75


# ─── error regime (the self-DOS case) ─────────────────────────────────


def test_at_or_above_cap_emits_error(tmp_path: Path) -> None:
    """Predicted == cap is the actual DOS boundary — every additional
    pending pushes past."""
    out = validate_self_qos_limit(
        tmp_path,
        spec=_spec(new_array_size=100),
    )
    finding = next(f for f in out.findings if f.code == "qos_max_jobs_exceeded")
    assert finding.severity == "error"
    assert finding.suggested_fix is not None
    assert "Split" in finding.suggested_fix or "split" in finding.suggested_fix


def test_existing_plus_new_exceeds_cap(tmp_path: Path) -> None:
    """The headline lesson-6 bug class: 50 existing + 100-task array
    blows through the 100-cap."""
    out = validate_self_qos_limit(
        tmp_path,
        spec=_spec(current_user_pending_count=50, new_array_size=100),
    )
    finding = next(f for f in out.findings if f.code == "qos_max_jobs_exceeded")
    assert finding.severity == "error"
    assert finding.evidence["predicted_total"] == 150
    assert finding.evidence["qos_max_jobs_per_user"] == 100


@pytest.mark.parametrize(
    "current,new,cap,expected_severity",
    [
        # exactly-cap and beyond → error
        (0, 100, 100, "error"),
        (50, 50, 100, "error"),
        (99, 2, 100, "error"),
        # warn band with default warn_at_pct=0.7
        (70, 0, 100, "warning"),
        (50, 25, 100, "warning"),
        # below warn threshold → pass (no findings)
        (10, 10, 100, "pass"),
        (0, 1, 100, "pass"),
    ],
)
def test_threshold_table(
    tmp_path: Path,
    current: int,
    new: int,
    cap: int,
    expected_severity: str,
) -> None:
    """Pin the exact threshold semantics so a future "let me round
    differently" change can't silently alter the boundaries."""
    if new < 1:
        pytest.skip("new_array_size has Field(ge=1); spec rejects new=0 by design")
    out = validate_self_qos_limit(
        tmp_path,
        spec=_spec(
            current_user_pending_count=current,
            new_array_size=new,
            qos_max_jobs_per_user=cap,
        ),
    )
    if expected_severity == "pass":
        assert out.findings == []
    else:
        assert any(f.severity == expected_severity for f in out.findings)
