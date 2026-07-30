"""The zero-network tripwire's own fire path.

Every lint rule in this repo must demonstrate its fire path against a synthetic
violation (``docs/internals/engineering-principles.md``); a test-only guard earns
the same standard. These are the cases the 2026-07-30 adversarial review used to
prove the ABSENCE of a guard — a socket opened and a subprocess spawned inside
the readiness feed, both surviving the whole suite. They must now fail loudly.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from tests._no_network import NetworkAttempted, no_network  # noqa: F401


def test_opening_a_socket_fires() -> None:
    with pytest.raises(NetworkAttempted):
        socket.socket()


def test_create_connection_fires() -> None:
    with pytest.raises(NetworkAttempted):
        socket.create_connection(("example.invalid", 22))


def test_name_resolution_fires() -> None:
    with pytest.raises(NetworkAttempted):
        socket.getaddrinfo("example.invalid", 22)


def test_spawning_a_subprocess_fires() -> None:
    """An ``ssh``/``rsync`` child is a network call wearing a different hat."""
    with pytest.raises(NetworkAttempted):
        subprocess.run(["ssh", "h", "true"], check=False, timeout=5)
    with pytest.raises(NetworkAttempted):
        subprocess.Popen(["rsync", "-a", "x", "y"])


def test_the_tripwire_is_not_swallowed_by_a_fail_open_except_Exception() -> None:
    """THE point of the BaseException base class.

    Every layer it guards wraps its work in ``except Exception`` so a broken
    ledger can never perturb SSH. An ``AssertionError``-based tripwire is eaten by
    exactly that swallow and silently proves nothing — which is how two live
    mutations survived the suite. Reproduce the swallow and show it does not
    catch this.
    """
    caught_by_fail_open = False
    try:
        try:
            socket.socket()
        except Exception:  # noqa: BLE001 — deliberately reproducing the fail-open
            caught_by_fail_open = True
    except NetworkAttempted:
        pass
    else:  # pragma: no cover — only reachable if the guard regressed
        raise AssertionError("the tripwire did not escape a fail-open except Exception")
    assert not caught_by_fail_open


def test_ordinary_work_is_unaffected() -> None:
    """The guard bounds outbound calls, not the test's own file/CPU work."""
    assert sum(range(10)) == 45
