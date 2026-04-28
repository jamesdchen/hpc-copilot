"""UTC timestamp helper shared across the package.

Centralised so timestamps in journal records, status reports, and shim
metadata all use the same canonical format (ISO-8601 with explicit
``+00:00`` offset). Previously each call site rolled its own; some used
local time without a TZ offset, which caused cross-host disagreement.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utcnow_iso"]


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with offset.

    Example: ``"2026-04-28T01:53:25+00:00"``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
