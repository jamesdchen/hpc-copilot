"""Tests for the decide-partial-handling switch."""

from __future__ import annotations

from hpc_agent.ops.aggregate.decide_partial_handling import decide_partial_handling


def test_no_failed_waves_proceeds_in_code() -> None:
    out = decide_partial_handling(failed_count=0, combined_count=10)
    assert out["decided_by"] == "code"
    assert out["decision"] == "proceed"


def test_retries_remaining_resolves_retry_in_code() -> None:
    out = decide_partial_handling(failed_count=2, combined_count=8, retries_exhausted=False)
    assert out["decided_by"] == "code"
    assert out["decision"] == "retry"


def test_exhausted_partial_escalates_with_missing_fraction() -> None:
    out = decide_partial_handling(failed_count=2, combined_count=8, retries_exhausted=True)
    assert out["decided_by"] == "judgement"
    assert out["decision"] is None
    assert out["missing_fraction"] == 0.2  # 2 / (2 + 8)
    assert set(out["candidates"]) == {"accept-partial", "force-retry-failed"}


def test_missing_fraction_is_computed() -> None:
    out = decide_partial_handling(failed_count=1, combined_count=3, retries_exhausted=True)
    assert out["missing_fraction"] == 0.25
