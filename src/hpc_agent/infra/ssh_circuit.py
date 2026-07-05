"""Persistent per-host SSH circuit breaker — the fleet-level ban-hammer guard.

The 2026-07-04 incident: a wedged SSH preflight probe retried against one
cluster all night, piling up thousands of half-open connections until the
cluster's intrusion filter banned the box's source IP (login nodes first,
then the DTN). The per-call timeout fix bounds ONE call — but nothing
stopped a *fleet* of workers, detached runners, and retry ladders from
collectively hammering the host. This module is that stop.

Mechanism (classic circuit breaker, persisted per host):

* Every ssh-family attempt reports its outcome. **Consecutive
  connection-level failures** (connect timeout, banner-exchange timeout,
  connection refused/reset — see :data:`_CONNECTION_FAILURE_MARKERS`, plus
  the wrapper-level :class:`TimeoutError`) increment a per-host counter;
  any attempt that reaches the remote side (success, auth failure, remote
  command non-zero) proves the connection path works and resets it.
* At :data:`CIRCUIT_THRESHOLD` consecutive failures the circuit **opens**:
  every subsequent attempt to that host fails FAST with
  :class:`hpc_agent.errors.SshCircuitOpen` (``ssh_circuit_open`` /
  ``network`` / ``retry_safe=False``) instead of opening another
  connection the intrusion filter will count.
* After the cooldown (exponential: :data:`BASE_COOLDOWN_SEC` doubling to
  :data:`MAX_COOLDOWN_SEC`) exactly ONE caller may claim the **half-open
  probe slot**; its success closes the circuit, its failure re-opens with
  a doubled cooldown. The slot is claimed under the state file's lock so
  a fleet waking up together cannot stampede.

State lives at ``<journal home>/_ssh_circuit/<host>.json`` (journal home =
:func:`hpc_agent.state.run_record._current_homedir`, so ``HPC_JOURNAL_DIR``
redirection — and therefore the test suite's autouse isolation — applies).
File-based on purpose: CLI invocations, detached workers, and the MCP
server are separate processes, and the breaker only works if they share
one view of the host's health. Mutations use the repo's standard
``advisory_flock`` + ``atomic_write_json`` idiom (same as
``state/canary_cache.py``) for cross-process safety on POSIX and win32.

Override: ``HPC_SSH_CIRCUIT_OVERRIDE=<host>[,<host>...]`` bypasses the
fail-fast for exactly the named hosts — explicit and per-host, for the
operator who knows why the failures happened (e.g. a VPN flap) and accepts
the ban risk. Failures are still recorded while overridden.

Fail-open posture: a broken state dir / lock must never block SSH — every
read/write error here degrades to "breaker inactive", loudly where useful.
The breaker is a protection layer, not a correctness gate.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from hpc_agent.errors import SshCircuitOpen

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "BASE_COOLDOWN_SEC",
    "CIRCUIT_THRESHOLD",
    "MAX_COOLDOWN_SEC",
    "PROBE_CLAIM_TTL_SEC",
    "check_circuit",
    "circuit_state_path",
    "classify_connection_failure",
    "guarded_call",
    "record_connection_failure",
    "record_connection_success",
]

#: Consecutive connection-level failures that open the circuit.
CIRCUIT_THRESHOLD = 3

#: First cooldown after the circuit opens (seconds).
BASE_COOLDOWN_SEC = 300.0

#: Cooldown ceiling — repeated half-open failures double up to this (seconds).
MAX_COOLDOWN_SEC = 3600.0

#: A claimed half-open probe slot older than this is considered abandoned
#: (claimant crashed / was killed) and may be reclaimed. Comfortably larger
#: than one ssh attempt's worst case (SSH_TIMEOUT_SEC=60s default).
PROBE_CLAIM_TTL_SEC = 120.0

#: Env var naming hosts whose OPEN circuit is explicitly bypassed
#: (comma-separated). Per-host and explicit by design — no global kill switch.
OVERRIDE_ENV = "HPC_SSH_CIRCUIT_OVERRIDE"

# stderr markers that mean the CONNECTION itself failed — no TCP/SSH session
# was established (or it was torn down at banner/kex time). Auth failures
# ("Permission denied") and remote-command non-zero exits deliberately do NOT
# appear here: they prove the host accepted a connection, which is the
# opposite of ban-risk evidence. Matched case-insensitively against
# stderr+stdout, mirroring remote._SSH_THROTTLE_MARKERS' matching style.
_CONNECTION_FAILURE_MARKERS: tuple[str, ...] = (
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "timed out during banner exchange",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    # sshd-side teardown before auth (MaxStartups, fail2ban) — suffix-trimmed
    # so both "... by remote host" and the bare form match.
    "ssh_exchange_identification: connection closed",
    "kex_exchange_identification: connection closed",
    "kex_exchange_identification: read: connection reset",
)


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* (``user@host`` or a bare alias) — the same
    normalization :func:`hpc_agent.infra.ssh_throttle._host` uses, so the
    throttle and the breaker key identically."""
    return ssh_target.rsplit("@", 1)[-1].strip()


