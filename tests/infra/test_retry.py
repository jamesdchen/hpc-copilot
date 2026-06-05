"""Tests for ``hpc_agent.infra.retry`` — the retry-as-data surface.

A retry is a :class:`RetryPolicy` value applied by :func:`run_with_retry`,
not an inline loop. These tests pin the runner's contract:

- success on the first attempt never sleeps,
- a transient failure is retried with the policy's backoff delays, in
  order, then succeeds,
- exhausting ``max_attempts`` re-raises the *last* exception,
- an exception outside ``retry_on`` propagates immediately, with no sleep,
- :meth:`RetryPolicy.delay_for` backoff math + the ``max_delay_sec`` cap,
- the ``on_retry`` callback fires with ``(attempt, exc, delay)``,
- ``__post_init__`` validation rejects bad fields.

All sleeping is via an injected recorder — no real time elapses.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.retry import RetryPolicy, run_with_retry


class _Recorder:
    """A drop-in for ``sleep`` that records the delays it was asked for."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def test_succeeds_on_first_try_never_sleeps() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        return "ok"

    result = run_with_retry(fn, policy=RetryPolicy(), sleep=sleeper)

    assert result == "ok"
    assert calls["n"] == 1
    assert sleeper.delays == []


def test_fails_twice_then_succeeds_sleeps_with_backoff_delays_in_order() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"transient {calls['n']}")
        return "done"

    policy = RetryPolicy(max_attempts=5, base_delay_sec=2.0, backoff_factor=2.0)
    result = run_with_retry(fn, policy=policy, sleep=sleeper)

    assert result == "done"
    assert calls["n"] == 3
    # Two failures -> two sleeps, with attempt-1 and attempt-2 delays.
    assert sleeper.delays == [2.0, 4.0]


def test_exhausts_max_attempts_and_reraises_last_exception() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        raise RuntimeError(f"boom {calls['n']}")

    policy = RetryPolicy(max_attempts=3, base_delay_sec=1.0, backoff_factor=2.0)
    with pytest.raises(RuntimeError, match="boom 3"):
        run_with_retry(fn, policy=policy, sleep=sleeper)

    assert calls["n"] == 3
    # Sleeps only between attempts: after attempt 1 and attempt 2, not 3.
    assert sleeper.delays == [1.0, 2.0]


def test_non_retryable_exception_propagates_immediately_with_no_sleep() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        raise ValueError("not retryable")

    policy = RetryPolicy(max_attempts=5, retry_on=(KeyError,))
    with pytest.raises(ValueError, match="not retryable"):
        run_with_retry(fn, policy=policy, sleep=sleeper)

    # Tried exactly once; no retry, no sleep.
    assert calls["n"] == 1
    assert sleeper.delays == []


def test_retry_on_allows_listed_exception() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise KeyError("listed")
        return "recovered"

    policy = RetryPolicy(max_attempts=3, base_delay_sec=2.0, retry_on=(KeyError,))
    result = run_with_retry(fn, policy=policy, sleep=sleeper)

    assert result == "recovered"
    assert calls["n"] == 2
    assert sleeper.delays == [2.0]


def test_retry_on_matches_subclasses() -> None:
    """``isinstance`` semantics: a subclass of a listed type is retryable."""
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise FileNotFoundError("subclass of OSError")
        return "ok"

    policy = RetryPolicy(max_attempts=3, base_delay_sec=1.0, retry_on=(OSError,))
    result = run_with_retry(fn, policy=policy, sleep=sleeper)

    assert result == "ok"
    assert calls["n"] == 2


def test_delay_for_backoff_math() -> None:
    policy = RetryPolicy(base_delay_sec=2.0, backoff_factor=2.0)
    # 1-based: base * factor ** (attempt - 1)
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    assert policy.delay_for(3) == 8.0
    assert policy.delay_for(4) == 16.0


def test_delay_for_respects_max_delay_cap() -> None:
    policy = RetryPolicy(base_delay_sec=2.0, backoff_factor=2.0, max_delay_sec=5.0)
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(2) == 4.0
    # 8.0 and 16.0 are clamped to the 5.0 ceiling.
    assert policy.delay_for(3) == 5.0
    assert policy.delay_for(4) == 5.0


def test_delay_for_non_default_backoff_factor() -> None:
    policy = RetryPolicy(base_delay_sec=1.0, backoff_factor=3.0)
    assert policy.delay_for(1) == 1.0
    assert policy.delay_for(2) == 3.0
    assert policy.delay_for(3) == 9.0


def test_on_retry_callback_fires_with_attempt_exc_delay() -> None:
    sleeper = _Recorder()
    events: list[tuple[int, str, float]] = []

    def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        events.append((attempt, str(exc), delay))

    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(f"fail {calls['n']}")
        return "ok"

    policy = RetryPolicy(max_attempts=4, base_delay_sec=2.0, backoff_factor=2.0)
    result = run_with_retry(fn, policy=policy, sleep=sleeper, on_retry=on_retry)

    assert result == "ok"
    # Fired before each of the two sleeps, with 1-based attempt + the
    # delay that was then slept.
    assert events == [(1, "fail 1", 2.0), (2, "fail 2", 4.0)]
    assert [e[2] for e in events] == sleeper.delays


def test_on_retry_not_fired_when_exhausted() -> None:
    """The final (exhausting) attempt does not fire ``on_retry``."""
    sleeper = _Recorder()
    events: list[int] = []

    def fn() -> str:
        raise RuntimeError("always")

    policy = RetryPolicy(max_attempts=2, base_delay_sec=1.0)
    with pytest.raises(RuntimeError, match="always"):
        run_with_retry(
            fn,
            policy=policy,
            sleep=sleeper,
            on_retry=lambda attempt, _exc, _delay: events.append(attempt),
        )

    # Only the first (retried) attempt fires the callback, not the second.
    assert events == [1]
    assert sleeper.delays == [1.0]


def test_max_attempts_one_disables_retry() -> None:
    sleeper = _Recorder()
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        raise RuntimeError("once")

    with pytest.raises(RuntimeError, match="once"):
        run_with_retry(fn, policy=RetryPolicy(max_attempts=1), sleep=sleeper)

    assert calls["n"] == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_attempts": 0}, "max_attempts"),
        ({"max_attempts": -1}, "max_attempts"),
        ({"base_delay_sec": -1.0}, "base_delay_sec"),
        ({"backoff_factor": 0.5}, "backoff_factor"),
        ({"backoff_factor": 0.0}, "backoff_factor"),
    ],
)
def test_post_init_validation_raises(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_valid_boundaries_accepted() -> None:
    """Boundary values that should be allowed (not rejected)."""
    # max_attempts=1, base_delay_sec=0, backoff_factor=1 are all valid.
    policy = RetryPolicy(max_attempts=1, base_delay_sec=0.0, backoff_factor=1.0)
    assert policy.delay_for(1) == 0.0
    assert policy.delay_for(5) == 0.0  # factor=1 -> constant schedule
