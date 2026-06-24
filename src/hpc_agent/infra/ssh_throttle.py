"""Per-host SSH connection-open throttle — the ``safe_interval`` ban-driver guard.

A cluster's fail2ban / connection-rate limiter counts how *often* an IP opens
SSH connections, not how long each one takes (that is ``ConnectTimeout``) nor
how many auth methods each one tries (that is ``IdentitiesOnly``). When
connections bunch up — the failure-retry storms and back-to-back probes/polls
that tripped a real ban — nothing else in the stack caps the *rate*.

This module enforces a minimum wall-clock interval between consecutive
ssh-family spawns to the same host. If calls are naturally spaced wider than the
interval it sleeps ~0; a burst is throttled to one open per interval. It is
thread-safe: concurrent submits to one host reserve staggered slots and so
serialize through the interval instead of all firing at once.

Modelled on AiiDA's ``safe_interval`` (default 30s between opening connections).
**Off by default here** (``HPC_SSH_SAFE_INTERVAL`` unset / ``0``): ControlMaster
multiplexing already collapses the happy path, and a non-zero global gap would
needlessly serialize every flow. Turn it on for a cluster that rate-limits, or
when multiplexing is unavailable. The structural follow-up — deriving the
interval from a per-cluster budget in ``clusters.yaml`` — is tracked separately.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

__all__ = ["reset_throttle_state", "resolve_safe_interval", "throttle_connection"]

# host -> monotonic timestamp of the slot the last gated spawn reserved.
_LAST_OPEN: dict[str, float] = {}
_LOCK = threading.Lock()


def resolve_safe_interval() -> float:
    """Seconds to enforce between connection opens to one host; ``0.0`` disables.

    Reads ``HPC_SSH_SAFE_INTERVAL`` (float seconds). Unset/empty → ``0.0``
    (disabled) silently. A negative or non-numeric value warns to stderr — a
    typo must not wedge every ssh call — and disables.
    """
    raw = os.environ.get("HPC_SSH_SAFE_INTERVAL")
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        val = float(raw)
    except ValueError:
        print(
            f"hpc-agent: ignoring HPC_SSH_SAFE_INTERVAL={raw!r} (not a number); "
            "connection throttle disabled",
            file=sys.stderr,
        )
        return 0.0
    if val < 0:
        print(
            f"hpc-agent: ignoring negative HPC_SSH_SAFE_INTERVAL={raw!r}; "
            "connection throttle disabled",
            file=sys.stderr,
        )
        return 0.0
    return val


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* (``user@host`` or a bare alias)."""
    return ssh_target.rsplit("@", 1)[-1].strip()


def throttle_connection(
    ssh_target: str,
    *,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """Block until ``safe_interval`` has elapsed since the last open to this host.

    Returns the seconds actually slept (``0.0`` when the throttle is disabled,
    the host is the first contact, or enough time has already passed).
    *sleep* / *clock* are injected for testing.

    The per-host slot is reserved under a lock *before* sleeping (the sleep
    itself happens outside the lock), so N callers that arrive together reserve
    ``t``, ``t+interval``, ``t+2·interval`` … and wake staggered rather than
    colliding — turning a burst into a paced sequence.
    """
    interval = resolve_safe_interval()
    host = _host(ssh_target)
    if interval <= 0.0 or not host:
        return 0.0
    with _LOCK:
        now = clock()
        last = _LAST_OPEN.get(host)
        wait = 0.0 if last is None else max(0.0, interval - (now - last))
        _LAST_OPEN[host] = now + wait
    if wait > 0.0:
        sleep(wait)
    return wait


def reset_throttle_state() -> None:
    """Clear all per-host timestamps (test seam)."""
    with _LOCK:
        _LAST_OPEN.clear()
