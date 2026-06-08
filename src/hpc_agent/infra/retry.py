"""The single "retry-as-data" surface for the control-flow-out-of-LLM work.

The control loop should never narrate a retry in prose and never ask the
LLM to read stderr and *decide* whether to try again. A retry is a
**value**: a :class:`RetryPolicy` (how many attempts, the backoff
schedule, which exceptions are retryable) that :func:`run_with_retry`
applies in plain code. The policy is the data; the runner is the
mechanism; the model is out of the loop.

:func:`hpc_agent.infra.remote._with_ssh_backoff` (delays ``2s/4s/8s/16s``,
i.e. ``base_delay=2.0`` × ``backoff_factor=2.0``) is built on this surface
(#308): it adapts its two retry triggers — a raised ``TimeoutError`` and a
throttle-marked ``CompletedProcess`` — onto :func:`run_with_retry` via
``_ssh_backoff_policy``, so the exponential schedule lives here as data
rather than as a hand-rolled loop.

Note the ``_attempt(...)`` retry inside :mod:`hpc_agent.infra.transport` is
**not** a backoff target: it is wrapped by
:func:`hpc_agent.infra.ssh_options.run_with_named_pipe_retry`, a one-shot,
condition-specific retry (sticky verdict, no sleep, no exponential schedule)
for a single Windows named-pipe ``getsockname`` failure. It is a different
mechanism, not a delay loop, and is intentionally left as-is.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["RetryPolicy", "run_with_retry"]

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """A retry expressed as data, applied by :func:`run_with_retry`.

    Fields
    ------
    max_attempts:
        Total number of attempts (NOT retries) — ``1`` disables retrying.
    base_delay_sec:
        Delay before the *first* retry, in seconds. The schedule grows
        from here by :attr:`backoff_factor`.
    backoff_factor:
        Multiplier applied per attempt. ``2.0`` (the default) yields the
        ``base/2·base/4·base…`` doubling schedule the infra ssh/rsync
        retries already use (delays ``2s/4s/8s/16s`` at the defaults).
    max_delay_sec:
        Optional ceiling each computed delay is capped at. ``None``
        (default) leaves the exponential schedule uncapped.
    retry_on:
        Exception types that are retryable. The empty tuple (default)
        means "retry on any :class:`Exception`"; a non-empty tuple
        restricts retrying to its members (anything else propagates
        immediately).

    A retry's delay is :meth:`delay_for`; the loop that consumes it lives
    in :func:`run_with_retry`.
    """

    max_attempts: int = 3
    base_delay_sec: float = 2.0
    backoff_factor: float = 2.0
    max_delay_sec: float | None = None
    retry_on: tuple[type[BaseException], ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay_sec < 0:
            raise ValueError(f"base_delay_sec must be >= 0, got {self.base_delay_sec}")
        if self.backoff_factor < 1:
            raise ValueError(f"backoff_factor must be >= 1, got {self.backoff_factor}")

    def delay_for(self, attempt: int) -> float:
        """Seconds to sleep before retrying, for a 1-based *attempt*.

        ``base_delay_sec * backoff_factor ** (attempt - 1)``, so
        ``attempt=1`` yields ``base_delay_sec`` and each later attempt
        multiplies by :attr:`backoff_factor`. When :attr:`max_delay_sec`
        is set, the result is capped at it.
        """
        delay = self.base_delay_sec * self.backoff_factor ** (attempt - 1)
        if self.max_delay_sec is not None:
            delay = min(delay, self.max_delay_sec)
        return delay


def run_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run *fn*, retrying retryable failures per *policy*.

    Calls ``fn()`` up to ``policy.max_attempts`` times. When an attempt
    raises an exception that *policy* considers retryable —
    ``isinstance(exc, policy.retry_on)`` if :attr:`~RetryPolicy.retry_on`
    is non-empty, otherwise any :class:`Exception` — the runner sleeps
    ``policy.delay_for(attempt)`` (via the injected *sleep*) and tries
    again. After the final attempt is exhausted it re-raises the **last**
    exception. A non-retryable exception is never caught: it propagates
    immediately, with no sleep and no further attempts.

    Parameters
    ----------
    fn:
        Zero-arg thunk performing the work and returning its result.
    policy:
        The :class:`RetryPolicy` value driving attempts and backoff.
    sleep:
        Injectable sleeper — defaults to :func:`time.sleep`; tests pass a
        recorder so no real time elapses.
    on_retry:
        Optional callback fired *before* each sleep, as
        ``on_retry(attempt, exc, delay)`` (1-based attempt number, the
        exception that triggered the retry, the delay about to be slept).
        Never fired for the final (exhausting) attempt or for a
        non-retryable exception.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            # Non-retryable: not an instance of the allow-list (when one
            # is given). Propagate immediately — no sleep, no retry.
            if policy.retry_on and not isinstance(exc, policy.retry_on):
                raise
            last_exc = exc
            # Last attempt exhausted: re-raise the failure we just caught
            # rather than sleeping for a retry that will never happen.
            if attempt == policy.max_attempts:
                raise
            delay = policy.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleep(delay)
    # Unreachable: the loop either returns, or re-raises on the final
    # attempt. Present so static analysis sees every path accounted for.
    assert last_exc is not None  # noqa: S101 - invariant guard
    raise last_exc
