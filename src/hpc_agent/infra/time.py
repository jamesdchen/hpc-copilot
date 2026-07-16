"""UTC timestamp helpers shared across the package.

Centralised so timestamps in journal records, status reports, and
per-run sidecars all use the same canonical format (ISO-8601 with
explicit ``+00:00`` offset), and so every consumer of those records
parses them back the same way.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "utcnow",
    "utcnow_iso",
    "parse_iso_utc",
    "parse_iso_utc_or_none",
    "status_age_seconds",
    "humanize_age_sec",
]


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware ``datetime``."""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Example: ``"2026-04-28T01:53:25+00:00"``.
    """
    return utcnow().isoformat(timespec="seconds")


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware UTC ``datetime``.

    Accepts the trailing ``Z`` shorthand. Naive inputs are treated as
    UTC (matches the convention used by samples written before the
    explicit-offset format was adopted). Raises ``ValueError`` on
    malformed input.
    """
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_utc_or_none(s: str | None) -> datetime | None:
    """Permissive variant of :func:`parse_iso_utc` returning ``None`` on error."""
    if not s or not isinstance(s, str):
        return None
    try:
        return parse_iso_utc(s)
    except (TypeError, ValueError):
        return None


def status_age_seconds(last_status: dict | None) -> int | None:
    """Return age in seconds of ``last_status.checked_at``, or ``None``.

    Returns ``None`` when *last_status* is empty, has no ``checked_at``,
    or the timestamp is unparseable. Pure read; never raises.

    Lives here so any subject that reads a journal entry's last-status
    checkpoint (``ops/monitor`` for ``list-in-flight``; ``meta/campaign``
    for ``load-context``'s in-flight enumeration) can compute the age
    without crossing into another subject.
    """
    if not isinstance(last_status, dict):
        return None
    iso = last_status.get("checked_at")
    if not isinstance(iso, str):
        return None
    ts = parse_iso_utc_or_none(iso)
    if ts is None:
        return None
    delta = utcnow() - ts
    return max(0, int(delta.total_seconds()))


def humanize_age_sec(sec: int | float) -> str:
    """Compact human age string for *sec* seconds: ``45s`` / ``37m`` / ``2h 5m``.

    Used by disclosure lines that name how long ago something happened (e.g. the
    gated canary-skip "validated 37m ago"). Negative inputs clamp to ``0s``.
    """
    s = max(0, int(sec))
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, rem_min = divmod(minutes, 60)
    return f"{hours}h {rem_min}m" if rem_min else f"{hours}h"
