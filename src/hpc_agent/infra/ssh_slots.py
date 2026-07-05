"""Cross-process per-host SSH connection-slot limiter — the burst-prevention guard.

The 2026-07-05 incident (proving run #4): a doctor re-arm spawned several
detached workers whose startup SSH probes, plus the driving agent's own
probes, all hit one login node *simultaneously*. Each call was individually
well-behaved (bounded timeout, breaker-guarded), but the fleet's **concurrent
burst** looked like an intrusion/MaxStartups pattern and the host dropped the
lot — the circuit breaker then (correctly) opened over an outage the fleet
had self-inflicted. The breaker bounds *consecutive failures*; the
``safe_interval`` throttle (:mod:`hpc_agent.infra.ssh_throttle`) bounds
*frequency within one process* and is off by default. Nothing bounded
**cross-process concurrency**. This module is that bound.

Mechanism — N claimable slots per host, shared across processes:

* Before any ssh-family subprocess is spawned, the caller claims one of
  :func:`resolve_max_connections` (default :data:`DEFAULT_MAX_CONNECTIONS`)
  per-host slot files under ``<journal home>/_ssh_throttle/`` (sibling of the
  breaker's ``_ssh_circuit/``; journal home =
  :func:`hpc_agent.state.run_record._current_homedir`, so ``HPC_JOURNAL_DIR``
  redirection and the test suite's isolation apply).
* A claim is a lock-free **atomic exclusive create** (``O_CREAT|O_EXCL``) of
  ``<host>.slot<i>`` — the hot path with a free slot costs one syscall and
  takes no lock. The advisory lock is taken only on contention, and only to
  serialize *reclaiming* a stale slot.
* **Slot-hold approximation:** the slot is held for the WHOLE subprocess
  call, spawn to exit, not just the connect/auth phase. The connect window
  cannot be observed from outside the ssh child, and whole-call holding
  over-approximates (never under-counts) concurrent connection attempts —
  the cheapest correct approximation. Cost: two long transfers to one host
  serialize a third; with N=2 that is acceptable pacing, and short control
  commands (the storm pattern) dominate.
* Waiters back off with a **deterministic pid-derived jitter** (no
  ``random``; testable via injected clock/sleep) on a doubling poll
  interval, and give up after :data:`SLOT_WAIT_MAX_SEC` with
  :class:`hpc_agent.errors.SshSlotWaitTimeout` — bounded by construction,
  never a new wedge class. While waiting they re-consult the circuit
  breaker: a circuit that opens turns the whole queue into fail-fast
  (an open breaker must not accumulate queued attempts).
* A slot whose claimant crashed is reclaimed when its pid is no longer
  alive (fast path, same-box) or its claim is older than
  :data:`SLOT_TTL_SEC` (backstop; sized above the worst legitimate hold,
  ``RSYNC_TIMEOUT_SEC=1800`` default). Normal releases are ``finally``
  unlinks, so only a hard kill leaks a slot.

Fail-open posture (same as the breaker): a broken state dir, an unwritable
slot file, a corrupt claim doc — none of it may block SSH. Every OSError
degrades to "limiter inactive for this call"; a malformed slot file reads
as stale (reclaimable). The limiter is a protection layer, not a
correctness gate.

Override: ``HPC_SSH_MAX_CONNECTIONS`` — an integer per-host slot count.
``0`` disables the limiter entirely; unset/empty means the default; a
negative or non-numeric value warns to stderr and falls back to the
default (a typo must not silently disable burst protection).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from typing import TYPE_CHECKING

from hpc_agent.errors import SshSlotWaitTimeout

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

__all__ = [
    "DEFAULT_MAX_CONNECTIONS",
    "SLOT_JITTER_MAX_SEC",
    "SLOT_POLL_BASE_SEC",
    "SLOT_POLL_MAX_SEC",
    "SLOT_TTL_SEC",
    "SLOT_WAIT_MAX_SEC",
    "acquire_slot",
    "connection_slot",
    "release_slot",
    "resolve_max_connections",
    "slot_paths",
]

#: Concurrent connection attempts allowed per host, fleet-wide.
DEFAULT_MAX_CONNECTIONS = 2

#: A waiter gives up (raises :class:`SshSlotWaitTimeout`) this long after it
#: started waiting. Bounded on purpose: an unbounded queue is the wedge class
#: the 2026-07-04 preflight probe already demonstrated.
SLOT_WAIT_MAX_SEC = 120.0

#: First poll interval for a waiter (doubles up to :data:`SLOT_POLL_MAX_SEC`).
SLOT_POLL_BASE_SEC = 1.0

#: Poll-interval ceiling for waiters.
SLOT_POLL_MAX_SEC = 8.0

#: Maximum pid-derived jitter added to each waiter's poll interval, so a
#: fleet that starts waiting together re-polls staggered instead of in
#: lockstep. Derived from the pid (deterministic, testable) — not random.
SLOT_JITTER_MAX_SEC = 1.0

#: Backstop staleness bound: a claim older than this is reclaimable even if
#: its pid looks alive (e.g. pid reuse, or a foreign machine's pid on a
#: shared journal home). Sized above the worst legitimate whole-call hold
#: (``RSYNC_TIMEOUT_SEC`` defaults to 1800s). The pid-liveness check below
#: reclaims a crashed claimant's slot much sooner on the common same-box case.
SLOT_TTL_SEC = 2100.0

#: Env var overriding the per-host slot count (``0`` disables the limiter).
MAX_CONNECTIONS_ENV = "HPC_SSH_MAX_CONNECTIONS"

# Prime modulus for the pid→jitter map: spreads consecutive pids (workers
# spawned back-to-back get near-consecutive pids) across the jitter range.
_JITTER_MOD = 997


def resolve_max_connections() -> int:
    """Per-host concurrent-connection slot count; ``0`` disables the limiter.

    Reads :data:`MAX_CONNECTIONS_ENV` (int). Unset/empty →
    :data:`DEFAULT_MAX_CONNECTIONS`. ``0`` → disabled (explicit opt-out).
    Negative or non-numeric warns to stderr and uses the default — unlike
    the opt-in ``safe_interval`` throttle, this guard is on by default, so
    a typo must degrade to "still guarded", not "silently unlimited".
    """
    raw = os.environ.get(MAX_CONNECTIONS_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_CONNECTIONS
    try:
        val = int(raw)
    except ValueError:
        val = -1
    if val < 0:
        print(
            f"hpc-agent: ignoring {MAX_CONNECTIONS_ENV}={raw!r} (not a non-negative "
            f"integer); using the default of {DEFAULT_MAX_CONNECTIONS}",
            file=sys.stderr,
        )
        return DEFAULT_MAX_CONNECTIONS
    return val


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* (``user@host`` or a bare alias) — identical
    normalization to :func:`hpc_agent.infra.ssh_circuit._host` /
    :func:`hpc_agent.infra.ssh_throttle._host`, so all three guards key alike."""
    return ssh_target.rsplit("@", 1)[-1].strip()


