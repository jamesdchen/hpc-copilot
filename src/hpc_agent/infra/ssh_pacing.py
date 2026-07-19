"""Cross-process per-host SSH-establishment RATE limiter — a token bucket.

The 2026-07-17 incident (proving run #15 discovery drill): a burst of many
short-lived processes each opened a FRESH ssh connection to one login node in
rapid succession. The concurrency-slot limiter
(:mod:`hpc_agent.infra.ssh_slots`) caps at most N=2 connections IN FLIGHT at
once — but it does not bound the establishment RATE: sequential fast connects
(each claims a slot, connects, releases, all under the cap) storm the remote at
whatever cadence the fleet spawns them. A MaxStartups-class throttle dropped
three connects in a row → the circuit breaker
(:mod:`hpc_agent.infra.ssh_circuit`) opened over a self-inflicted outage. The
slot limiter bounds the CONCURRENCY axis; this module bounds the orthogonal
RATE axis.

Mechanism — a classic token bucket, persisted per host, shared across
processes:

* Each NEW connection establishment consumes one token. Tokens refill at a
  steady rate of one per :data:`PACING_MIN_SPACING_SEC` (so the steady-state
  spacing between establishments is ~that interval), up to a ceiling of
  :data:`PACING_BURST` tokens (an idle host banks a burst allowance, so a
  handful of back-to-back connects after a quiet spell go through immediately
  and only a *sustained* storm is paced).
* State lives at ``<journal home>/_ssh_pacing/<host>.json`` (journal home =
  :func:`hpc_agent.state.run_record.current_homedir`, so ``HPC_JOURNAL_DIR``
  redirection — and the test suite's autouse isolation — applies), a sibling of
  the breaker's ``_ssh_circuit/`` and the slots' ``_ssh_throttle/``.
  File-based ON PURPOSE: the storm generator is many SHORT-LIVED processes, so
  an in-process bucket would never see the fleet's aggregate rate — the state
  must be on disk, exactly like the breaker and the slots. The read-modify-write
  goes through :func:`hpc_agent.infra.io.atomic_locked_update` — the SAME
  atomic + advisory-lock discipline the circuit file uses — so concurrent
  processes serialize their reservations without a lost update.
* A reservation may drive the bucket negative (a caller that finds it empty
  still reserves its token and waits the proportional time until that token
  would have refilled), so N processes racing on one empty bucket each wait a
  DIFFERENT, staggered amount rather than all waking together. A
  **deterministic pid-derived jitter** (±:data:`PACING_JITTER_FRAC`, no
  ``random`` — testable via an injected clock/sleep) spreads them further.
* **The wait is CAPPED at :data:`PACING_MAX_WAIT_SEC`.** A limiter must never
  DEADLOCK a leg: past the cap the caller discloses a "pacing cap exceeded"
  line to stderr and proceeds anyway, and the recorded deficit is floored so a
  storm's backlog beyond the cap is forgotten (recovery after the storm is
  bounded by the cap, not by the storm's depth).

Exemptions (where a token is NOT consumed):

* **asyncssh channel REUSE.** A command run over an already-open persistent
  connection (:meth:`hpc_agent.infra.ssh_engine._Engine.run` on a warm
  connection) is NOT an establishment — no new outbound handshake reaches the
  host — so it never reaches this limiter. The pacing is applied only in
  :meth:`_Engine._open`, the seam where a genuine new asyncssh connection is
  dialed.
* The two seams where a NEW outbound handshake actually launches — the native
  one-shot spawn (:func:`hpc_agent.infra.ssh_circuit.guarded_call`, through
  which every one-shot ssh AND every scp/rsync/tar transfer funnels) and the
  asyncssh connect (:meth:`_Engine._open`) — both route through
  :func:`pace_establishment`, so there is ONE pacing helper wrapping both.

Fail-open posture (same as the breaker and the slots): a broken state dir, an
unwritable bucket file, a wedged lock — none of it may block SSH. Every error
in the reserve path degrades to "limiter inactive for this call"; the pacing is
a protection layer, not a correctness gate.

Escape hatch: ``HPC_NO_SSH_PACING=1`` disables the rate limiter entirely (the
establishment spacing is then unbounded, exactly as before this guard existed).
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.env_flags import env_flag

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "NO_PACING_ENV",
    "PACING_BURST",
    "PACING_JITTER_FRAC",
    "PACING_MAX_WAIT_SEC",
    "PACING_MIN_SPACING_SEC",
    "bucket_path",
    "pace_establishment",
    "pacing_disabled",
]

#: Steady-state minimum spacing between NEW establishments to one host
#: (seconds). The token bucket refills one token per this interval, so once the
#: burst allowance is spent, establishments are paced ~this far apart.
PACING_MIN_SPACING_SEC = 0.3

#: Burst allowance: the bucket ceiling. An idle host banks up to this many
#: tokens, so a short flurry after a quiet spell goes through with no wait and
#: only a sustained storm is throttled.
PACING_BURST = 3

#: Maximum fraction (±) of a computed wait added as deterministic pid-derived
#: jitter, so a fleet that starts waiting together re-spaces staggered instead
#: of waking in unison. Derived from the pid (no ``random`` — testable).
PACING_JITTER_FRAC = 0.2

#: Hard cap on any single pacing wait (seconds). Past this the caller discloses
#: "pacing cap exceeded" and proceeds anyway — a limiter must NEVER deadlock a
#: leg on itself (the same bounded-by-construction posture as the slot limiter's
#: :data:`hpc_agent.infra.ssh_slots.SLOT_WAIT_MAX_SEC`).
PACING_MAX_WAIT_SEC = 10.0

#: Env var: ``HPC_NO_SSH_PACING=1`` disables the establishment-rate limiter.
NO_PACING_ENV = "HPC_NO_SSH_PACING"

#: Token refill rate (tokens/second) and bucket capacity, derived from the two
#: tunables above so there is one source of truth for the spacing/burst.
_RATE = 1.0 / PACING_MIN_SPACING_SEC
_CAPACITY = float(PACING_BURST)

# Prime modulus for the pid→jitter map (mirrors ssh_slots._JITTER_MOD): spreads
# consecutive pids (back-to-back workers get near-consecutive pids) across the
# jitter range.
# MIRROR: hpc_agent/infra/ssh_slots.py::_JITTER_MOD pinned-by tests/infra/test_ssh_pacing.py::TestMirrorPins::test_jitter_mod_matches_slots  # noqa: E501
_JITTER_MOD = 997


def pacing_disabled() -> bool:
    """Whether the establishment-rate limiter is turned off for this process."""
    return env_flag(NO_PACING_ENV, default=False)


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* — identical normalization to
    :func:`hpc_agent.infra.ssh_slots._host` /
    :func:`hpc_agent.infra.ssh_circuit._host`, so all the per-host guards key
    alike."""
    return ssh_target.rsplit("@", 1)[-1].strip()


