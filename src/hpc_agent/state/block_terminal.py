"""Block terminal-result store — the durable record of a detached submit
block's TERMINAL outcome, keyed by ``(run_id, block, cmd_sha)``, so a
re-invocation after the detached worker finished REPLAYS that outcome instead of
re-spawning a redundant worker.

Run #7 papercut (2026-07-05): a detached block (``submit-s2`` / ``submit-s3``)
re-invoked after its worker had already reached the block's terminal state
re-detached a FRESH worker. The single-lease
(:mod:`hpc_agent._kernel.lifecycle.detached`) only refuses a LIVE sibling and
self-heals on a dead pid, so a FINISHED block re-executes — redundant SSH, a new
canary poll, no cached result returned (the agent scraped the worker log). This
store is the "already terminal for this tree" signal the block consults before
spawning.

Distinct from :mod:`hpc_agent.state.decision_briefs` (the provenance-gate brief
journal): that is append-only and records only greenlightable (``needs_decision``)
briefs. THIS records the FULL ``SubmitBlockResult`` for EVERY terminal — including
a clean ``needs_decision=False`` completion (the S3-clean-terminal sibling, whose
brief the provenance journal never stored) — keyed on the tree's ``cmd_sha`` so a
nudge (a moved ``cmd_sha``) correctly forces re-execution rather than replaying a
stale outcome.

Storage locality mirrors the ``.hpc/runs/`` sidecar tree::

    <experiment_dir>/.hpc/runs/<run_id>.<block>.terminal.json

One JSON object per ``(run_id, block)``, OVERWRITTEN on each terminal (latest
wins; a re-canary after a nudge supersedes). Written atomically (tmp +
``os.replace``) under the same advisory-``flock`` discipline the decision journal
uses, so a crash mid-write can never leave a torn record.

Pure I/O: no ``_wire`` import, no SSH, no mapreduce (the same posture as
``state/decision_briefs.py``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.io import advisory_flock
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SCHEMA_VERSION", "record_terminal", "read_terminal", "terminal_path"]

# Bump only on a breaking record-shape change; readers tolerate unknown extra
# keys (forward-compat) so additive fields do NOT need a bump.
SCHEMA_VERSION = 1

_log = logging.getLogger(__name__)


def _validate_segment(value: str, *, what: str) -> None:
    """A ``(run_id | block)`` becomes a path segment — it must be fs-safe.

    Mirrors :func:`state.decision_briefs._validate_run_id` so a terminal record
    can never escape the ``.hpc/runs/`` tree.
    """
    if not value:
        raise errors.SpecInvalid(f"{what} must be a non-empty string")
    if "/" in value or "\\" in value or value in (".", ".."):
        raise errors.SpecInvalid(f"{what} must be filesystem-safe; got {value!r}")


def terminal_path(experiment_dir: Path, run_id: str, block: str) -> Path:
    """Return the JSON path for a ``(run_id, block)`` terminal record.

    Lands under the per-experiment sidecar tree (``RepoLayout(...).runs``),
    beside the run's ``.decisions.jsonl`` / ``.briefs.jsonl``. Raises
    :class:`errors.SpecInvalid` on a non-filesystem-safe *run_id* or *block*.
    """
    _validate_segment(run_id, what="run_id")
    _validate_segment(block, what="block")
    from hpc_agent._kernel.contract.layout import RepoLayout

    return RepoLayout(experiment_dir).runs / f"{run_id}.{block}.terminal.json"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def record_terminal(
    experiment_dir: Path,
    *,
    run_id: str,
    block: str,
    cmd_sha: str,
    result_dump: dict[str, Any],
) -> dict[str, Any]:
    """Record (OVERWRITE) a block's terminal result for idempotent replay.

    Keyed by ``(run_id, block)``; *cmd_sha* fingerprints the tree so a later
    replay can prove the outcome still applies. *result_dump* is the block's
    ``SubmitBlockResult.model_dump(mode="json")``. Written atomically (tmp +
    ``os.replace``) under a flock so a torn record is impossible. Returns the
    record written.
    """
    path = terminal_path(experiment_dir, run_id, block)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": utcnow_iso(),
        "run_id": run_id,
        "block": block,
        "cmd_sha": cmd_sha or "",
        "result": result_dump,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with advisory_flock(_lock_path(path)):
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, default=str)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    return record


def read_terminal(experiment_dir: Path, run_id: str, block: str) -> dict[str, Any] | None:
    """Return the recorded terminal for ``(run_id, block)``, or ``None``.

    ``None`` when the record is absent OR unreadable/corrupt — the fail-open
    signal the replay path treats as "nothing to replay, re-execute". A missing
    or damaged record must never crash a re-invocation.

    Raises :class:`errors.SpecInvalid` on a bad *run_id* / *block* (a programmer
    error in the caller, distinct from a benign missing file).
    """
    path = terminal_path(experiment_dir, run_id, block)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("block_terminal: skipping unreadable %s (%s)", path, exc)
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        _log.warning("block_terminal: skipping corrupt %s (%s)", path, exc)
        return None
    return obj if isinstance(obj, dict) else None