def _safe_name(host: str) -> str:
    """Filesystem-safe filename component for *host* (mirrors ssh_circuit)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def _slot_dir() -> Path:
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_ssh_throttle"


def slot_paths(host: str, limit: int | None = None) -> list[Path]:
    """The slot files for *host* (test/introspection seam)."""
    if limit is None:
        limit = resolve_max_connections()
    base = _slot_dir()
    safe = _safe_name(host)
    return [base / f"{safe}.slot{i}" for i in range(limit)]


def _reclaim_lock_path(host: str) -> Path:
    return _slot_dir() / f"{_safe_name(host)}.slots.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort same-box liveness for *pid*; ``True`` when unsure.

    POSIX: signal-0 probe (EPERM still means alive). win32:
    ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` via ctypes —
    ``os.kill(pid, 0)`` on Windows calls ``TerminateProcess`` and must
    never be used as a probe. Any doubt reads as alive: a false "alive"
    only delays reclaim until :data:`SLOT_TTL_SEC`; a false "dead" would
    over-admit.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes  # noqa: PLC0415 — win32-only probe

        process_query_limited_information = 0x1000
        try:
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information, False, pid
            )
        except OSError:
            return True
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
            return True
        # NULL handle: dead pid → ERROR_INVALID_PARAMETER (87); access
        # denied (5) means it exists. Anything unexpected reads as alive.
        last_error: int = ctypes.windll.kernel32.GetLastError()  # type: ignore[attr-defined]
        return last_error != 87
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _claim_is_stale(
    path: Path,
    *,
    now: float,
    pid_alive: Callable[[int], bool],
) -> bool:
    """True when the slot file at *path* belongs to a dead or expired claim.

    A malformed/unreadable claim doc reads as stale (fail-open: a corrupt
    slot file must not permanently eat a slot).
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # Already released between our create-failure and this check.
        return True
    except (OSError, ValueError):
        return True
    if not isinstance(doc, dict):
        return True
    try:
        claimed_at = float(doc.get("claimed_at"))  # type: ignore[arg-type]
        pid = int(doc.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    if now - claimed_at > SLOT_TTL_SEC:
        return True
    return not pid_alive(pid)


def _create_claim(path: Path, *, host: str, pid: int, now: float) -> bool:
    """Atomically create the slot file at *path*; ``False`` if already held.

    The exclusive create IS the claim — the doc written right after is
    metadata for staleness checks, not part of the atomicity story.
    """
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        payload = json.dumps(
            {"schema_version": 1, "host": host, "pid": pid, "claimed_at": now}
        ).encode("utf-8")
        os.write(fd, payload)
    finally:
        os.close(fd)
    return True


def _try_claim(
    host: str,
    limit: int,
    *,
    pid: int,
    now: float,
    pid_alive: Callable[[int], bool],
) -> Path | None:
    """One pass over the host's slots: claim a free (or stale) one, or ``None``.

    Free-slot hot path: the first ``O_CREAT|O_EXCL`` succeeds — no lock, no
    read. A held slot is checked for staleness with a lock-free read; only
    an actual reclaim serializes under the advisory lock (contention-only
    locking). OSErrors other than the exists-collision propagate to the
    caller's fail-open handler.
    """
    base = _slot_dir()
    base.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(host)
    stale: list[Path] = []
    for i in range(limit):
        path = base / f"{safe}.slot{i}"
        if _create_claim(path, host=host, pid=pid, now=now):
            return path
        if _claim_is_stale(path, now=now, pid_alive=pid_alive):
            stale.append(path)
    if not stale:
        return None
    # Contention with at least one stale claim: reclaim under the lock so a
    # fleet of waiters doesn't all unlink+create the same slot at once.
    from hpc_agent.infra.io import advisory_flock

    with advisory_flock(_reclaim_lock_path(host)):
        for path in stale:
            # Re-check under the lock: a peer may have reclaimed it already,
            # and the new claim must not be stolen.
            if not _claim_is_stale(path, now=now, pid_alive=pid_alive):
                continue
            with contextlib.suppress(OSError):
                path.unlink()
            if _create_claim(path, host=host, pid=pid, now=now):
                return path
    return None


def acquire_slot(
    ssh_target: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], object] = time.sleep,
    pid: int | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> Path | None:
    """Claim one per-host connection slot, waiting (bounded) if all are held.

    Returns the claimed slot file's path (pass to :func:`release_slot`), or
    ``None`` when the limiter is disabled / the host is empty / the state
    dir is broken (fail-open — the call proceeds unguarded rather than
    blocking SSH on limiter breakage).

    Waiters poll with a doubling interval plus a deterministic pid-derived
    jitter, re-consult the circuit breaker each round (an OPEN circuit
    raises :class:`~hpc_agent.errors.SshCircuitOpen` immediately — waiters
    must not stay queued against a host the fleet already knows is down),
    and give up with :class:`~hpc_agent.errors.SshSlotWaitTimeout` once
    :data:`SLOT_WAIT_MAX_SEC` has elapsed. *clock* / *sleep* / *pid* /
    *pid_alive* are injectable for tests (wall clock: state is
    cross-process, so ``time.monotonic`` cannot be shared).
    """
    limit = resolve_max_connections()
    host = _host(ssh_target)
    if limit <= 0 or not host:
        return None
    if pid is None:
        pid = os.getpid()
    jitter = (pid % _JITTER_MOD) / _JITTER_MOD * SLOT_JITTER_MAX_SEC
    start = clock()
    deadline = start + SLOT_WAIT_MAX_SEC
    interval = SLOT_POLL_BASE_SEC
    announced = False
    while True:
        try:
            token = _try_claim(host, limit, pid=pid, now=clock(), pid_alive=pid_alive)
        except OSError:
            return None  # fail open: limiter breakage must never block SSH
        if token is not None:
            return token
        now = clock()
        if now >= deadline:
            raise SshSlotWaitTimeout(
                f"gave up waiting for an ssh connection slot to host '{host}' after "
                f"{SLOT_WAIT_MAX_SEC:.0f}s: all {limit} per-host slots stayed held "
                f"(burst prevention: at most {limit} concurrent connection attempts "
                f"per host, fleet-wide). Slot state lives under "
                f"<journal home>/_ssh_throttle/. Raise or disable the cap with "
                f"{MAX_CONNECTIONS_ENV}=<n> (0 disables)."
            )
        # An open breaker fails the waiter fast instead of leaving it queued.
        from hpc_agent.infra.ssh_circuit import check_circuit

        check_circuit(ssh_target, clock=clock)
        if not announced:
            print(
                f"hpc-agent: all {limit} ssh connection slots to {host} are held — "
                f"waiting (bounded, ≤{SLOT_WAIT_MAX_SEC:.0f}s) for one to free",
                file=sys.stderr,
                flush=True,
            )
            announced = True
        sleep(min(interval + jitter, max(0.0, deadline - now)))
        interval = min(interval * 2.0, SLOT_POLL_MAX_SEC)


def release_slot(token: Path | None) -> None:
    """Release a slot claimed by :func:`acquire_slot` (``None`` is a no-op)."""
    if token is None:
        return
    with contextlib.suppress(OSError):
        token.unlink()


@contextlib.contextmanager
def connection_slot(
    ssh_target: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], object] = time.sleep,
    pid: int | None = None,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> Iterator[Path | None]:
    """Hold one per-host connection slot for the duration of the block.

    The ``finally`` release runs on success AND on any exception from the
    guarded call (a timed-out ssh must free its slot for the next waiter).
    """
    token = acquire_slot(ssh_target, clock=clock, sleep=sleep, pid=pid, pid_alive=pid_alive)
    try:
        yield token
    finally:
        release_slot(token)
