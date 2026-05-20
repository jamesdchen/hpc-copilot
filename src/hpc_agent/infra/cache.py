"""Generic in-process TTL cache shared by infra/job-level callers.

Three module-level dicts in the codebase implement the same pattern with
the same 60-second TTL:

* ``infra.inspect._CACHE`` — caches :class:`ClusterSnapshot` by
  ``(cluster, scheduler)`` so a single submit cycle that re-asks doesn't
  re-pay the SSH cost.
* ``job.backfill._PROBE_CACHE`` — caches :class:`BackfillProbe` by
  ``(cluster, constraint, walltime_minute)``.
* (Not migrated.) ``hpc_agent.runner`` last-status snapshot is
  file-based with a multi-hour horizon; different lifetime, different
  storage. We deliberately leave that one alone.

This module collapses the in-process ones onto a small, generic,
LRU-bounded TTL cache. Time is :func:`time.monotonic` so callers don't
get bitten by wall-clock jumps.

Design choices
--------------

* **Per-instance ``name``** so :func:`clear_all` can iterate every cache
  ever constructed in a process and so debug logs can identify which
  cache is evicting entries.
* **OrderedDict-based LRU** with ``move_to_end`` on every ``get`` /
  ``put`` and ``popitem(last=False)`` for eviction. Bounding the size
  keeps a misbehaving caller (e.g. unique key per call) from leaking
  memory.
* **TTL is per ``put``**: ``put`` records the monotonic timestamp and
  ``get`` evicts on miss. We don't run a background sweeper — caches
  this small don't need one and a sweeper would complicate teardown in
  tests.
* **No threading lock.** The two callers we migrate are single-threaded
  in practice (CLI invocations). If future callers need thread-safety,
  add a ``threading.Lock`` around ``get`` / ``put`` then; we'd rather
  not pay the contention cost speculatively.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

# Global registry so :func:`clear_all` can flush every TTLCache the
# process has created. Tests rely on this to keep instances hermetic.
_REGISTRY: list[TTLCache] = []


class TTLCache(Generic[K, V]):
    """Bounded LRU cache with per-entry monotonic-clock TTL.

    Parameters
    ----------
    name:
        Human-readable label used in the global registry and debug logs.
    ttl_sec:
        Entries written more than ``ttl_sec`` seconds ago are treated as
        misses on :meth:`get` and evicted lazily.
    max_size:
        Maximum number of live entries. The least-recently-used entry is
        evicted on overflow.
    """

    def __init__(self, name: str, ttl_sec: float, max_size: int = 256) -> None:
        if ttl_sec < 0:
            raise ValueError("ttl_sec must be non-negative")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.name = name
        self.ttl_sec = float(ttl_sec)
        self.max_size = int(max_size)
        # OrderedDict: maps key -> (monotonic_written_at, value). The
        # insertion / move-to-end order is the LRU order.
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        _REGISTRY.append(self)

    def get(self, key: K) -> V | None:
        """Return the cached value or ``None`` if absent / expired.

        On TTL miss the entry is dropped so the caller can re-populate
        without a separate :meth:`invalidate` call.
        """
        hit = self._data.get(key)
        if hit is None:
            return None
        written_at, value = hit
        if time.monotonic() - written_at > self.ttl_sec:
            # Lazy eviction on read keeps the data structure simple.
            self._data.pop(key, None)
            return None
        # Touch — refresh LRU position so frequently-read entries
        # survive eviction even if they weren't recently re-written.
        self._data.move_to_end(key)
        return value

    def put(self, key: K, value: V) -> None:
        """Insert or refresh ``key``'s entry.

        Re-writing an existing key resets its TTL clock and moves it to
        the most-recently-used end. If insertion would exceed
        ``max_size``, the LRU entry is evicted first.
        """
        now = time.monotonic()
        if key in self._data:
            self._data.move_to_end(key)
            self._data[key] = (now, value)
            return
        self._data[key] = (now, value)
        # Evict LRU if we just blew past the bound.
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def invalidate(self, key: K) -> None:
        """Drop ``key`` if present. No-op otherwise."""
        self._data.pop(key, None)

    def clear(self) -> None:
        """Drop every entry in this cache."""
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        # Honour TTL: a stale entry should not report present.
        return self.get(key) is not None  # type: ignore[arg-type]


def clear_all() -> None:
    """Flush every :class:`TTLCache` instance in the process.

    Tests use this to keep state from leaking across cases. Production
    callers shouldn't need it; the per-cache TTL handles staleness.
    """
    for cache in _REGISTRY:
        cache.clear()


__all__ = ["TTLCache", "clear_all"]
