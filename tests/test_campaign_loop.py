"""Tests for the asyncio campaign loop in ``hpc_mapreduce.campaign.loop``.

The loop is fully IO-injected (``submit_one``, ``await_completion``,
``should_submit``) so these tests exercise every concurrency / stopping
behaviour without SSH or scheduler involvement.

Sync test functions call ``asyncio.run`` on async helpers — keeps the
test runtime stdlib-only (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from hpc_mapreduce.campaign.loop import CampaignResult, run_campaign


def _id_factory():
    counter = 0

    async def submit() -> str:
        nonlocal counter
        counter += 1
        return f"run_{counter:04d}"

    return submit


# ---------------------------------------------------------------------------
# Termination semantics
# ---------------------------------------------------------------------------


def test_stops_when_should_submit_returns_false_immediately() -> None:
    """An immediately-False predicate yields zero submits and a clean exit."""
    events: list[dict] = []

    async def driver() -> CampaignResult:
        return await run_campaign(
            concurrency=4,
            submit_one=_id_factory(),
            await_completion=lambda _rid: asyncio.sleep(0),
            should_submit=lambda: False,
            on_event=events.append,
        )

    result = asyncio.run(driver())
    assert isinstance(result, CampaignResult)
    assert result.iterations_submitted == 0
    assert result.iterations_completed == 0
    assert result.terminated_reason == "tasks_exhausted"
    assert events == [{"event": "stopped", "reason": "tasks_exhausted"}]


def test_runs_until_predicate_flips_false_then_drains_in_flight() -> None:
    """Predicate stops returning True after N submits; loop drains the queue."""
    submit = _id_factory()
    n_target = 5
    counter = {"n": 0}

    def gate() -> bool:
        if counter["n"] >= n_target:
            return False
        counter["n"] += 1
        return True

    async def driver() -> CampaignResult:
        return await run_campaign(
            concurrency=2,
            submit_one=submit,
            await_completion=lambda _rid: asyncio.sleep(0),
            should_submit=gate,
            on_event=None,
        )

    result = asyncio.run(driver())
    assert result.iterations_submitted == n_target
    assert result.iterations_completed == n_target
    assert result.terminated_reason == "tasks_exhausted"


# ---------------------------------------------------------------------------
# Concurrency invariant
# ---------------------------------------------------------------------------


def test_concurrency_cap_is_respected() -> None:
    """At no point may more than `concurrency` iterations be in flight."""
    submit = _id_factory()
    state = {"in_flight": 0, "high_water": 0}
    n_target = 12
    counter = {"n": 0}

    async def slow_completion(_rid: str) -> None:
        state["in_flight"] += 1
        state["high_water"] = max(state["high_water"], state["in_flight"])
        # Yield so the loop has a chance to top up before we resolve.
        await asyncio.sleep(0)
        state["in_flight"] -= 1

    def gate() -> bool:
        if counter["n"] >= n_target:
            return False
        counter["n"] += 1
        return True

    cap = 3

    async def driver() -> CampaignResult:
        return await run_campaign(
            concurrency=cap,
            submit_one=submit,
            await_completion=slow_completion,
            should_submit=gate,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == n_target
    assert state["high_water"] <= cap, (
        f"in-flight peaked at {state['high_water']}, exceeds cap {cap}"
    )


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------


def test_iteration_failure_is_surfaced_but_loop_continues() -> None:
    """A single iteration's exception lands as an event with `error`; the
    loop continues to drain its queue."""
    submit = _id_factory()
    counter = {"n": 0}
    events: list[dict] = []

    async def maybe_fail(rid: str) -> None:
        if rid == "run_0002":
            raise RuntimeError("simulated cluster failure")

    def gate() -> bool:
        if counter["n"] >= 3:
            return False
        counter["n"] += 1
        return True

    async def driver() -> CampaignResult:
        return await run_campaign(
            concurrency=1,
            submit_one=submit,
            await_completion=maybe_fail,
            should_submit=gate,
            on_event=events.append,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == 3
    failures = [e for e in events if e.get("error")]
    assert len(failures) == 1
    assert failures[0]["run_id"] == "run_0002"
    assert "RuntimeError" in failures[0]["error"]


# ---------------------------------------------------------------------------
# Wall-clock budget
# ---------------------------------------------------------------------------


def test_wall_clock_budget_stops_new_submits_then_drains() -> None:
    """When the budget elapses mid-loop, in-flight iterations finish but
    no new ones are launched. terminated_reason flips to wall_clock_budget."""
    submit = _id_factory()

    async def driver() -> CampaignResult:
        completion_done = asyncio.Event()

        async def hold(_rid: str) -> None:
            await completion_done.wait()

        async def trigger_release() -> None:
            await asyncio.sleep(0.1)
            completion_done.set()

        release_task = asyncio.create_task(trigger_release())
        try:
            return await run_campaign(
                concurrency=2,
                submit_one=submit,
                await_completion=hold,
                should_submit=lambda: True,  # would loop forever without budget
                wall_clock_budget_seconds=0.05,
            )
        finally:
            release_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await release_task

    result = asyncio.run(driver())
    assert result.terminated_reason == "wall_clock_budget"
    assert result.iterations_submitted == 2
    assert result.iterations_completed == 2


# ---------------------------------------------------------------------------
# Async should_submit
# ---------------------------------------------------------------------------


def test_should_submit_can_be_async() -> None:
    """The predicate accepts both sync and async callables."""
    submit = _id_factory()
    counter = {"n": 0}

    async def gate() -> bool:
        if counter["n"] >= 2:
            return False
        counter["n"] += 1
        return True

    async def driver() -> CampaignResult:
        return await run_campaign(
            concurrency=1,
            submit_one=submit,
            await_completion=lambda _rid: asyncio.sleep(0),
            should_submit=gate,
        )

    result = asyncio.run(driver())
    assert result.iterations_completed == 2


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_concurrency_must_be_at_least_one() -> None:
    async def driver():
        await run_campaign(
            concurrency=0,
            submit_one=_id_factory(),
            await_completion=lambda _rid: asyncio.sleep(0),
            should_submit=lambda: True,
        )

    with pytest.raises(ValueError, match="concurrency"):
        asyncio.run(driver())
