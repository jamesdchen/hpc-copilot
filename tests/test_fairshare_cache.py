"""Tests for ``forecast.fairshare_cache``.

The cache reduces SSH round-trips by reading a short-TTL'd file
instead of querying ``sshare`` every prediction. Pure I/O wrapped
around an injected fetch callable, so tests don't need SSH.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from claude_hpc.forecast.fairshare_cache import get_or_fetch, read_cache, write_cache

if TYPE_CHECKING:
    from pathlib import Path

_NOW = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


# ─── read/write round-trip ────────────────────────────────────────────


def test_read_returns_none_when_no_cache(tmp_path: Path) -> None:
    assert read_cache(tmp_path, now=_NOW) is None


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_cache(tmp_path, fairshare_by_user={"alice": 0.7, "bob": 0.3}, now=_NOW)
    out = read_cache(tmp_path, now=_NOW)
    assert out == {"alice": 0.7, "bob": 0.3}


def test_read_returns_none_when_stale(tmp_path: Path) -> None:
    """Default TTL is 60 minutes; a 2-hour-old cache is stale."""
    write_cache(tmp_path, fairshare_by_user={"alice": 0.7}, now=_NOW)
    later = _NOW + timedelta(hours=2)
    assert read_cache(tmp_path, now=later) is None


def test_read_respects_ttl_minutes_override(tmp_path: Path) -> None:
    """Caller can extend the TTL; a 5-min cache stays fresh under
    ttl_minutes=10."""
    write_cache(tmp_path, fairshare_by_user={"alice": 0.7}, now=_NOW)
    later = _NOW + timedelta(minutes=5)
    assert read_cache(tmp_path, now=later, ttl_minutes=10) is not None


def test_corrupt_cache_returns_none(tmp_path: Path) -> None:
    cache = tmp_path / ".hpc" / "sshare_cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{not valid json")
    assert read_cache(tmp_path, now=_NOW) is None


# ─── get_or_fetch ─────────────────────────────────────────────────────


def test_get_or_fetch_uses_cache_when_fresh(tmp_path: Path) -> None:
    write_cache(tmp_path, fairshare_by_user={"alice": 0.7}, now=_NOW)
    call_count = [0]

    def fetch() -> str:
        call_count[0] += 1
        return ""

    out = get_or_fetch(tmp_path, fetch_text=fetch, now=_NOW)
    assert out == {"alice": 0.7}
    assert call_count[0] == 0  # fetch not called


def test_get_or_fetch_calls_fetch_when_missing(tmp_path: Path) -> None:
    sshare_text = "Account|User|FairShare\nlabA|alice|0.5\n"
    out = get_or_fetch(tmp_path, fetch_text=lambda: sshare_text, now=_NOW)
    assert out == {"alice": 0.5}


def test_get_or_fetch_persists_after_fetch(tmp_path: Path) -> None:
    """After a fetch, subsequent calls hit the cache."""
    sshare_text = "Account|User|FairShare\nlabA|alice|0.5\n"
    call_count = [0]

    def fetch() -> str:
        call_count[0] += 1
        return sshare_text

    get_or_fetch(tmp_path, fetch_text=fetch, now=_NOW)
    get_or_fetch(tmp_path, fetch_text=fetch, now=_NOW)
    assert call_count[0] == 1  # second call hit cache


def test_get_or_fetch_refetches_when_stale(tmp_path: Path) -> None:
    sshare_text = "Account|User|FairShare\nlabA|alice|0.5\n"
    call_count = [0]

    def fetch() -> str:
        call_count[0] += 1
        return sshare_text

    get_or_fetch(tmp_path, fetch_text=fetch, now=_NOW)
    get_or_fetch(
        tmp_path,
        fetch_text=fetch,
        now=_NOW + timedelta(hours=2),  # past TTL
    )
    assert call_count[0] == 2


def test_cache_filename_is_predictable(tmp_path: Path) -> None:
    """Pin the on-disk path so callers + manual debugging can find it."""
    write_cache(tmp_path, fairshare_by_user={"alice": 0.7}, now=_NOW)
    cache = tmp_path / ".hpc" / "sshare_cache.json"
    assert cache.is_file()
    payload = json.loads(cache.read_text())
    assert "fetched_at" in payload
    assert payload["fairshare_by_user"] == {"alice": 0.7}
