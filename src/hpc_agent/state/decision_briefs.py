"""Decision briefs — the durable record of the brief a block emitted at a
decision boundary (conduct rule 9, the provenance gate).

Design origin: ``docs/design/proving-run-2-hardening.md`` §6 (rule 9).
Proving run #3 surfaced the failure this store closes: the agent
hand-injected a ``resolved`` field (``result_dir_template``) that no brief
had recommended and no human nudge named — a silent LLM default paper-over.
The provenance gate refuses such a greenlight, but it can only do so if the
brief the block actually emitted is on disk to diff against. This module is
that disk.

It is the brief-side mirror of :mod:`hpc_agent.state.decision_journal` (the
``y``/nudge audit log): CODE persists the brief the moment a block returns a
decision-point Result, in BOTH driving modes (block-drive driver AND direct
MCP-tool invocation). The v1 ``next_block`` lesson is canon — never key this
on block-drive-only state (``pending_decision`` doesn't exist in MCP-direct
mode, and at S1 no RunRecord exists yet). So the SUBMIT BLOCKS themselves
persist, not the driver.

Storage locality (mirrors the ``.hpc/runs/`` sidecar tree the decision
journal already uses)::

    <experiment_dir>/.hpc/runs/<run_id>.briefs.jsonl

One JSONL record per emitted brief, newest last, **append-only**. Appends are
serialized under the same advisory ``flock`` discipline
``state/decision_journal.py`` uses so concurrent writers can't interleave
bytes mid-line.

Pure I/O: no ``_wire`` import, no SSH, no mapreduce (the same posture as
``state/decision_journal.py``).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.decision_journal import _append_jsonl_line

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "SCHEMA_VERSION",
    "append_brief",
    "read_briefs",
    "latest_brief_for_block",
    "block_names_match",
    "briefs_path",
]

# Bump only on a breaking record-shape change; readers tolerate unknown extra
# keys (forward-compat) so additive fields do NOT need a bump.
SCHEMA_VERSION = 1

_log = logging.getLogger(__name__)


def _validate_run_id(run_id: str) -> None:
    """The run id becomes a path segment — it must be filesystem-safe.

    Mirrors :func:`state.decision_journal._validate_scope`'s run-scope guard so
    a brief file can never escape the ``.hpc/runs/`` tree.
    """
    if not run_id:
        raise errors.SpecInvalid("run_id must be a non-empty string")
    if "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        raise errors.SpecInvalid(f"run_id must be filesystem-safe; got {run_id!r}")


def briefs_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the JSONL path for a run's emitted-brief journal.

    Lands under the per-experiment sidecar tree (``RepoLayout(...).runs``),
    beside the run's ``.decisions.jsonl`` — created lazily on first append.

    Raises :class:`errors.SpecInvalid` on a non-filesystem-safe *run_id*.
    """
    _validate_run_id(run_id)
    from hpc_agent._kernel.contract.layout import RepoLayout

    return RepoLayout(experiment_dir).runs / f"{run_id}.briefs.jsonl"


def append_brief(
    experiment_dir: Path,
    *,
    run_id: str,
    block: str,
    brief: dict[str, Any],
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one emitted brief to a run's brief journal.

    Called by the submit blocks the instant they return a decision-point
    Result (``needs_decision=True``). *ts* is auto-stamped when omitted.
    Returns the record written.

    Append-only: never reads-modifies-writes a prior record.

    Raises :class:`errors.SpecInvalid` on a bad *run_id* or an empty *block*.
    """
    _validate_run_id(run_id)
    if not block:
        raise errors.SpecInvalid("block must be a non-empty string (the block terminator id)")
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts or utcnow_iso(),
        "run_id": run_id,
        "block": block,
        "brief": dict(brief) if brief else {},
    }
    _append_jsonl_line(briefs_path(experiment_dir, run_id), record)
    return record


def read_briefs(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Return every brief record for a run, in append (chronological) order.

    Returns ``[]`` when the journal file does not exist yet (an old run, a
    campaign, or a test that never persists briefs) — the fail-open-on-absence
    case the provenance gate relies on. Blank / individually-corrupt lines are
    skipped with a warning rather than stranding the rest of the trail.

    Raises :class:`errors.SpecInvalid` on a bad *run_id*.
    """
    path = briefs_path(experiment_dir, run_id)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("decision_briefs: skipping unreadable %s (%s)", path, exc)
        return records
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("decision_briefs: skipping corrupt line %d in %s (%s)", lineno, path, exc)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def block_names_match(a: str, b: str) -> bool:
    """True iff two block names refer to the same block, tolerating short forms.

    The submit blocks journal a brief under the SHORT name (``"s1"`` — the
    ``SubmitBlockResult.block`` literal), while a greenlight's ``append-decision``
    may name the block either way (``"s1"`` or ``"submit-s1"``). Normalizes the
    same way :func:`ops.decision.journal._chain_successor` does — exact match, or
    one ends with ``-<other>`` — so ``"s1"`` matches ``"submit-s1"`` without
    matching ``"submit-s11"``.
    """
    la = (a or "").strip().lower()
    lb = (b or "").strip().lower()
    if not la or not lb:
        return False
    return la == lb or la.endswith(f"-{lb}") or lb.endswith(f"-{la}")


def latest_brief_for_block(experiment_dir: Path, run_id: str, block: str) -> dict[str, Any] | None:
    """Return the most recent brief record for *(run_id, block)*, or ``None``.

    "Most recent" = the last matching record in append order (a block re-run
    after a nudge appends a fresh brief; the gate diffs against the latest).
    Block names match via :func:`block_names_match` (short-form tolerant).
    ``None`` when no brief for this block was ever persisted — the fail-open
    signal the provenance gate treats as "nothing to diff against".

    Raises :class:`errors.SpecInvalid` on a bad *run_id*.
    """
    for record in reversed(read_briefs(experiment_dir, run_id)):
        if block_names_match(str(record.get("block") or ""), block):
            return record
    return None
