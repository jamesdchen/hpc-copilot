"""Tests for the decide-concurrency switch."""

from __future__ import annotations

from hpc_agent.meta.campaign.atoms.decide_concurrency import decide_concurrency


def test_no_async_support_resolves_sequential_in_code() -> None:
    out = decide_concurrency(supports_async=False, remaining_jobs=100, in_flight=0)
    assert out["decided_by"] == "code"
    assert out["decision"] == "sequential"
    assert out["max_in_flight"] == 1


def test_no_headroom_resolves_sequential_in_code() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=3, in_flight=3)
    assert out["decided_by"] == "code"
    assert out["decision"] == "sequential"


def test_async_with_headroom_escalates_the_aggressiveness() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=10, in_flight=2, k_cap=4)
    assert out["decided_by"] == "judgement"
    assert out["decision"] is None
    # safe bound = min(k_cap=4, headroom=8) = 4
    assert out["max_in_flight"] == 4
    assert set(out["candidates"]) == {"sequential", "parallel"}


def test_headroom_below_cap_bounds_the_offer() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=4, in_flight=2, k_cap=8)
    assert out["decided_by"] == "judgement"
    # headroom = 2 < k_cap=8 → bound 2
    assert out["max_in_flight"] == 2


def test_unbounded_budget_uses_k_cap() -> None:
    out = decide_concurrency(supports_async=True, remaining_jobs=None, in_flight=0, k_cap=4)
    assert out["decided_by"] == "judgement"
    assert out["max_in_flight"] == 4
