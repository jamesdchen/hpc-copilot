"""Tests for the decide-resubmit policy switch.

Encodes hpc-status Step 6: 0 failures → complete; failed_fraction <=
threshold → resubmit (boundary inclusive); failed_fraction > threshold →
escalate with safe_default "investigate"; total_tasks < 1 → SpecInvalid.
The default threshold is 0.0 — every failure escalates unless the caller
opts into auto-resubmit by passing a threshold > 0.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops.decide_resubmit import decide_resubmit


def test_no_failures_is_complete() -> None:
    out = decide_resubmit(failed_count=0, total_tasks=100)
    assert out["action"] == "complete"
    assert out["failed_fraction"] == 0.0
    assert out["safe_default"] is None


def test_any_failure_escalates_under_default_threshold() -> None:
    # Default threshold is 0.0: a single failure already escalates —
    # auto-resubmit is an explicit caller opt-in, never the default.
    out = decide_resubmit(failed_count=1, total_tasks=100)
    assert out["action"] == "escalate"
    assert out["threshold"] == 0.0
    assert out["safe_default"] == "investigate"


def test_below_opted_in_threshold_resubmits() -> None:
    # 5 / 100 = 0.05, below the caller's explicit 0.10 threshold.
    out = decide_resubmit(failed_count=5, total_tasks=100, resubmit_failed_threshold=0.10)
    assert out["action"] == "resubmit"
    assert out["safe_default"] is None


def test_at_threshold_resubmits_boundary_inclusive() -> None:
    # failed_fraction == threshold (10 / 100 == 0.10) must still resubmit.
    out = decide_resubmit(failed_count=10, total_tasks=100, resubmit_failed_threshold=0.10)
    assert out["failed_fraction"] == 0.10
    assert out["failed_fraction"] == out["threshold"]
    assert out["action"] == "resubmit"
    assert out["safe_default"] is None


def test_above_threshold_escalates_with_investigate_default() -> None:
    # 11 / 100 = 0.11, above the caller's explicit 0.10 threshold.
    out = decide_resubmit(failed_count=11, total_tasks=100, resubmit_failed_threshold=0.10)
    assert out["action"] == "escalate"
    assert out["safe_default"] == "investigate"


def test_failed_fraction_is_computed() -> None:
    out = decide_resubmit(failed_count=5, total_tasks=100)
    assert out["failed_fraction"] == 0.05  # 5 / 100
    assert out["failed_count"] == 5
    assert out["total_tasks"] == 100


def test_custom_threshold_shifts_the_boundary() -> None:
    # At a 0.25 threshold, 0.20 resubmits where the default would escalate.
    out = decide_resubmit(failed_count=20, total_tasks=100, resubmit_failed_threshold=0.25)
    assert out["failed_fraction"] == 0.20
    assert out["threshold"] == 0.25
    assert out["action"] == "resubmit"


def test_zero_total_tasks_is_spec_invalid() -> None:
    with pytest.raises(errors.SpecInvalid, match="total_tasks must be >= 1"):
        decide_resubmit(failed_count=0, total_tasks=0)
