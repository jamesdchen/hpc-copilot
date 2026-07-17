"""Fault-injection harness — shared fixtures + injection vocabulary.

Step 3 of the transport-robustness sequence
(``docs/plans/transport-robustness-2026-07-17/``). The AUDIT (step 1) ends with
a fault-injection test-point inventory (§7): each seam, when a channel is
SEVERED / HUNG / GARBLED mid-op, exercises a distinct failure path. This
directory drills those paths and asserts the DOCTRINE outcome, never an
implementation detail:

* a severed channel RAISES or degrades to UNKNOWN — never a default/settled
  verdict (the F3 rule: "severed → UNKNOWN, never zero-rows");
* a torn / truncated stream is REFUSED (positive-evidence ack absent) — never
  parsed as a valid empty result;
* every breaker / slot / deadline actually FIRES.

See ``FAULT-HARNESS.md`` for the fixture vocabulary, the coverage table against
the audit inventory, and how step-2 units extend this suite.

Fixture vocabulary
------------------
``sever_at(target, ...)``   — patch a seam so it RAISES a transport exception
                              (``ConnectionError`` / ``OSError``) — a channel
                              that dropped mid-op. ``after_n_calls`` lets the
                              first N calls pass through (a mid-pull sever).
``hang_at(target, seconds)``— patch a seam so it BLOCKS for *seconds* — for a
                              seam whose caller enforces a Python-level deadline
                              (a pump-thread join, a ``future.result`` backstop).
                              Deliberately SHORT, test-tuned waits — never a real
                              300 s / 1800 s deadline. (The OS-pipe deadline of
                              ``run_capture_bounded`` is enforced by the kernel,
                              not a Python callable, so THAT drill uses a real
                              short-lived sleeper subprocess instead — see
                              ``sleeper_argv``.)
``garble_at(target, ...)``  — patch a seam so it RETURNS a truncated / garbage
                              value (an rc-0 read with NO ack line; a version
                              string that doesn't parse) — the "looks clean but
                              is torn" case.

All three are context-managed: the patch is torn down when the test ends.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hpc_agent.infra import ssh_options

# ---------------------------------------------------------------------------
# ssh-probe cache warm discipline (copied rationale from tests/infra/conftest.py)
# ---------------------------------------------------------------------------
# ``ssh_options._local_openssh_supports_gcm`` / ``_windows_openssh_named_pipe_supported``
# are ``functools.cache``-protected and lazily fire ``subprocess.run(["ssh", "-V"])``
# once per process. Fault-injection tests patch ``subprocess.run`` / ``subprocess.Popen``
# at MANY module seams; those patches land on the global ``subprocess`` module, so a
# COLD version probe that fires inside a ``with patch(...):`` window ends up in the
# mock's call list — bumping ``call_count`` assertions or, worse (the 661a6ca7 CI
# double-red), raising ``ValueError: not enough values to unpack`` when a MagicMock
# ``Popen`` is iterated by the real ``subprocess.run``. Warming BOTH probes against the
# REAL subprocess module before every test means no probe ever lands inside an
# injection window, whatever ran before this test in the xdist worker. Any new test
# added here inherits the protection automatically.


@pytest.fixture(autouse=True)
def _warm_ssh_version_probe_cache() -> None:
    """Pre-warm BOTH cached ``ssh -V`` probes before each test (see module docstring)."""
    ssh_options._local_openssh_supports_gcm()
    if sys.platform == "win32":
        ssh_options._windows_openssh_named_pipe_supported()


# ---------------------------------------------------------------------------
# Injection fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sever_at() -> Iterator[Callable[..., MagicMock]]:
    """Patch a seam so it RAISES a transport-level exception (a severed channel).

    ``install(target, *, exc=ConnectionError, message=..., after_n_calls=0,
    passthrough=None)`` patches dotted-path *target*. By default every call
    raises *exc(message)*. ``after_n_calls`` lets the first N calls succeed
    (returning ``passthrough(*a, **k)`` if given, else ``None``) before the
    sever — the "drop mid-transfer after N batches" shape. Returns the installed
    mock so a test can assert on how many times the seam was reached.
    """
    stack = ExitStack()

    def install(
        target: str,
        *,
        exc: type[BaseException] = ConnectionError,
        message: str = "fault-injection: severed channel",
        after_n_calls: int = 0,
        passthrough: Callable[..., Any] | None = None,
    ) -> MagicMock:
        state = {"n": 0}

        def side_effect(*a: Any, **k: Any) -> Any:
            if state["n"] < after_n_calls:
                state["n"] += 1
                return passthrough(*a, **k) if passthrough is not None else None
            raise exc(message)

        return stack.enter_context(patch(target, side_effect=side_effect))

    yield install
    stack.close()


@pytest.fixture
def hang_at() -> Iterator[Callable[..., MagicMock]]:
    """Patch a seam so it BLOCKS for *seconds* (a hung op the caller must bound).

    ``install(target, *, seconds)`` — a SHORT, test-tuned block (never a real
    deadline). Use only for seams whose caller enforces a Python-level bound (a
    pump-thread ``join(timeout=...)``, a ``future.result(timeout=...)`` backstop);
    the drill asserts the caller REAPS/RAISES rather than wedging.
    """
    stack = ExitStack()

    def install(target: str, *, seconds: float) -> MagicMock:
        def side_effect(*a: Any, **k: Any) -> None:
            time.sleep(seconds)

        return stack.enter_context(patch(target, side_effect=side_effect))

    yield install
    stack.close()


@pytest.fixture
def garble_at() -> Iterator[Callable[..., MagicMock]]:
    """Patch a seam so it RETURNS a truncated / garbage value (looks clean, is torn).

    ``install(target, *, return_value=None, side_effect=None)`` — pass
    *return_value* for a single garbled result (an rc-0 ``CompletedProcess`` with
    no ack line), or *side_effect* for a sequence. Returns the installed mock.
    """
    stack = ExitStack()

    def install(
        target: str,
        *,
        return_value: Any = None,
        side_effect: Any = None,
    ) -> MagicMock:
        if side_effect is not None:
            return stack.enter_context(patch(target, side_effect=side_effect))
        return stack.enter_context(patch(target, return_value=return_value))

    yield install
    stack.close()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable wall clock — no real time passes.

    Starts at the REAL epoch: breaker/slot state is read back by production
    paths that gate on ``time.time()``, and a 1970 ``opened_at`` would look
    long-expired. ``sleep`` returns a callable that advances the clock, so a
    bounded-wait deadline can be driven to expiry without a real sleep.
    """

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def sleep(self, seconds: float) -> None:
        """A ``sleep`` drop-in that advances the fake clock instead of blocking."""
        self.advance(seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    """A fresh :class:`FakeClock` per test."""
    return FakeClock()


def proc(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` — the shape ``remote.ssh_run`` returns."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def sleeper_argv(seconds: float = 30.0) -> list[str]:
    """Argv for a real child that sleeps *seconds* — the honest hang for the
    OS-pipe deadline of ``run_capture_bounded`` (whose bound the kernel enforces,
    not a Python callable, so it cannot be driven by ``hang_at``)."""
    return [sys.executable, "-c", f"import time; time.sleep({float(seconds)})"]
