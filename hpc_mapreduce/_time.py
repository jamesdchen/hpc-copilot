"""UTC timestamp helpers shared across the package.

Centralised so timestamps in journal records, status reports, and
per-run sidecars all use the same canonical format (ISO-8601 with
explicit ``+00:00`` offset), and so every consumer of those records
parses them back the same way.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow", "utcnow_iso", "parse_iso_utc", "parse_iso_utc_or_none"]


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
