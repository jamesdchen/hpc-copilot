"""Cross-invocation TTL cache for :class:`ClusterSnapshot` node inspections.

``inspect_cluster`` (``infra/inspect/__init__.py::inspect_cluster``) already
memoises within one process for 60s via the in-process ``_CACHE`` TTLCache
(``infra/inspect/_common.py``), so a single submit cycle that re-asks pays the
scheduler round-trip once. But CLI invocations, detached workers, and the MCP
server are SEPARATE processes with SEPARATE ``_CACHE`` instances — a planner
that shells out ``hpc-agent`` twice, or an MCP call landing in a fresh worker,
re-pays the ``scontrol``/``qhost`` SSH cost every time. This module is the
CROSS-PROCESS sibling of that in-process cache: one disk view they can share.

Design mirrors ``ops/preflight/probe_cache.py`` verbatim-in-style (the
handshake-eliding sibling this pattern was proven on):

* **Keyed ``(cluster_name, scheduler)``.** One JSON file per cluster under the
  journal home (``<home>/_snapshot_cache/<cluster>.json``); the scheduler is the
  entry key inside it — the same composite key ``_CACHE`` uses, so the two
  caches never disagree about what a hit means. A ``clusters.yaml`` edit that
  flips a cluster's scheduler lands on a fresh entry (no cross-scheduler bleed).
* **SUCCESS-only.** ``inspect_cluster`` degrades to a partial snapshot rather
  than raising (see that module's docstring), so "did it work" is not "did it
  return" — a snapshot carrying ANY ``errors`` entry is a degraded probe and is
  never cached; the next call re-inspects. This is the cache-is-an-optimisation-
  never-a-correctness-gate line: a degraded read must not be replayed as truth.
* **TTL.** :data:`TTL_ENV` (default :data:`DEFAULT_TTL_SEC`, matched to the
  in-process ``_CACHE`` budget so the two horizons agree); ``0`` (or any
  non-positive value) disables the cache. Staleness is bounded by the TTL
  alone — v1 skips breaker-style invalidation (a 60s window is short enough
  that a drained/added node is re-observed within a minute).
* **Bypass.** :data:`BYPASS_ENV` ``=1`` opts the disk cache out entirely (dev
  work on the inspect path itself), independent of the in-process cache.
* **Fail-open.** A broken cache dir/file degrades to "no cache" (inspect live),
  never a raise — every OSError is swallowed, mirroring the probe cache and the
  breaker.

Mutations use the repo's ``advisory_flock`` + ``atomic_write_json`` idiom
(``infra/io.py``) so the concurrent writers above (separate processes sharing
one journal home) never tear a file. Sibling stale entries are pruned on write.

This does NOT touch ``cluster_history/`` (``infra/inspect/_persist.py``): that
is provenance — an append-only record of what was observed — not a cache, and
the two must stay separate (a cache is disposable; provenance is not).
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.ssh_circuit import _safe_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "BYPASS_ENV",
    "DEFAULT_TTL_SEC",
    "TTL_ENV",
    "cache_disabled",
    "cache_path",
    "load_fresh",
    "snapshot_ttl_sec",
    "store",
]

#: Env var overriding the snapshot TTL in seconds; non-positive disables.
TTL_ENV = "HPC_SNAPSHOT_CACHE_TTL_SEC"

#: Env var (``=1``) that opts the disk cache out entirely.
BYPASS_ENV = "HPC_NO_SNAPSHOT_CACHE"

#: Default snapshot TTL. Matched to the in-process ``_CACHE`` TTLCache budget
#: (``infra/inspect/_common.py``): both horizons agree so a cross-process hit
#: is never fresher-or-staler than an in-process one would have been.
DEFAULT_TTL_SEC = 60.0


def cache_disabled() -> bool:
    """True when :data:`BYPASS_ENV` ``=1`` opts the disk cache out."""
    return os.environ.get(BYPASS_ENV) == "1"


def snapshot_ttl_sec() -> float:
    """The effective TTL (seconds); ``0.0`` means the cache is disabled."""
    raw = os.environ.get(TTL_ENV, "")
    if not raw.strip():
        return DEFAULT_TTL_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_TTL_SEC


def cache_path(cluster_name: str) -> Path:
    """State file for *cluster_name* under the journal home (test-isolatable)."""
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_snapshot_cache" / f"{_safe_name(cluster_name)}.json"


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


def _read_doc(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _is_success(snapshot: Any) -> bool:
    """True only for a fully-clean snapshot dict (no ``errors`` entries).

    ``inspect_cluster`` returns a partial snapshot with populated ``errors``
    rather than raising on scheduler trouble, so "returned" is not "succeeded".
    A degraded snapshot must never be cached — the next call must re-inspect.
    """
    return isinstance(snapshot, dict) and not snapshot.get("errors")


def load_fresh(
    cluster_name: str, *, scheduler: str, clock: Callable[[], float] = time.time
) -> dict[str, Any] | None:
    """The cached snapshot dict for (*cluster_name*, *scheduler*), or ``None``.

    ``None`` when the cache is bypassed/disabled, or the entry is absent,
    expired, or malformed — every "not a clean hit" case collapses to "inspect
    live". A hit returns a COPY of the stored snapshot dict, ready for
    ``infra/inspect/_common.py::_snapshot_from_dict``.
    """
    if cache_disabled():
        return None
    ttl = snapshot_ttl_sec()
    if ttl <= 0:
        return None
    doc = _read_doc(cache_path(cluster_name))
    entries = (doc or {}).get("entries")
    entry = entries.get(scheduler) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return None
    try:
        at = float(entry["at"])
        snapshot = entry["snapshot"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(snapshot, dict):
        return None
    if clock() - at > ttl:
        return None
    return dict(snapshot)


def store(
    cluster_name: str,
    *,
    scheduler: str,
    snapshot: dict[str, Any],
    clock: Callable[[], float] = time.time,
) -> None:
    """Record a SUCCESSFUL snapshot for (*cluster_name*, *scheduler*); never raises.

    A snapshot carrying any ``errors`` entry (a degraded probe) is never
    stored. Stale sibling entries are pruned on write. Fail-open: a broken
    cache dir must never break inspection.
    """
    if cache_disabled():
        return
    ttl = snapshot_ttl_sec()
    if ttl <= 0 or not _is_success(snapshot):
        return
    path = cache_path(cluster_name)
    now = clock()
    try:
        from hpc_agent.infra.io import advisory_flock, atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        with advisory_flock(_lock_path(path)):
            doc = _read_doc(path) or {
                "schema_version": 1,
                "cluster": cluster_name,
                "entries": {},
            }
            entries = doc.get("entries")
            if not isinstance(entries, dict):
                entries = {}
            entries = {
                k: v
                for k, v in entries.items()
                if isinstance(v, dict) and now - _float_or(v.get("at"), 0.0) <= ttl
            }
            entries[scheduler] = {"at": now, "snapshot": snapshot}
            doc["entries"] = entries
            atomic_write_json(path, doc)
    except OSError:
        # Fail-open: a broken cache dir must never break inspection.
        return


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
