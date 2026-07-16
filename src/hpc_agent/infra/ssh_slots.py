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
  :func:`hpc_agent.state.run_record.current_homedir`, so ``HPC_JOURNAL_DIR``
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
* Waiters poll on a **flat sub-second interval** (P5, the 2026-07-16
  latency ruling): a fixed :data:`SLOT_POLL_BASE_SEC` cadence plus a
  **deterministic pid-derived jitter** (no ``random``; testable via injected
  clock/sleep), and give up after :data:`SLOT_WAIT_MAX_SEC` with
  :class:`hpc_agent.errors.SshSlotWaitTimeout` — bounded by construction,
  never a new wedge class. The cadence is *flat*, not a doubling back-off:
  a doubling ladder that grew to seconds meant a slot freed just after a
  poll was not noticed for the whole (grown) interval, so wakeup lag scaled
  with hold duration — 4–8s on a long hold. A flat poll bounds wakeup lag
  at :data:`SLOT_POLL_BASE_SEC` + :data:`SLOT_JITTER_MAX_SEC` (sub-second)
  **independent of how long the slot was held**; the poll is a local
  filesystem stat, not an SSH op, so a tight cadence adds no remote load.
  While waiting they re-consult the circuit
  breaker: a circuit that opens turns the whole queue into fail-fast
  (an open breaker must not accumulate queued attempts).
* A slot whose claimant crashed is reclaimed by PID-LIVENESS: the claim
  records its holder's pid, and a slot whose pid is no longer alive is
  reclaimable (:func:`hpc_agent.infra.proc.pid_alive`, the shared probe over
  psutil). There is NO wall-clock TTL expiry (the G4 library-lifecycle
  shrink, ruled 2026-07-12): a wall-clock lease below the worst legitimate
  hold is exactly what over-admitted a connection in bug-sweep #35, and a
  TTL is the hand-rolled lifecycle bookkeeping the ruling retires. The
  holder RELEASES its own slot (ownership-bound, below); a dead holder's
  slot is reaped on liveness alone. Normal releases are ``finally`` unlinks,
  so only a hard kill leaks a slot, and pid-liveness reclaims that on the
  next contended acquire.

  Scope of the pid-liveness reaper (honest limit): it assumes the claim's
  pid lives in the SAME pid namespace as the reader — true for the normal
  case (the journal home is local; the driving agent, detached workers, and
  mcp-serve are sibling processes on one box). A journal home shared across
  MACHINES (a foreign pid that is coincidentally alive locally) is outside
  the guard's reliable domain; there it degrades toward under-admission
  (a leaked foreign slot makes acquirers WAIT, never over-admit), bounded by
  the fail-open :data:`SLOT_WAIT_MAX_SEC` give-up — the safe direction for a
  burst limiter.

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
from hpc_agent.infra.proc import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

