"""TTL cache of successful cluster-side preflight checks (#255).

Every ``submit-flow`` invocation reproduces the job preamble's
``command -v uv`` over SSH to fail fast on a missing runtime (see
:func:`hpc_agent.ops.submit_flow._preflight_runtime_check`). Across a demo
iteration loop that re-submits the same ``(host, env-activation)`` many times,
that cluster round-trip is pure repetition — nothing it validates changed.

This module caches a *successful* preflight per
``(host, env-activation, framework-version)`` tuple for a TTL (default 15 min),
in a single global JSON file alongside the journal home
(``~/.claude/hpc/_preflight_cache.json`` — global, not per-repo, because the
thing validated is the cluster + env, not the experiment). A hit within TTL
lets the caller skip the cluster-side check; a miss / expiry / disable runs it.

Invalidation is structural, not time-only:

* The **env-activation** (``MODULES`` + ``CONDA_SOURCE`` + ``CONDA_ENV``) is
  folded into the key, so editing the cluster conda env between submits misses.
* The **framework version** is folded in, so a ``pip install -U`` misses.
* ``HPC_NO_PREFLIGHT_CACHE=1`` disables the cache globally
  (:func:`cache_disabled`).
* ``HPC_PREFLIGHT_TTL_SEC`` overrides the TTL.

Only *successes* are recorded — a failed preflight is never cached, so the next
submit re-runs it.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_TTL_SEC",
    "cache_disabled",
    "is_preflight_fresh",
    "preflight_cache_key",
    "record_preflight",
]

#: Default freshness window (seconds) — 15 minutes, per #255.
DEFAULT_TTL_SEC = 900


def _cache_path() -> Path:
    """Global cache file under the journal home (honours ``HPC_JOURNAL_DIR``)."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_preflight_cache.json"


def cache_disabled() -> bool:
    """True when ``HPC_NO_PREFLIGHT_CACHE=1`` opts the cache out entirely."""
    return os.environ.get("HPC_NO_PREFLIGHT_CACHE") == "1"


def _ttl_sec() -> int:
    """Effective TTL — ``HPC_PREFLIGHT_TTL_SEC`` (positive int) or the default."""
    raw = os.environ.get("HPC_PREFLIGHT_TTL_SEC")
    if raw:
        try:
            val = int(raw)
        except ValueError:
            return DEFAULT_TTL_SEC
        if val > 0:
            return val
    return DEFAULT_TTL_SEC


def preflight_cache_key(*, host: str, activation: str, version: str) -> str:
    """Stable cache key for a ``(host, env-activation, version)`` tuple.

    *host* stays human-readable; *activation* (which may carry long or
    sensitive cluster paths) is hashed so the on-disk file is compact and
    doesn't leak the raw activation string. A change in any of the three
    yields a different key — the structural invalidation #255 requires.
    """
    act_hash = hashlib.sha256(activation.encode("utf-8")).hexdigest()[:16]
    return f"{host}|{act_hash}|{version}"


def _read_cache() -> dict[str, Any]:
    """Best-effort read of the cache file; ``{}`` on any problem."""
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) to aware UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def is_preflight_fresh(key: str, *, now: datetime | None = None) -> bool:
    """True when *key* has a recorded success still within its TTL.

    Returns ``False`` when the cache is disabled, the key is absent, the entry
    is malformed, or the entry has expired — every "not positively fresh" case
    collapses to "re-run the check", the safe default. *now* is injectable for
    tests; production leaves it ``None`` (current UTC).
    """
    if cache_disabled():
        return False
    entry = _read_cache().get(key)
    if not isinstance(entry, dict):
        return False
    validated_at = _parse_iso(entry.get("validated_at"))
    if validated_at is None:
        return False
    ttl = entry.get("ttl_sec")
    if not isinstance(ttl, int) or ttl <= 0:
        ttl = DEFAULT_TTL_SEC
    now = now or datetime.now(timezone.utc)
    age = (now - validated_at).total_seconds()
    return 0 <= age < ttl


def record_preflight(key: str, *, checks: list[str] | None = None) -> None:
    """Record a *successful* preflight for *key* (no-op when caching disabled).

    Stamps ``validated_at`` (now) + the effective ``ttl_sec`` + the list of
    *checks* that passed. Best-effort: a write failure (read-only home, race)
    is swallowed — the cache is an optimisation, never a correctness gate.
    """
    if cache_disabled():
        return
    from hpc_agent.infra.io import atomic_write_json
    from hpc_agent.infra.time import utcnow_iso

    cache = _read_cache()
    cache[key] = {
        "validated_at": utcnow_iso(),
        "ttl_sec": _ttl_sec(),
        "checks": list(checks or []),
    }
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, cache)
    except OSError:
        pass
