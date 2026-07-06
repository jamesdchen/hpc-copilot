"""Semantic-parity table for the asyncssh engine's failure classification.

The engine replaces the one-shot path's stderr-text classifiers with typed
exceptions. That swap is only safe if every exception maps onto the SAME
breaker outcome the stderr marker it replaces produces today — the ban-safety
machinery keys on this distinction:

* ``ssh_circuit._CONNECTION_FAILURE_MARKERS`` counts ONLY connection-level
  evidence toward the breaker (refused / reset / banner-or-kex teardown /
  unreachable). Auth failures, host-key mismatches, and DNS failures are
  DELIBERATELY absent — they prove the host accepted (or never saw) a
  connection, the opposite of ban-risk evidence, and today they RESET the
  counter via ``record_connection_success``.
* A naive port that recorded ``PermissionDenied`` as a breaker failure would
  make a bad key walk the host's circuit open — a semantic regression no
  stderr-marker test would catch. This table pins each mapping.

Command-time failures on an ESTABLISHED connection (wedge / channel death)
record NOTHING — phase-1 broker precedent (``ssh_broker._Pool.run`` discards
the channel and raises without touching the breaker). The one-shot path counts
a raised TimeoutError as connection evidence only because a subprocess cannot
distinguish a connect-hang from a command-hang; the engine can, and a command
hang on a live session is not connection evidence. Deliberate, documented
deviation — the fallback one-shot still records honestly if the host is sick.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

asyncssh = pytest.importorskip("asyncssh")
# The engine module ships alongside this table; importorskip (not a plain
# import) so lint/type checks stay green while the module is in flight.
ssh_engine = pytest.importorskip("hpc_agent.infra.ssh_engine")


def _no_route_oserror() -> OSError:
    import errno

    return OSError(errno.EHOSTUNREACH, "No route to host")


def _unreachable_oserror() -> OSError:
    import errno

    return OSError(errno.ENETUNREACH, "Network is unreachable")


# (exception factory, expected class, one-shot stderr analog it replaces)
# expected class: "throttle" = connection-level evidence, feeds
# record_connection_failure exactly like its marker; "fatal" = reached the
# host (or never resolved it), records success exactly like today's
# non-marker stderr.
_CONNECT_CLASSIFICATION_TABLE = [
    pytest.param(
        lambda: asyncio.TimeoutError(),
        "throttle",
        "timed out during banner exchange",  # the MaxStartups signature
        id="connect-timeout-banner-withheld",
    ),
    pytest.param(
        lambda: ConnectionRefusedError(),
        "throttle",
        "connection refused",
        id="connection-refused",
    ),
    pytest.param(
        lambda: ConnectionResetError(),
        "throttle",
        "connection reset by peer",
        id="connection-reset",
    ),
    pytest.param(
        lambda: asyncssh.ConnectionLost("Connection lost"),
        "throttle",
        "kex_exchange_identification: connection closed",
        id="pre-auth-teardown",
    ),
    pytest.param(
        _no_route_oserror,
        "throttle",
        "no route to host",
        id="no-route",
    ),
    pytest.param(
        _unreachable_oserror,
        "throttle",
        "network is unreachable",
        id="network-unreachable",
    ),
    pytest.param(
        lambda: socket.gaierror(8, "nodename nor servname provided"),
        "fatal",
        "could not resolve hostname (NOT a marker today)",
        id="dns-failure-resets-counter",
    ),
    pytest.param(
        lambda: asyncssh.PermissionDenied(reason="no more auth methods"),
        "fatal",
        "permission denied (deliberately NOT a marker: host accepted us)",
        id="auth-failure-never-walks-the-breaker",
    ),
    pytest.param(
        lambda: asyncssh.HostKeyNotVerifiable("host key mismatch"),
        "fatal",
        "host key verification failed (NOT a marker today)",
        id="host-key-mismatch-resets-counter",
    ),
]


@pytest.mark.parametrize(("make_exc", "expected", "_analog"), _CONNECT_CLASSIFICATION_TABLE)
def test_connect_exception_classification_parity(make_exc, expected, _analog):
    """Each connect-time exception classifies as its stderr analog does today."""
    assert ssh_engine.classify_engine_failure(make_exc()) == expected


def test_every_connection_failure_marker_has_an_engine_analog():
    """The engine's throttle class must cover the one-shot marker set.

    If a new marker is added to ``ssh_circuit._CONNECTION_FAILURE_MARKERS``
    without an engine-side exception mapping, the engine path silently stops
    counting that failure class toward the breaker. This cross-references the
    two so the sets can only drift loudly.
    """
    from hpc_agent.infra.ssh_circuit import _CONNECTION_FAILURE_MARKERS

    covered = {
        # marker → the exception class the engine sees instead
        "connection refused": ConnectionRefusedError,
        "connection reset by peer": ConnectionResetError,
        "connection timed out": asyncio.TimeoutError,
        "timed out during banner exchange": asyncio.TimeoutError,
        "operation timed out": asyncio.TimeoutError,
        "no route to host": OSError,
        "network is unreachable": OSError,
        "ssh_exchange_identification: connection closed": asyncssh.ConnectionLost,
        "kex_exchange_identification: connection closed": asyncssh.ConnectionLost,
        "kex_exchange_identification: read: connection reset": asyncssh.ConnectionLost,
    }
    assert set(_CONNECTION_FAILURE_MARKERS) == set(covered), (
        "connection-failure marker set changed — update the engine "
        "classification table (and classify_engine_failure) to match"
    )
    for marker, exc_cls in covered.items():
        if exc_cls is OSError:
            exc: BaseException = _no_route_oserror()
        elif exc_cls is asyncssh.ConnectionLost:
            exc = asyncssh.ConnectionLost("Connection lost")
        else:
            exc = exc_cls()
        assert ssh_engine.classify_engine_failure(exc) == "throttle", (
            f"engine must count {exc_cls.__name__} toward the breaker (replaces marker {marker!r})"
        )