def _safe_name(host: str) -> str:
    """Filesystem-safe filename component for *host*."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def circuit_state_path(host: str) -> Path:
    """State file for *host* under the journal home (test-isolatable)."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_ssh_circuit" / f"{_safe_name(host)}.json"


def _lock_path(target: Path) -> Path:
    """Sibling ``.lock`` path — the repo-wide ``<name>.lock`` convention."""
    return target.with_suffix(target.suffix + ".lock")


def _read_doc(path: Path) -> dict[str, Any] | None:
    """Parse the state file; ``None`` on absent/unreadable/malformed."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _overridden(host: str) -> bool:
    """True when *host* is named in :data:`OVERRIDE_ENV`."""
    raw = os.environ.get(OVERRIDE_ENV, "")
    if not raw.strip():
        return False
    return host in {part.strip() for part in raw.split(",") if part.strip()}


def _fresh_doc(host: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "host": host,
        "state": "closed",
        "consecutive_failures": 0,
        "cooldown_sec": BASE_COOLDOWN_SEC,
        "opened_at": None,
        "probe_claimed_at": None,
        "last_failure": None,
    }


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_error(
    host: str, doc: dict[str, Any], *, now: float, probe_in_flight: bool
) -> SshCircuitOpen:
    """Build the fail-fast envelope error: why, until when, how to override."""
    failures = int(_float_or(doc.get("consecutive_failures"), 0))
    cooldown = _float_or(doc.get("cooldown_sec"), BASE_COOLDOWN_SEC)
    deadline = _float_or(doc.get("opened_at"), now) + cooldown
    if probe_in_flight:
        detail = "a half-open probe is already in flight; failing fast until it resolves"
    else:
        detail = f"failing fast until {_iso(deadline)} (~{max(0.0, deadline - now):.0f}s)"
    return SshCircuitOpen(
        f"ssh circuit for host '{host}' is OPEN after {failures} consecutive "
        f"connection-level failures (ban-risk protection: refusing to open more "
        f"connections that the cluster's intrusion filter would count); {detail}. "
        f"Override for this host only with {OVERRIDE_ENV}={host}."
    )


def classify_connection_failure(cp: subprocess.CompletedProcess[str]) -> str | None:
    """The matched connection-level marker for *cp*, or ``None``.

    ``None`` means "the connection reached the host" — a success, an auth
    failure, or a remote command's non-zero exit. Only a returned marker
    counts toward the breaker. (A raised :class:`TimeoutError` never gets
    here; :func:`guarded_call` counts it directly — at this seam a wrapper
    timeout is indistinguishable from a banner-exchange hang, and the
    incident's all-night retries were exactly those timeouts.)
    """
    if cp.returncode == 0:
        return None
    blob = ((cp.stderr or "") + "\n" + (cp.stdout or "")).lower()
    for marker in _CONNECTION_FAILURE_MARKERS:
        if marker in blob:
            return marker
    return None


def check_circuit(ssh_target: str, *, clock: Callable[[], float] = time.time) -> None:
    """Gate one ssh attempt to *ssh_target*; raise :class:`SshCircuitOpen` to refuse.

    Fast path (closed / no state / overridden): one lock-free file read, no
    writes. When the circuit is open and the cooldown has ended, this claims
    the single half-open probe slot under the file lock — the caller that
    returns normally IS the probe; concurrent claimants keep failing fast.
    *clock* is injectable for tests (wall clock: state is cross-process, so
    ``time.monotonic`` cannot be shared).
    """
    host = _host(ssh_target)
    if not host or _overridden(host):
        return
    path = circuit_state_path(host)
    doc = _read_doc(path)
    if doc is None or doc.get("state") != "open":
        return

    from hpc_agent.infra.io import advisory_flock, atomic_write_json

    now = clock()
    probe_claimed = False
    try:
        with advisory_flock(_lock_path(path)):
            # Re-read under the lock: another process may have just closed
            # the circuit or claimed the probe slot.
            doc = _read_doc(path)
            if doc is None or doc.get("state") != "open":
                return
            deadline = _float_or(doc.get("opened_at"), now) + _float_or(
                doc.get("cooldown_sec"), BASE_COOLDOWN_SEC
            )
            if now < deadline:
                raise _open_error(host, doc, now=now, probe_in_flight=False)
            claimed_at = doc.get("probe_claimed_at")
            if claimed_at is not None and now - _float_or(claimed_at, 0.0) < PROBE_CLAIM_TTL_SEC:
                raise _open_error(host, doc, now=now, probe_in_flight=True)
            doc["probe_claimed_at"] = now
            atomic_write_json(path, doc)
            probe_claimed = True
    except OSError:
        # Fail open: a broken lock/state dir must never block SSH.
        return
    if probe_claimed:
        print(
            f"hpc-agent: ssh circuit for {host} half-open — allowing one probe "
            f"(success closes the circuit; failure doubles the cooldown)",
            file=sys.stderr,
            flush=True,
        )


def record_connection_failure(
    ssh_target: str,
    *,
    detail: str = "",
    clock: Callable[[], float] = time.time,
) -> None:
    """Record one connection-level failure; open / re-open the circuit as due.

    Closed: increments the consecutive counter; at :data:`CIRCUIT_THRESHOLD`
    the circuit opens with :data:`BASE_COOLDOWN_SEC`. Open with a claimed
    probe slot: the half-open probe failed — re-open with a doubled cooldown.
    Open with NO claimed slot (a straggler that was already in flight when a
    peer opened the circuit): evidence only, no cooldown doubling — otherwise
    a concurrent burst would multiply the cooldown spuriously.
    """
    host = _host(ssh_target)
    if not host:
        return
    path = circuit_state_path(host)
    opened = reopened = False
    cooldown = BASE_COOLDOWN_SEC
    failures = 0
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path) or _fresh_doc(host)
            now = clock()
            if doc.get("state") == "open":
                if doc.get("probe_claimed_at") is not None:
                    cooldown = min(
                        _float_or(doc.get("cooldown_sec"), BASE_COOLDOWN_SEC) * 2.0,
                        MAX_COOLDOWN_SEC,
                    )
                    doc["cooldown_sec"] = cooldown
                    doc["opened_at"] = now
                    doc["probe_claimed_at"] = None
                    doc["consecutive_failures"] = (
                        int(_float_or(doc.get("consecutive_failures"), 0)) + 1
                    )
                    reopened = True
            else:
                failures = int(_float_or(doc.get("consecutive_failures"), 0)) + 1
                doc["consecutive_failures"] = failures
                if failures >= CIRCUIT_THRESHOLD:
                    doc["state"] = "open"
                    doc["opened_at"] = now
                    doc["cooldown_sec"] = BASE_COOLDOWN_SEC
                    doc["probe_claimed_at"] = None
                    cooldown = BASE_COOLDOWN_SEC
                    opened = True
            doc["last_failure"] = {"at": now, "detail": detail[:300]}
            failures = int(_float_or(doc.get("consecutive_failures"), 0))
            atomic_write_json(path, doc)
    except OSError:
        return
    if opened or reopened:
        verb = "re-OPENED (half-open probe failed)" if reopened else "OPENED"
        print(
            f"hpc-agent: ssh circuit for {host} {verb} after {failures} consecutive "
            f"connection failure(s) — failing fast for {cooldown:.0f}s to avoid an IP ban. "
            f"Override with {OVERRIDE_ENV}={host}.",
            file=sys.stderr,
            flush=True,
        )


def record_connection_success(ssh_target: str) -> None:
    """Reset the breaker after a connection that reached the host.

    Hot-path cheap: a lock-free read decides whether anything needs to
    change; the (locked) write happens only when the counter is nonzero or
    the circuit is open — the steady healthy state costs one file read and
    zero lock traffic.
    """
    host = _host(ssh_target)
    if not host:
        return
    path = circuit_state_path(host)
    doc = _read_doc(path)
    if doc is None:
        return
    if doc.get("state") != "open" and not _float_or(doc.get("consecutive_failures"), 0):
        return
    was_open = False
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path)
            if doc is None:
                return
            was_open = doc.get("state") == "open"
            atomic_write_json(path, _fresh_doc(host))
    except OSError:
        return
    if was_open:
        print(
            f"hpc-agent: ssh circuit for {host} closed (probe succeeded)",
            file=sys.stderr,
            flush=True,
        )


def guarded_call(
    ssh_target: str,
    fn: Callable[[], subprocess.CompletedProcess[str]],
    *,
    clock: Callable[[], float] = time.time,
) -> subprocess.CompletedProcess[str]:
    """Run one ssh-family attempt under the breaker.

    Consults :func:`check_circuit` BEFORE the attempt (so a retry ladder's
    next rung against an open circuit fails fast instead of proceeding),
    then records the outcome: a raised :class:`TimeoutError` or a
    connection-marked ``CompletedProcess`` counts as a connection failure;
    anything that reached the host resets the counter.
    """
    check_circuit(ssh_target, clock=clock)
    try:
        cp = fn()
    except TimeoutError as exc:
        record_connection_failure(ssh_target, detail=str(exc), clock=clock)
        raise
    marker = classify_connection_failure(cp)
    if marker is not None:
        stderr_snip = (cp.stderr or "").strip()[:200]
        record_connection_failure(ssh_target, detail=f"{marker}: {stderr_snip}", clock=clock)
    else:
        record_connection_success(ssh_target)
    return cp
