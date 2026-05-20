"""Cached SSH query for ``sshare -P`` output.

Fetching fairshare from the cluster on every prediction is expensive
(~50-500ms SSH round-trip). The values are stable on the order of
hours, so a TTL'd cache at
``<experiment_dir>/.hpc/sshare_cache.json`` cuts cluster load by
orders of magnitude without losing meaningful freshness.

Cache shape::

    {"fetched_at": "<ISO>", "fairshare_by_user": {...}}

Default TTL: 1 hour. Override via ``ttl_minutes=`` for tests or
projects with unusually volatile fairshare (rare).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hpc_agent.forecast.sshare_parser import parse_sshare


def _cache_path(experiment_dir: Path) -> Path:
    return experiment_dir / ".hpc" / "sshare_cache.json"


def _is_fresh(cached_at_iso: str, *, ttl_minutes: int, now: datetime) -> bool:
    try:
        cached_at = datetime.fromisoformat(cached_at_iso)
    except (ValueError, TypeError):
        return False
    # Normalize both sides to tz-aware UTC so a naive/aware mix doesn't
    # raise TypeError (silently treated as "not fresh" by the catch-all
    # below, masking real cache reads).
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = now - cached_at
    return age < timedelta(minutes=ttl_minutes)


def read_cache(
    experiment_dir: Path,
    *,
    now: datetime | None = None,
    ttl_minutes: int = 60,
) -> dict[str, float] | None:
    """Return cached ``{user: fairshare}`` if fresh; ``None`` if absent
    or stale."""
    if now is None:
        now = datetime.now(timezone.utc)
    path = _cache_path(experiment_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not _is_fresh(data.get("fetched_at", ""), ttl_minutes=ttl_minutes, now=now):
        return None
    fs = data.get("fairshare_by_user")
    if not isinstance(fs, dict):
        return None
    return {str(k): float(v) for k, v in fs.items() if isinstance(v, (int, float))}


def write_cache(
    experiment_dir: Path,
    *,
    fairshare_by_user: dict[str, float],
    now: datetime | None = None,
) -> Path:
    """Persist ``fairshare_by_user`` to the cache. Returns the path."""
    if now is None:
        now = datetime.now(timezone.utc)
    path = _cache_path(experiment_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": now.isoformat(timespec="seconds"),
        "fairshare_by_user": fairshare_by_user,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def get_or_fetch(
    experiment_dir: Path,
    *,
    fetch_text: Callable[[], str],
    now: datetime | None = None,
    ttl_minutes: int = 60,
) -> dict[str, float]:
    """Return cached fairshare or call ``fetch_text()`` for a fresh
    sshare snapshot. ``fetch_text`` is a 0-arg callable that returns
    raw ``sshare -P`` output (caller-supplied so this module stays
    pure / no SSH dependency).
    """
    cached = read_cache(experiment_dir, now=now, ttl_minutes=ttl_minutes)
    if cached is not None:
        return cached
    text = fetch_text()
    parsed = parse_sshare(text)
    write_cache(experiment_dir, fairshare_by_user=parsed, now=now)
    return parsed


__all__ = ["get_or_fetch", "read_cache", "write_cache"]
