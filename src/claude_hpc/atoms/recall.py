"""``recall`` primitive — query past interview.json files for memory across campaigns.

The interview primitive persists structured intent (goal, task_kind,
budget, abort_if, transcript, provenance, cmd_sha) into
``<campaign_dir>/interview.json``. ``recall`` walks a directory tree
for those files and returns filtered, recency-ordered summaries — the
substrate for "show me my last 5 LR sweeps" prompts in the next
interview.

Read-only and idempotent. Filesystem-only — no separate index DB.
Operator passes the experiments root explicitly via ``--root``; there
is no cross-session state.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


__all__ = ["recall_campaigns"]


# Hard cap on filesystem walk to avoid pathological scans of giant
# directory trees. Walking 10K interview.json files would take seconds;
# beyond that the operator should narrow --root.
_MAX_INTERVIEW_FILES = 10_000


@primitive(
    name="recall",
    verb="query",
    side_effects=[],
    idempotent=True,
)
def recall_campaigns(
    root: Path,
    *,
    task_kind: str | None = None,
    operator: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Walk *root* for ``interview.json`` files; return filtered summaries.

    *root* is a filesystem path under which campaign directories live.
    Directories without an ``interview.json`` are skipped silently.

    Filters (all optional, AND-combined):

    * ``task_kind``: exact match against ``intent.task_kind``.
    * ``operator``: exact match against ``intent.produced_by.operator``.
    * ``since``: ISO-8601 timestamp; only campaigns with
      ``_materialized.at >= since`` are included.

    Returns ``{campaigns: [...], total_matching: int, showing: int}``.
    Sorted by ``_materialized.at`` descending; truncated to ``limit``.
    Each summary mirrors the recall.output.json schema's campaign block.

    Malformed ``interview.json`` files are skipped silently — the recall
    surface is best-effort discovery, not a strict validator.
    """
    if not root.is_dir():
        raise ValueError(f"recall root is not a directory: {root}")

    rows: list[dict[str, Any]] = []
    seen = 0
    for path in root.rglob("interview.json"):
        seen += 1
        if seen > _MAX_INTERVIEW_FILES:
            break
        try:
            doc = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        summary = _summarize(doc, path)
        if summary is None:
            continue
        if task_kind is not None and summary.get("task_kind") != task_kind:
            continue
        if operator is not None and summary.get("operator") != operator:
            continue
        if since is not None:
            mat_at = summary.get("materialized_at")
            if not mat_at or mat_at < since:
                continue
        rows.append(summary)

    rows.sort(key=lambda r: r.get("materialized_at") or "", reverse=True)
    total_matching = len(rows)
    return {
        "campaigns": rows[:limit],
        "total_matching": total_matching,
        "showing": min(total_matching, limit),
    }


def _summarize(doc: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """Project an interview.json doc to the recall summary shape.

    Returns None for docs missing the ``_materialized`` block — those are
    almost certainly not produced by this version of the interview
    primitive (they could be a legacy format or someone's hand-written
    file that happens to be named interview.json).
    """
    materialized = doc.get("_materialized") or {}
    if not materialized:
        return None
    produced_by = doc.get("produced_by") or {}
    return {
        "campaign_dir": str(path.parent.resolve()),
        "goal": doc.get("goal"),
        "task_kind": doc.get("task_kind"),
        "task_count": doc.get("task_count"),
        "operator": produced_by.get("operator"),
        "produced_by_kind": produced_by.get("kind"),
        "materialized_at": materialized.get("at"),
        "cmd_sha": materialized.get("cmd_sha"),
    }
