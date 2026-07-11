"""Tests for the decide-concurrency switch."""

from __future__ import annotations

from hpc_agent.meta.campaign.atoms.decide_concurrency import decide_concurrency


def test_no_async_support_resolves_sequential_in_code() -> None:
    out = decide_concurrency(supports_async=False, remaining_jobs=100, in_flight=0)
    assert out["decided_by"] == "code"
    assert out["decision"] == "sequential"
    assert out["max_in_flight"] == 1


def test_no_headroom_resolves_sequential_in_code() -> None:
    # campaign-budget's remaining ALREADY nets in-flight, so genuinely-no-headroom
    # is remaining_jobs == 0 (bug-sweep #67).
    out = decide_concurrency(supports_async=True, remaining_jobs=0, in_flight=3)
    assert out["decided_by"] == "code"
    assert out["decision"] == "sequential"


def test_remaining_already_nets_in_flight_boundary() -> None:
    """The exact double-count boundary: remaining=4 with 4 in flight is STILL
    affordable (remaining already subtracted them), so it must escalate — not
    resolve sequential 'no headroom' (bug-sweep #67)."""
    out = decide_concurrency(supports_async=True, remaining_jobs=4, in_flight=4, k_cap=8)
    assert out["decided_by"] == "judgement"
    # headroom = 4 (NOT 4-4=0) → bound min(8, 4) = 4
    assert out["max_in_flight"] == 4


def test_async_with_headroom_escalates_the_aggressiveness() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=10, in_flight=2, k_cap=4)
    assert out["decided_by"] == "judgement"
    assert out["decision"] is None
    # safe bound = min(k_cap=4, headroom=10) = 4
    assert out["max_in_flight"] == 4
    assert set(out["candidates"]) == {"sequential", "parallel"}


def test_headroom_below_cap_bounds_the_offer() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=2, in_flight=2, k_cap=8)
    assert out["decided_by"] == "judgement"
    # headroom = remaining=2 < k_cap=8 → bound 2 (in_flight is NOT re-subtracted)
    assert out["max_in_flight"] == 2


def test_unbounded_budget_uses_k_cap() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=None, in_flight=0, k_cap=4)
    assert out["decided_by"] == "judgement"
    assert out["max_in_flight"] == 4
