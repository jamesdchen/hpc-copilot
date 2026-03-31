"""Job lifecycle tracking: event logging via append-only JSON-lines audit trail.

Event types: submit, resubmit, complete, fail (extensible via log_event).
"""

from __future__ import annotations

__all__ = [
    "log_event",
    "read_events",
]

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event logging
# ---------------------------------------------------------------------------


def log_event(audit_path: str | Path, action: str, **details) -> None:
    """Append a JSON-lines event to the lifecycle audit trail.

    Parameters
    ----------
    audit_path : path to lifecycle.jsonl
    action : event name (e.g. "submit", "resubmit", "complete", "fail")
    **details : arbitrary key-value pairs stored alongside the event
    """
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "action": action,
        **details,
    }
    try:
        Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
        with open(audit_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        logger.warning("Failed to write audit log: %s", exc)


def read_events(audit_path: str | Path) -> list[dict]:
    """Read all events from a lifecycle.jsonl file."""
    events: list[dict] = []
    path = Path(audit_path)
    if not path.exists():
        return events
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events