__all__ = [
    "DEFAULT_MAX_CONNECTIONS",
    "SLOT_DISCLOSE_FIRST_SEC",
    "SLOT_DISCLOSE_INTERVAL_SEC",
    "SLOT_JITTER_MAX_SEC",
    "SLOT_POLL_BASE_SEC",
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

#: Flat poll cadence for a waiter (P5, 2026-07-16 latency ruling). No
#: doubling back-off: a waiter re-checks for a freed slot every
#: :data:`SLOT_POLL_BASE_SEC` (+ jitter) for the whole wait, so wakeup lag
#: after a release is bounded by this + :data:`SLOT_JITTER_MAX_SEC`,
#: *independent of how long the slot was held*. Sub-second by construction:
#: ``SLOT_POLL_BASE_SEC + SLOT_JITTER_MAX_SEC < 0.6`` (pinned by a fire-path
#: test). Kept a local filesystem stat, not an SSH op, so a tight cadence
#: adds no load to the remote host the limiter protects.
SLOT_POLL_BASE_SEC = 0.3

#: Maximum pid-derived jitter added to each waiter's flat poll interval, so a
#: fleet that starts waiting together re-polls staggered instead of in
#: lockstep. Derived from the pid (deterministic, testable) — not random.
#: Bounded so ``SLOT_POLL_BASE_SEC + SLOT_JITTER_MAX_SEC`` stays sub-second.
SLOT_JITTER_MAX_SEC = 0.2

#: Seconds a blocked waiter waits before its FIRST periodic slot-hold
#: disclosure to stderr. F-N (run #10): a process blocked ~15 min acquiring a
#: slot emitted ZERO output and was indistinguishable from a hung/dead process
#: — it cost two diagnostic rounds. Kept small so the disclosure lands early,
#: well before an operator starts wondering whether the process is alive.
SLOT_DISCLOSE_FIRST_SEC = 10.0

#: Interval between subsequent slot-hold disclosures once a waiter has passed
#: :data:`SLOT_DISCLOSE_FIRST_SEC` — a steady heartbeat naming who holds the
#: slots for as long as the wait lasts.
SLOT_DISCLOSE_INTERVAL_SEC = 30.0

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
    from hpc_agent.state.run_record import current_homedir

    return current_homedir() / "_ssh_throttle"


def slot_paths(host: str, limit: int | None = None) -> list[Path]:
    """The slot files for *host* (test/introspection seam)."""
    if limit is None:
        limit = resolve_max_connections()
    base = _slot_dir()
    safe = _safe_name(host)
    return [base / f"{safe}.slot{i}" for i in range(limit)]


def _reclaim_lock_path(host: str) -> Path:
    return _slot_dir() / f"{_safe_name(host)}.slots.lock"


def _slot_hold_disclosure(host: str, limit: int, *, now: float, elapsed: float) -> str:
    """A deterministic one-line description of who is holding *host*'s slots.

    Pure disclosure for the F-N wait-visibility fix: names the host, the
    elapsed wait, and — per slot — the holder's pid, whether that pid is still
    alive (the reclaim signal, now that pid-liveness is the sole reaper), and the
    claim age. Read from the SAME slot files the claim path uses; a slot that
    is free or has an unreadable/malformed claim doc at read time is named as
    such (``free`` / ``unreadable``) rather than omitted, so every slot in the
    pool is accounted for. Never behaviour-changing — it only reports state.

    Wording is fixed (no timestamps beyond integer-second ages) so the line is
    stable across polls and greppable in a log.
    """
    base = _slot_dir()
    safe = _safe_name(host)
    parts: list[str] = []
    for i in range(limit):
        path = base / f"{safe}.slot{i}"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            parts.append(f"slot{i}=free")
            continue
        try:
            pid = int(doc.get("pid"))  # type: ignore[arg-type]
            claimed_at = float(doc.get("claimed_at"))  # type: ignore[arg-type]
        except (AttributeError, TypeError, ValueError):
            parts.append(f"slot{i}=unreadable")
            continue
        age = max(0, int(now - claimed_at))
        alive = "alive" if _pid_alive(pid) else "DEAD"
        parts.append(f"slot{i}=pid:{pid}({alive}) age:{age}s")
    return (
        f"hpc-agent: still waiting {int(elapsed)}s for an ssh connection slot to "
        f"{host} (all {limit} held) — holders: {', '.join(parts)}"
    )


def _emit_slot_disclosure(host: str, limit: int, *, now: float, elapsed: float) -> None:
    """Print the slot-hold disclosure to stderr; swallow ANY error (fail-open).

    A disclosure is diagnostics only — a broken read, a squatting state dir, an
    encoding hiccup — none of it may perturb the wait it describes. The whole
    build-and-print is wrapped so a disclosure error never propagates into the
    acquire loop.
    """
    with contextlib.suppress(Exception):
        print(
            _slot_hold_disclosure(host, limit, now=now, elapsed=elapsed),
            file=sys.stderr,
            flush=True,
        )


# PID liveness is one substrate fact with ONE definition (infra/proc.py, over
# psutil). This module keeps the ``_pid_alive`` name as a pure alias so the
# ``pid_alive=`` default arg below and any importer see the shared probe; the
# former hand-rolled win32/POSIX copy (which diverged from detached.py's on the
# zombie/access-denied edge) was deleted — audit 2026-07-07, finding #1.
_pid_alive = pid_alive


def _claim_is_stale(
    path: Path,
    *,
    pid_alive: Callable[[int], bool],
) -> bool:
    """True when the slot file at *path* belongs to a DEAD claim.

    Staleness is PID-LIVENESS only (the G4 shrink): a claim is reclaimable iff
    its recorded pid is no longer alive. There is no wall-clock TTL — a lease
    below the worst legitimate hold is exactly what over-admitted a connection
    in bug-sweep #35, and ownership-bound :func:`release_slot` already guards the
    hand-off race a TTL was reaching for. A malformed/unreadable claim doc reads
    as stale (fail-open: a corrupt slot file must not permanently eat a slot).
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
        pid = int(doc.get("pid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
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
        if _claim_is_stale(path, pid_alive=pid_alive):
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
            if not _claim_is_stale(path, pid_alive=pid_alive):
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
    # Flat poll cadence (P5): a fixed per-waiter interval, NOT a doubling
    # back-off — so a slot freed just after a poll is noticed within one flat
    # interval regardless of how long it was held (wakeup lag < 0.6s).
    poll = SLOT_POLL_BASE_SEC + jitter
    start = clock()
    deadline = start + SLOT_WAIT_MAX_SEC
    announced = False
    # F-N: a blocked waiter must not be silent. First disclosure lands after
    # SLOT_DISCLOSE_FIRST_SEC, then every SLOT_DISCLOSE_INTERVAL_SEC.
    next_disclosure = start + SLOT_DISCLOSE_FIRST_SEC
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
        # F-N periodic disclosure: name the holders (pid / liveness / age) so a long
        # wait is legible instead of looking like a hung process. Fail-open.
        if now >= next_disclosure:
            _emit_slot_disclosure(host, limit, now=now, elapsed=now - start)
            next_disclosure = now + SLOT_DISCLOSE_INTERVAL_SEC
        sleep(min(poll, max(0.0, deadline - now)))


def release_slot(token: Path | None, *, pid: int | None = None) -> None:
    """Release a slot claimed by :func:`acquire_slot` (``None`` is a no-op).

    Ownership-verified (bug-sweep #35): re-read the slot doc and unlink ONLY
    when it still records THIS releaser's pid. This is the ownership-bound
    release the G4 ruling makes the PRIMARY hand-off mechanism (a TTL was the
    hand-rolled backstop it retires): if a waiter reclaimed our slot while we
    held it (only ever legitimate once our pid is dead — but a raced pid-reuse
    successor is possible on a shared home), the file at *token* now carries the
    SUCCESSOR's claim, and unlinking it would evict a live claimant and
    over-admit a connection to the very host the limiter protects. On a pid
    mismatch the release is a no-op. A missing or
    unreadable doc is also left alone (nothing verifiable to release; a corrupt
    leftover is handled by the staleness reclaim path). *pid* defaults to the
    current process (the acquirer's default), and is injectable to mirror
    :func:`acquire_slot`'s test seam.
    """
    if token is None:
        return
    releaser = os.getpid() if pid is None else pid
    try:
        doc = json.loads(token.read_text(encoding="utf-8"))
        owner = int(doc["pid"])  # type: ignore[index]
    except (OSError, ValueError, TypeError, KeyError):
        # Unverifiable ownership — leave the file for the pid-liveness reclaim
        # rather than risk deleting a successor's live claim.
        return
    if owner != releaser:
        return  # our claim was reclaimed; the path belongs to another holder now
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
        # Release under the SAME pid the acquire claimed with (both default to
        # os.getpid()), so ownership verification recognises our own claim.
        release_slot(token, pid=pid)
