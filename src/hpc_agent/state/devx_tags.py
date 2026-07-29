"""The devx session-tag ledger — the ONE dev-loop seam the product ships.

The 2026-07-28 dev-loop/product separation drew the wheel boundary: every
maintainer procedure lives repo-side, and the product keeps exactly one devx
affordance — a session can TAG itself as data for the dev loop to ingest
(``tag-session``, :mod:`hpc_agent.ops.devx_tag`). This module is that seam's
substrate: one append-only JSONL ledger per experiment,

    <experiment_dir>/.hpc/devx/session_tags.jsonl

written through :func:`hpc_agent.infra.io.append_jsonl_line` (the canonical
flock-append discipline every ledger in the package uses — append-only,
whole-line-atomic, crash-durable).

Records are OPAQUE to core: ``tags`` are free-slug strings and ``note`` is
free text; nothing in the product reads them back to change behavior. The
only consumer is the maintainer's ingestion tooling, which runs repo-side —
by design this module has a writer and a plain reader, and NO interpreter.
A tag is data ABOUT the session, never evidence: nothing here enters a gate,
a journal, or a decision path (contrast ``state/decision_journal.py``, whose
records are load-bearing evidence with authorship tiers).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["append_session_tag", "read_session_tags", "session_tags_path"]


def session_tags_path(experiment_dir: Path) -> Path:
    """``.hpc/devx/session_tags.jsonl`` for *experiment_dir* (file may not exist)."""
    return RepoLayout(experiment_dir).hpc / "devx" / "session_tags.jsonl"


def append_session_tag(
    experiment_dir: Path,
    *,
    tags: list[str],
    note: str | None,
    session_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    """Append one tag record and return it (as written, ``ts`` stamped here)."""
    record: dict[str, Any] = {
        "ts": utcnow_iso(),
        "tags": list(tags),
        "note": note,
        "session_id": session_id,
        "run_id": run_id,
    }
    path = session_tags_path(experiment_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_line(path, record)
    return record


def read_session_tags(experiment_dir: Path) -> list[dict[str, Any]]:
    """Every parseable record in the ledger, in file order.

    A torn or foreign line is skipped, not fatal — the ledger is ingestion
    data, and a devx read must never wedge a product path.
    """
    import json

    path = session_tags_path(experiment_dir)
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
