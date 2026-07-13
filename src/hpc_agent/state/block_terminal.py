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

__all__ = [
    "SCHEMA_VERSION",
    "legacy_terminal_block_keys",
    "read_terminal",
    "read_terminal_with_fallback",
    "record_terminal",
    "terminal_block_key",
    "terminal_path",
]

# Bump only on a breaking record-shape change; readers tolerate unknown extra
# keys (forward-compat) so additive fields do NOT need a bump.
SCHEMA_VERSION = 1

_log = logging.getLogger(__name__)


# ── ONE key derivation (2026-07-07 key-mismatch fix) ─────────────────────────
#
# The terminal store, the detached lease (``_kernel/lifecycle/detached.py``), the
# doctor dead-worker scan (``ops/recover/doctor.py``), and the status-watch
# recorder must all key a detached block's terminal by the SAME string, or the
# doctor's cross-read mis-fires. Before this fix the submit recorder keyed by the
# block's SHORT literal ("s2"/"s3"/"s4") while the lease + doctor keyed by the
# detach VERB ("submit-s2"/"submit-s3"/"submit-s4"): a FINISHED submit worker
# recorded "s2" but the doctor looked up "submit-s2", found nothing, and drafted a
# spurious re-invoke (writer↔replayer agreed on the short key, so replay itself
# worked — only the doctor cross-read broke). status-watch was already correct
# (verb everywhere). The canonical key is therefore the VERB, and this is the ONE
# place that maps a block's short literal to it.
_SUBMIT_BLOCK_TO_VERB: dict[str, str] = {"s2": "submit-s2", "s3": "submit-s3", "s4": "submit-s4"}


def terminal_block_key(block_or_verb: str) -> str:
    """Canonical terminal-store block key for a detached block — the detach VERB.

    Accepts EITHER a submit block's short literal (``"s2"``/``"s3"``/``"s4"`` — what
    ``SubmitBlockResult.block`` carries) OR a verb already
    (``"submit-s2"``/``"submit-s3"``/``"submit-s4"``/``"submit-speculate"``/
    ``"status-watch"`` — what the lease stamps and the doctor reads off it), and
    returns the canonical VERB key. IDEMPOTENT: a verb maps to itself, so a caller
    that already holds the verb (the doctor, status-watch) is a no-op, while the
    submit recorder/replayer (which hold the short literal) canonicalize.

    THIS is the single derivation every writer and reader routes through — the
    submit recorder, the submit replay reader, the status-watch recorder, and the
    doctor dead-worker scan — so the store can never re-develop the "recorded under
    a short key, read under the verb key" split. Pinned by
    ``tests/state/test_block_terminal.py::test_terminal_block_key_is_the_one_derivation``.
    """
    return _SUBMIT_BLOCK_TO_VERB.get(block_or_verb, block_or_verb)


def legacy_terminal_block_keys(canonical_verb: str) -> tuple[str, ...]:
    """Pre-fix short keys a reader must ALSO try during the deprecation window.

    A run whose terminal was recorded BEFORE the 2026-07-07 canonical-key fix sits
    on disk under the submit block's SHORT literal ("s2"/"s3"/"s4"). A reader that
    only looked under the new verb key would miss it — re-executing a completed
    block (submit replay) or drafting a spurious re-invoke (doctor). So a reader
    tries the canonical verb key first and falls back to these legacy keys. Only
    the three numbered submit blocks ever wrote a short key; ``submit-speculate``
    and ``status-watch`` never did (verb == key from the start), so they have no
    legacy fallback. Remove this once no mid-flight run predates the fix.
    """
    if canonical_verb.startswith("submit-"):
        short = canonical_verb[len("submit-") :]
        if short in _SUBMIT_BLOCK_TO_VERB:  # "s2"/"s3"/"s4" only, never "speculate"
            return (short,)
    return ()


def read_terminal_with_fallback(
    experiment_dir: Path, run_id: str, block_or_verb: str
) -> dict[str, Any] | None:
    """Read a terminal by its canonical VERB key, falling back to legacy short keys.

    The migration-aware read: canonicalize *block_or_verb* to the verb key
    (:func:`terminal_block_key`), read it, and on a miss try each
    :func:`legacy_terminal_block_keys` short key so a run recorded pre-fix still
    replays / is still recognized as finished. Fail-open exactly like
    :func:`read_terminal` (absent/corrupt → ``None``). Writers use the canonical
    key directly (:func:`terminal_block_key`); only READERS carry the fallback.
    """
    key = terminal_block_key(block_or_verb)
    record = read_terminal(experiment_dir, run_id, key)
    if record is not None:
        return record
    for legacy in legacy_terminal_block_keys(key):
        record = read_terminal(experiment_dir, run_id, legacy)
        if record is not None:
            return record
    return None


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
    with advisory_flock(_lock_path(path), timeout_sec=120.0):
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
