"""TTL record of canary-validated ``cmd_sha``s (#249).

Every ``submit-flow`` with ``canary=true`` fires a 1-task canary, waits for it
to terminate, and verifies it before the main array — 30s-30min of wall-clock
per submit. The canary validates that the cluster-side runtime (modules / conda
env / dispatch.py) can boot a single task for a given ``cmd_sha``. Once that's
been validated, re-running the *same* ``cmd_sha`` shortly after gets nothing new
from another canary.

This records ``canary_validated_at`` per ``(cmd_sha, framework-version)`` when a
canary verifies successfully (:func:`record_canary_validated`, called from
``verify-canary``). On the next submit of the same ``cmd_sha`` within
``HPC_CANARY_TTL_SEC`` (default 4h), ``submit-flow`` skips the canary and goes
straight to the main array (:func:`is_canary_validated_fresh`).

Key design — ``(cmd_sha, version)``, not ``(cmd_sha, env-activation, version)``:
the read site (``submit-flow``, holding ``job_env``) and the record site
(``verify-canary``, holding only the run sidecar) must compute the SAME key, and
``cmd_sha`` + the framework version are the two identities both reliably carry.
Folding env-activation in is the issue's stated invalidator, but deriving it
consistently across the two sites is fragile (the sidecar doesn't carry the raw
``MODULES`` / ``CONDA_SOURCE`` / ``CONDA_ENV``); a mismatch would silently make
the key never hit, defeating the optimization. Instead the env-activation edge
is covered by the bounded TTL plus the explicit overrides below. ``cmd_sha`` is
PARAMETER identity; a code/env change that matters generally moves ``cmd_sha``
or the version anyway.

Bypass / invalidate:

* ``HPC_NO_CANARY_SKIP=1`` disables the skip globally (:func:`cache_disabled`).
* ``HPC_CANARY_TTL_SEC`` overrides the freshness window.
* A framework version bump misses (different key).
* ``submit-flow``'s ``force_canary`` / ``--force-canary`` overrides per submit.

Only successful canary verifications are recorded — a failed canary is never
cached, so the next submit re-validates.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TTL_SEC",
    "cache_disabled",
    "canary_cache_key",
    "canary_validated_age_sec",
    "canary_validated_at",
    "is_canary_validated_fresh",
    "record_canary_validated",
]

#: Default freshness window (seconds) — 4 hours, per #249.
DEFAULT_TTL_SEC = 14400


def cache_disabled() -> bool:
    """True when ``HPC_NO_CANARY_SKIP=1`` disables the canary-skip optimization."""
    return os.environ.get("HPC_NO_CANARY_SKIP") == "1"


def _ttl_sec() -> int:
    raw = os.environ.get("HPC_CANARY_TTL_SEC")
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_TTL_SEC
        if val > 0:
            return val
    return DEFAULT_TTL_SEC


def _cache_path() -> Path:
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_canary_cache.json"


def _lock_path(target: Path) -> Path:
    """Sibling ``.lock`` path for *target* — the same convention as
    :func:`hpc_agent.state.run_record._lock_path` (``<name>.lock``)."""
    return target.with_suffix(target.suffix + ".lock")


def canary_cache_key(
    *,
    cmd_sha: str,
    version: str,
    cluster: str,
    tasks_py_sha: str = "",
    executor_sha: str = "",
) -> str:
    """Stable key for a ``(cmd_sha, framework-version, cluster)`` identity, plus
    optional CODE identity (``tasks_py_sha`` / ``executor_sha``).

    A version bump yields a different key (auto-invalidation on ``pip install
    -U``). ``cmd_sha`` is already a hash, so no further hashing is needed.
    ``cluster`` joined the key in proving run #5: a canary proves the spec runs
    on THAT cluster (modules, activation, scheduler dialect are cluster-local),
    so a discovery-validated entry must never let a hoffman2 submit skip its
    canary.

    ``tasks_py_sha`` / ``executor_sha`` fold CODE identity in (F41). ``cmd_sha``
    is PARAMETER identity ONLY (``state/run_sha.py`` hashes just the materialized
    ``resolve(i)`` kwargs — no executor, no ``tasks.py``, no experiment), so a
    run with identical swept kwargs but a DIFFERENT executor / ``tasks.py`` / repo
    carries the same ``cmd_sha``. Keyed on the triple alone it would skip its own
    never-boot-tested canary AND satisfy submit-s3's predecessor gate on a
    FOREIGN run's cache entry (the machine-global ``_canary_cache.json`` is shared
    across every repo on the box). Appending the code shas — both carried on the
    run sidecar / ``job_env`` at every call site — closes that cross-code /
    cross-repo hole. An empty (omitted) sha appends nothing, so a caller passing
    only the original three args gets a BYTE-IDENTICAL key: pre-existing entries
    simply never match again, the established (two→three-part) stale degradation.
    """
    parts = [cmd_sha, version, cluster]
    if tasks_py_sha:
        parts.append(f"tasks={tasks_py_sha}")
    if executor_sha:
        parts.append(f"exec={executor_sha}")
    return "|".join(parts)


def _read_cache() -> dict[str, Any]:
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def canary_validated_at(key: str, *, now: datetime | None = None) -> datetime | None:
    """The recorded ``validated_at`` timestamp for *key* when still within TTL, else None.

    Same "positively fresh" gate as :func:`is_canary_validated_fresh` (disabled /
    absent / malformed / expired all yield ``None`` — every "not positively
    fresh" case means "run the canary", the safe default), but returns the parsed
    ``validated_at`` datetime so a caller can (a) render the age and (b) compare
    it against LATER cluster-drift events. The gated submit-s2 skip couples this
    to the ssh circuit breaker: a breaker OPEN recorded after this timestamp
    invalidates the boot proof even inside the 4h TTL (latency-audit #10 event
    invalidation).
    """
    if cache_disabled():
        return None
    entry = _read_cache().get(key)
    if not isinstance(entry, dict):
        return None
    validated_at = _parse_iso(entry.get("validated_at"))
    if validated_at is None:
        return None
    ttl = entry.get("ttl_sec")
    if not isinstance(ttl, int) or ttl <= 0:
        ttl = DEFAULT_TTL_SEC
    now = now or datetime.now(timezone.utc)
    age = (now - validated_at).total_seconds()
    return validated_at if 0 <= age < ttl else None


def canary_validated_age_sec(key: str, *, now: datetime | None = None) -> int | None:
    """Age in seconds of *key*'s validation when fresh, else ``None``.

    Builds on :func:`canary_validated_at` (same freshness gate); returns the int
    age the gated skip disclosure names ("validated <age> ago").
    """
    now = now or datetime.now(timezone.utc)
    validated_at = canary_validated_at(key, now=now)
    if validated_at is None:
        return None
    return max(0, int((now - validated_at).total_seconds()))


def is_canary_validated_fresh(key: str, *, now: datetime | None = None) -> bool:
    """True when *key* has a recorded successful canary still within its TTL.

    ``False`` on disabled, absent, malformed, or expired — every "not positively
    fresh" case means "run the canary", the safe default. One definition of the
    freshness gate: delegates to :func:`canary_validated_at`.
    """
    return canary_validated_at(key, now=now) is not None


def record_canary_validated(key: str) -> None:
    """Record a *successful* canary validation for *key* (no-op when disabled).

    Best-effort: a write failure is swallowed (the record is an optimisation,
    never a correctness gate).
    """
    if cache_disabled():
        return
    from hpc_agent.infra.io import advisory_flock, atomic_write_json
    from hpc_agent.infra.time import utcnow_iso

    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the advisory flock across BOTH the read and the write so two
        # concurrent submits validating DIFFERENT cmd_shas can't lost-update
        # each other (read {A} / read {A} → write {A,B} clobbers write {A,C}).
        # This is the same lock idiom every other state read-modify-write uses
        # (state/journal.py, state/decision_journal.py → advisory_flock). A
        # lock-acquire or write failure degrades gracefully — the record is an
        # optimisation, never a correctness gate.
        with advisory_flock(_lock_path(path), timeout_sec=120.0):
            cache = _read_cache()
            cache[key] = {"validated_at": utcnow_iso(), "ttl_sec": _ttl_sec()}
            atomic_write_json(path, cache)
    except OSError:
        pass
