"""``interview`` primitive — persist campaign intent alongside an agent-written tasks.py.

The interview-time leak today is that the chat between hpc-agent and
either MARs or a human produces *only* a tasks.py; the *why* (goal,
budget, abort criterion, transcript, who decided) lives in transient
session context and is gone after the campaign starts.

This primitive reads a ``interview.input.json`` payload and an
already-existing ``tasks.py`` in the campaign workdir, validates that
they agree (``tasks.total() == intent.task_count``), then persists the
intent — plus a ``cmd_sha`` fingerprint of the produced tasks.py and a
materialization timestamp — to ``<campaign_dir>/interview.json``.

The primitive is deliberately small. It does NOT generate tasks.py;
that would require typing the search space (``logspace``, ``grid``,
``items_x_seeds``, …) which narrows the otherwise experiment-agnostic
``total() + resolve(i) -> Any`` contract. The interview agent (MARs or
claude-the-interviewer) writes tasks.py themselves, and this primitive
records the intent alongside.

A future opt-in field — ``intent.task_generator`` — is reserved in the
schema for the case where the operator *does* want a typed recipe to
regenerate tasks.py. The schema documents the slot; the materializer
that consumes it is a separate primitive (not yet written).

Idempotent on (intent, campaign_dir): re-running with the same intent
overwrites interview.json with byte-equivalent content modulo the
``_materialized.at`` timestamp.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc._internal._time import utcnow

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


__all__ = ["record_interview"]


@primitive(
    name="interview",
    verb="produce",
    side_effects=[SideEffect("file_write", "<campaign_dir>/{interview.json,meta.json}")],
    idempotent=True,
)
def record_interview(
    intent: Mapping[str, Any],
    *,
    campaign_dir: Path,
) -> dict[str, Any]:
    """Validate an agent-written tasks.py against *intent* and persist interview.json.

    *intent* must conform to ``schemas/interview.input.json``. *campaign_dir*
    must already contain a ``tasks.py`` produced by the interview agent.

    Returns the envelope ``data`` block from ``schemas/interview.output.json``.

    Raises ``ValueError`` (mapped by the agent_cli adapter to spec_invalid):
    - tasks.py missing from campaign_dir
    - ``tasks.total() != intent.task_count``
    - tasks.py imports cleanly but reports total() < 1
    """
    if not campaign_dir.is_dir():
        raise ValueError(f"campaign_dir does not exist: {campaign_dir}")

    tasks_py = campaign_dir / "tasks.py"
    if not tasks_py.is_file():
        raise ValueError(
            f"campaign_dir is missing tasks.py: {tasks_py}. The interview "
            f"agent must produce tasks.py before invoking this primitive."
        )

    from claude_hpc import compute_cmd_sha, load_tasks_module

    tasks_mod = load_tasks_module(tasks_py)
    total_tasks = int(tasks_mod.total())
    if total_tasks < 1:
        raise ValueError(
            f"tasks.total() = {total_tasks}; campaign has no tasks to dispatch"
        )

    declared = int(intent["task_count"])
    if declared != total_tasks:
        raise ValueError(
            f"intent.task_count = {declared} but tasks.total() = {total_tasks}; "
            f"interview agent's stated count disagrees with the produced tasks.py"
        )

    preview = {
        "first": tasks_mod.resolve(0),
        "mid": tasks_mod.resolve(total_tasks // 2),
        "last": tasks_mod.resolve(total_tasks - 1),
    }
    cmd_sha = compute_cmd_sha(tasks_mod)

    artifacts: list[str] = []

    interview_path = campaign_dir / "interview.json"
    interview_doc = {
        **dict(intent),
        "_materialized": {
            "at": utcnow().isoformat(),
            "cmd_sha": cmd_sha,
            "total_tasks": total_tasks,
        },
    }
    interview_path.write_text(json.dumps(interview_doc, indent=2, sort_keys=True) + "\n")
    artifacts.append("interview.json")

    # meta.json — only updated when intent supplied cluster_target or budget,
    # and only fields the interview owns. Existing keys win on conflict so an
    # operator who hand-edited meta.json doesn't get clobbered, except for
    # total_tasks which is always authoritative (must match tasks.total()).
    meta_updates: dict[str, Any] = {}
    if "cluster_target" in intent:
        ct = intent["cluster_target"]
        meta_updates["cluster"] = ct["cluster"]
        meta_updates["profile"] = ct["profile"]
        if ct.get("constraint") is not None:
            meta_updates["constraint"] = ct["constraint"]
    if "budget" in intent:
        meta_updates["budget"] = dict(intent["budget"])

    if meta_updates:
        meta_path = campaign_dir / "meta.json"
        existing: dict[str, Any] = {}
        if meta_path.exists():
            existing = json.loads(meta_path.read_text())
        merged = {**meta_updates, **existing}
        merged["total_tasks"] = total_tasks
        meta_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        artifacts.append("meta.json")

    return {
        "campaign_dir": str(campaign_dir.resolve()),
        "artifacts": artifacts,
        "total_tasks": total_tasks,
        "cmd_sha": cmd_sha,
        "preview": preview,
    }
