"""Asyncio in-flight queue for closed-loop campaigns.

``run_campaign`` is the framework's entire driver: it maintains
*concurrency* live submits, asks ``should_submit`` whether to launch
another iteration, awaits the next-finished one, and repeats until
either ``should_submit`` returns ``False`` (the user's ``tasks.py``
signals termination via ``total() == 0``) or a wall-clock budget is
exceeded.

The IO is fully injected so the loop is testable without SSH or a real
scheduler: callers pass ``submit_one`` (returns the ``run_id`` of a
freshly-launched iteration) and ``await_completion`` (resolves when a
given ``run_id`` reaches a terminal state). The CLI wrapper in
``cmd_campaign_run`` will wire these to the real
``runner.submit_and_record`` and ``runner.record_status``; tests pass
synchronous fakes.

Deliberate non-features:

* No driver-side reduce. The user's ``tasks.py`` calls
  :func:`hpc_mapreduce.reduce.history.prior` on its next invocation; the
  loop just notes completion.
* No retry logic. A failed iteration is reported via ``on_event`` and
  the loop continues; reissuing a failed iteration is the strategy
  library's call (the user's ``tasks.py`` can choose to re-propose the
  same params next iteration).
* No campaign-side state file. State, if any, is the strategy library's
  responsibility; the framework only persists run sidecars.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "CampaignResult",
    "run_campaign",
]


@dataclass
class CampaignResult:
    """Summary returned when ``run_campaign`` exits."""

    completed: list[str] = field(default_factory=list)
    """``run_id``\\ s that reached terminal state during this driver session."""

    terminated_reason: str = ""
    """One of: ``"tasks_exhausted"``, ``"wall_clock_budget"``, ``"cancelled"``."""

    iterations_submitted: int = 0
    iterations_completed: int = 0
    elapsed_seconds: float = 0.0


async def run_campaign(
    *,
    concurrency: int,
    submit_one: Callable[[], Awaitable[str]],
    await_completion: Callable[[str], Awaitable[None]],
    should_submit: Callable[[], Awaitable[bool] | bool],
    on_event: Callable[[dict[str, Any]], None] | None = None,
    wall_clock_budget_seconds: float | None = None,
) -> CampaignResult:
    """Run a closed-loop campaign with at most *concurrency* in-flight submits.

    Parameters
    ----------
    concurrency:
        Maximum live submits. Each ``submit_one`` call counts as one
        in-flight iteration; ``await_completion(run_id)`` retires it.
    submit_one:
        Async callable returning the ``run_id`` of the iteration it just
        launched. Called only when ``should_submit`` returns ``True`` and
        the in-flight count is below *concurrency*. Must not block on
        prior iterations completing — the loop's whole point is to
        keep K live.
    await_completion:
        Async callable that resolves (returns ``None``) when *run_id*
        reaches a terminal state on the cluster. The CLI wrapper backs
        this with periodic polling of ``runner.record_status``; tests
        pass deterministic stubs.
    should_submit:
        Sync or async predicate. Returning ``False`` tells the loop not
        to launch any more iterations. Real callers wire this to a
        re-import of the user's ``tasks.py`` and a check that
        ``total() > 0``; tests pass an in-memory counter.
    on_event:
        Optional callback for streaming events to stderr (one of
        ``submitted`` / ``completed`` / ``stopped`` / ``budget_exceeded``).
        The loop never relies on the callback's side effects, so a no-op
        is safe.
    wall_clock_budget_seconds:
        If set, the loop exits with ``terminated_reason="wall_clock_budget"``
        once this many seconds have elapsed since the call started. New
        submits stop being launched as soon as the budget is hit; the
        loop still drains in-flight iterations before returning so no
        iteration is silently abandoned.

    Returns
    -------
    CampaignResult
        Summary of the driver session — see field docstrings.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1; got {concurrency}")

    started_at = time.monotonic()
    in_flight: dict[asyncio.Task[None], str] = {}
    completed: list[str] = []
    submitted_count = 0
    terminated_reason = ""

    def _emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    async def _should_submit() -> bool:
        result = should_submit()
        if asyncio.iscoroutine(result):
            return bool(await result)
        return bool(result)

    def _budget_hit() -> bool:
        if wall_clock_budget_seconds is None:
            return False
        return (time.monotonic() - started_at) >= wall_clock_budget_seconds

    try:
        while True:
            # Top up in-flight unless the budget is exhausted or the
            # user's predicate says stop.
            while len(in_flight) < concurrency and not _budget_hit():
                if not await _should_submit():
                    break
                run_id = await submit_one()
                submitted_count += 1
                # ``await_completion`` may return any Awaitable; ensure
                # asyncio.create_task gets a Coroutine. ``ensure_future``
                # accepts both and gives us back a Task either way.
                task: asyncio.Task[None] = asyncio.ensure_future(await_completion(run_id))
                in_flight[task] = run_id
                _emit({"event": "submitted", "run_id": run_id})

            if not in_flight:
                if _budget_hit():
                    terminated_reason = "wall_clock_budget"
                    _emit({"event": "budget_exceeded"})
                else:
                    terminated_reason = "tasks_exhausted"
                    _emit({"event": "stopped", "reason": "tasks_exhausted"})
                break

            done, _ = await asyncio.wait(in_flight.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                run_id = in_flight.pop(task)
                # Surface task exceptions on the event channel so the
                # caller can react; the loop itself does not crash on a
                # single iteration's failure.
                exc = task.exception()
                if exc is not None:
                    _emit(
                        {
                            "event": "completed",
                            "run_id": run_id,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                else:
                    _emit({"event": "completed", "run_id": run_id})
                completed.append(run_id)
    except asyncio.CancelledError:
        terminated_reason = "cancelled"
        for task in in_flight:
            task.cancel()
        raise
    finally:
        elapsed = time.monotonic() - started_at

    return CampaignResult(
        completed=completed,
        terminated_reason=terminated_reason,
        iterations_submitted=submitted_count,
        iterations_completed=len(completed),
        elapsed_seconds=elapsed,
    )
