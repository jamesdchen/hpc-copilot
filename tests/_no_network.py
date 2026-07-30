"""A tripwire that turns "this code opens no connection" into a checked claim.

The readiness ledger's whole contract is **harvest, never probe**: it stores what
the system already learned and must never itself dial, spawn, or resolve. Prose
cannot hold that line — the 2026-07-30 adversarial review mutated
``state/readiness`` and ``ssh_circuit._feed_readiness`` to open a socket and to
spawn a subprocess, and **both mutations survived the whole suite**. Nothing was
watching. This is what watches.

Usage — one autouse fixture per test module that exercises a no-network path::

    from tests._no_network import no_network  # noqa: F401  (autouse fixture)

Why it raises a :class:`BaseException` subclass, not ``AssertionError``: every
layer under test is deliberately, totally fail-open (``except Exception`` around
the feed, around ``record_observation``, around the reducer) so that a broken
ledger can never perturb SSH. That same swallow eats an ``AssertionError`` raised
from inside a patched socket, so an ``assert``-based tripwire is silently
defeated by the code it is meant to police — the review proved exactly this.
:class:`NetworkAttempted` inherits from ``BaseException`` so it passes straight
through every ``except Exception`` in the call path and fails the test loudly.

Scope: the outbound primitives a "probe" would have to go through — socket
creation/connection, name resolution, and subprocess spawning (an ``ssh``/``rsync``
child is a network call wearing a different hat). It does not try to be a sandbox;
it is a guard against the specific regression of a storage/reducer layer quietly
growing a dial.
"""

from __future__ import annotations

import socket
import subprocess
from typing import TYPE_CHECKING, Any, NoReturn

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["NetworkAttempted", "no_network"]


class NetworkAttempted(BaseException):
    """Raised when a no-network code path tried to reach the outside world.

    ``BaseException``, not ``Exception``, ON PURPOSE — see the module docstring:
    the layers this guards are fail-open by design and would swallow anything
    catchable, turning the tripwire into a no-op.
    """


def _forbid(what: str) -> Any:
    def _raise(*args: object, **kwargs: object) -> NoReturn:
        raise NetworkAttempted(
            f"{what} was attempted on a path that promises to open no connection "
            f"(args={args!r}). The readiness ledger harvests what the system "
            f"already learned; SENSING belongs to infra/readiness_sensors.py."
        )

    return _raise


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail the test if anything in it dials out or spawns a child process.

    Autouse: import it into a module and every test there is covered, including
    tests added later by someone who never read this file — which is the point.
    """
    monkeypatch.setattr(socket, "socket", _forbid("socket.socket()"))
    monkeypatch.setattr(socket, "create_connection", _forbid("socket.create_connection()"))
    monkeypatch.setattr(socket, "getaddrinfo", _forbid("socket.getaddrinfo()"))
    monkeypatch.setattr(subprocess, "Popen", _forbid("subprocess.Popen()"))
    monkeypatch.setattr(subprocess, "run", _forbid("subprocess.run()"))
    monkeypatch.setattr(subprocess, "check_output", _forbid("subprocess.check_output()"))
    yield
