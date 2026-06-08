"""The single JSON-object extractor for every model-output boundary.

Both model-facing floors in this codebase end the same way: a model
emitted text, and the orchestrator must recover the one JSON object it
was asked for. The spawned worker's :func:`parse_worker_report` and the
raw-completion :func:`hpc_agent._kernel.lifecycle.structured.structured`
funnel share this exact need, so the extraction lives here once rather
than twice.

:func:`last_json_object` tries a whole-string parse first (a constrained
producer is told to emit only the object) and falls back to the last
balanced ``{...}`` span, so a model that prefixes chatter still parses.
It returns the *last* top-level object deliberately: the report contract
puts the answer in the final message, and any earlier brace runs are
intermediate reasoning, not the result.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

__all__ = ["last_json_object"]


def last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in *text*, or ``None``.

    Tries a whole-string parse first (the producer is told to emit only
    the object); falls back to the last balanced ``{...}`` span so a
    producer that prefixes chatter still parses.
    """
    stripped = text.strip()
    with contextlib.suppress(json.JSONDecodeError):
        whole = json.loads(stripped)
        if isinstance(whole, dict):
            return whole
    depth = 0
    start = -1
    last: str | None = None
    for i, char in enumerate(stripped):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                last = stripped[start : i + 1]
    if last is None:
        return None
    with contextlib.suppress(json.JSONDecodeError):
        obj = json.loads(last)
        if isinstance(obj, dict):
            return obj
    return None
