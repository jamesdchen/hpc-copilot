"""Unit tests for :mod:`claude_hpc.infra.cache`.

The cache backs :func:`infra.inspect.inspect_cluster` and
:func:`job.backfill.cached_probe`; correctness here is correctness for
both. The expensive tests live there. These are unit tests for the
data-structure-level invariants only:

* TTL eviction on :meth:`get` after a fake-clock advance.
* LRU eviction on :meth:`put` when ``max_size`` is exceeded.
* :func:`clear_all` walks the global registry.
* ``ValueError`` on bad construction args.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

from claude_hpc.infra.cache import TTLCache, clear_all


def test_get_returns_value_within_ttl() -> None:
    c: TTLCache[str, int] = TTLCache("test-fast", ttl_sec=60.0, max_size=4)
    c.put("a", 1)
    assert c.get("a") == 1
    assert "a" in c


def test_get_returns_none_after_ttl() -> None:
    c: TTLCache[str, int] = TTLCache("test-ttl", ttl_sec=10.0, max_size=4)
    base = time.monotonic()
    with mock.patch("claude_hpc.infra.cache.time.monotonic") as mt:
        mt.return_value = base
        c.put("a", 1)
        mt.return_value = base + 9.999
        assert c.get("a") == 1
        mt.return_value = base + 10.001
        assert c.get("a") is None
        # And the entry is dropped, not just shadowed.
        assert len(c) == 0


def test_lru_eviction_on_overflow() -> None:
    c: TTLCache[str, int] = TTLCache("test-lru", ttl_sec=60.0, max_size=2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    # 'a' was the LRU; it should be gone. 'b' and 'c' remain.
    assert c.get("a") is None
    assert c.get("b") == 2
    assert c.get("c") == 3
    assert len(c) == 2


def test_get_refreshes_lru_position() -> None:
    c: TTLCache[str, int] = TTLCache("test-lru-refresh", ttl_sec=60.0, max_size=2)
    c.put("a", 1)
    c.put("b", 2)
    # Read 'a' to make 'b' the LRU.
    assert c.get("a") == 1
    c.put("c", 3)
    # 'b' should be evicted, not 'a'.
    assert c.get("a") == 1
    assert c.get("b") is None
    assert c.get("c") == 3


def test_put_existing_key_resets_ttl_clock() -> None:
    c: TTLCache[str, int] = TTLCache("test-reset", ttl_sec=10.0, max_size=4)
    base = time.monotonic()
    with mock.patch("claude_hpc.infra.cache.time.monotonic") as mt:
        mt.return_value = base
        c.put("a", 1)
        mt.return_value = base + 8.0
        c.put("a", 99)  # refresh
        mt.return_value = base + 17.0  # 17s after first put, 9s after refresh
        assert c.get("a") == 99


def test_invalidate_drops_entry() -> None:
    c: TTLCache[str, int] = TTLCache("test-inv", ttl_sec=60.0, max_size=4)
    c.put("a", 1)
    c.invalidate("a")
    assert c.get("a") is None
    # No-op on missing key.
    c.invalidate("b")


def test_clear_drops_all_entries() -> None:
    c: TTLCache[str, int] = TTLCache("test-clear", ttl_sec=60.0, max_size=4)
    c.put("a", 1)
    c.put("b", 2)
    c.clear()
    assert len(c) == 0


def test_clear_all_walks_registry() -> None:
    a: TTLCache[str, int] = TTLCache("test-all-1", ttl_sec=60.0, max_size=4)
    b: TTLCache[str, int] = TTLCache("test-all-2", ttl_sec=60.0, max_size=4)
    a.put("x", 1)
    b.put("y", 2)
    clear_all()
    assert a.get("x") is None
    assert b.get("y") is None


def test_construction_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        TTLCache("bad", ttl_sec=-1.0, max_size=4)
    with pytest.raises(ValueError):
        TTLCache("bad", ttl_sec=60.0, max_size=0)