# MIRROR: hpc_agent/infra/ssh_slots.py::_safe_name pinned-by tests/infra/test_ssh_pacing.py::TestMirrorPins::test_safe_name_matches_slots  # noqa: E501
def _safe_name(host: str) -> str:
    """Filesystem-safe filename component for *host* (mirrors ssh_slots)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def bucket_path(host: str) -> Path:
    """The token-bucket state file for *host* (test/introspection seam)."""
    from hpc_agent.state.run_record import current_homedir

    return current_homedir() / "_ssh_pacing" / f"{_safe_name(host)}.json"


def _jitter_fraction(pid: int) -> float:
    """Deterministic jitter in ``[-PACING_JITTER_FRAC, +PACING_JITTER_FRAC]``.

    Derived from *pid* (no ``random`` so tests are reproducible), mapping the
    pid's residue mod :data:`_JITTER_MOD` onto the symmetric fraction range.
    """
    unit = (pid % _JITTER_MOD) / _JITTER_MOD  # [0, 1)
    return (2.0 * unit - 1.0) * PACING_JITTER_FRAC


def _read_bucket(doc: dict[str, Any] | None) -> tuple[float, float | None]:
    """Parse ``(tokens, last_refill)`` from *doc*; a fresh/broken doc reads as a
    FULL bucket (``last_refill=None`` ⇒ no refill this pass).

    Fail-open: a missing/corrupt/partial bucket file must not permanently pace
    (or permanently free) — it reads as freshly full, so the next establishment
    is admitted and the bucket re-anchors to the current clock.
    """
    if not isinstance(doc, dict):
        return _CAPACITY, None
    try:
        tokens = float(doc["tokens"])
        last = float(doc["last_refill"])
    except (KeyError, TypeError, ValueError):
        return _CAPACITY, None
    return tokens, last


def _reserve(host: str, *, now: float, pid: int) -> tuple[float, bool]:
    """Reserve one establishment under the bucket lock; return ``(wait, capped)``.

    *wait* is the UN-jittered seconds the caller must sleep before establishing
    (``0.0`` when a token was available); *capped* is True when the reservation
    hit :data:`PACING_MAX_WAIT_SEC` and the recorded deficit was floored. The
    read-modify-write runs inside :func:`atomic_locked_update`'s advisory lock so
    concurrent processes reserve without a lost update. *pid* is unused here (the
    jitter is applied by the caller) but kept for signature symmetry / future
    use.
    """
    from hpc_agent.infra.io import atomic_locked_update

    captured: dict[str, Any] = {}

    def mutate(doc: dict[str, Any] | None) -> dict[str, Any]:
        tokens, last = _read_bucket(doc)
        elapsed = 0.0 if last is None else max(0.0, now - last)
        tokens = min(_CAPACITY, tokens + elapsed * _RATE)
        # Reserve this establishment's token (may drive the bucket negative — a
        # waiter still claims its slot in line and sleeps the proportional time).
        tokens -= 1.0
        if tokens >= 0.0:
            wait = 0.0
            capped = False
        else:
            wait = -tokens / _RATE
            capped = wait > PACING_MAX_WAIT_SEC
            if capped:
                wait = PACING_MAX_WAIT_SEC
                # Floor the recorded deficit so a storm's backlog beyond the cap
                # is forgotten: recovery after the storm is bounded by the cap,
                # not by how deep the storm drove the bucket.
                tokens = -(PACING_MAX_WAIT_SEC * _RATE)
        captured["wait"] = wait
        captured["capped"] = capped
        return {
            "schema_version": 1,
            "host": host,
            "tokens": tokens,
            "last_refill": now,
        }

    atomic_locked_update(bucket_path(host), mutate)
    return float(captured["wait"]), bool(captured["capped"])


def pace_establishment(
    ssh_target: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], object] = time.sleep,
    pid: int | None = None,
) -> None:
    """Pace ONE new connection establishment to *ssh_target* (may sleep, bounded).

    Consumes one token from *ssh_target*'s per-host bucket and sleeps until it is
    available, capping the wait at :data:`PACING_MAX_WAIT_SEC` (a disclosed
    "pacing cap exceeded" then proceeds — the limiter never deadlocks a leg).
    A no-op when the limiter is disabled (``HPC_NO_SSH_PACING=1``), the host is
    empty, or the bucket state dir is broken (fail-open — pacing breakage must
    never block SSH).

    Call this at the ONE moment a NEW outbound ssh/scp/rsync process or asyncssh
    connection is about to launch — NOT on a reused channel (no establishment).
    *clock* / *sleep* / *pid* are injectable for tests (wall clock: state is
    cross-process, so ``time.monotonic`` cannot be shared).
    """
    if pacing_disabled():
        return
    host = _host(ssh_target)
    if not host:
        return
    if pid is None:
        pid = os.getpid()
    try:
        base_wait, capped = _reserve(host, now=clock(), pid=pid)
    except (OSError, TimeoutError, ValueError):
        # Fail open: a broken state dir / wedged lock / corrupt bucket must never
        # block SSH — pace nothing for this call.
        return
    if base_wait <= 0.0:
        return
    # Deterministic pid-derived jitter, then re-cap so jitter never lifts a
    # capped wait back over the hard bound.
    wait = base_wait * (1.0 + _jitter_fraction(pid))
    wait = min(wait, PACING_MAX_WAIT_SEC)
    if wait <= 0.0:
        return
    if capped:
        print(
            f"hpc-agent: ssh establishment pacing cap exceeded for {host} — waited "
            f"the max {PACING_MAX_WAIT_SEC:.0f}s and is proceeding anyway (never "
            f"deadlocking a leg on the rate limiter); the connection-establishment "
            f"rate to this host is being throttled (burst {PACING_BURST}, "
            f"~{PACING_MIN_SPACING_SEC * 1000:.0f}ms spacing). Disable with "
            f"{NO_PACING_ENV}=1.",
            file=sys.stderr,
            flush=True,
        )
    sleep(wait)
