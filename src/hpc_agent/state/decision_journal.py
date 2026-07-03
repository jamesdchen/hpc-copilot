"""Decision journal — the append-only record of every ``y``/nudge exchange.

Design origin: ``docs/design/human-amplification-blocks.md`` §2. Every
human touchpoint in the fork has one shape — code digests the evidence,
the LLM drafts a proposal, the human answers with ``y`` (greenlight) or a
natural-language nudge — and **every such exchange is journaled**. The
decision record, not the chat scroll, is the source of truth for *why* a
run (or campaign) took the shape it did.

This module GENERALIZES the per-run ``verdict_history`` audit
(``state/run_record.py`` — "why a non-deterministic decision took its
branch") from failure-escalations to *every* human touchpoint (submit
briefs, canary greenlights, campaign specs, anomalies, harvest
interpretations). It is a **separate store**: it never touches
``run_record.py`` or the ``RunRecord`` JSON.

Storage locality (mirrors the ``.hpc/`` cluster-relative tree that run
sidecars and campaign scratch already live under)::

    <experiment_dir>/.hpc/runs/<run_id>.decisions.jsonl        # scope_kind="run"
    <experiment_dir>/.hpc/campaigns/<campaign_id>/decisions.jsonl  # scope_kind="campaign"

One JSONL record per exchange, newest last, **append-only**: a write
never rewrites or truncates a prior record. Appends are serialized under
an advisory ``flock`` (the same lock discipline
``state/journal.py`` and ``ops/monitor/tick_log.py`` use) so concurrent
writers — an in-session agent, a slash-command surface, the campaign
driver — can't interleave bytes mid-line.

Pure I/O: no ``_wire`` import (the ``ops`` primitive layer owns the
Pydantic models and validates at the boundary), no SSH, no mapreduce.
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
    "SCOPE_KINDS",
    "append_decision",
    "read_decisions",
    "decisions_path",
]

# Bump only on a breaking record-shape change; readers tolerate unknown
# extra keys (forward-compat) so additive fields do NOT need a bump.
SCHEMA_VERSION = 1

# The two scopes a decision can belong to. A "run" decision journals the
# submit S1–S4 / anomaly / harvest touchpoints of a single run; a
# "campaign" decision journals the once-at-start spec greenlight plus the
# anomaly / completion briefs of an asynchronous campaign (design §4).
SCOPE_KINDS = frozenset({"run", "campaign"})

_log = logging.getLogger(__name__)


def _validate_scope(scope_kind: str, scope_id: str) -> None:
    """Validate the ``(scope_kind, scope_id)`` pair — fail loudly.

    A primitive owns its invariants: the scope id becomes a path segment,
    so it must be filesystem-safe (same constraint ``campaign_dir`` and
    the ``run_id`` slug already enforce) or it could escape the ``.hpc/``
    tree.
    """
    if scope_kind not in SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"scope_kind must be one of {sorted(SCOPE_KINDS)}; got {scope_kind!r}"
        )
    if not scope_id:
        raise errors.SpecInvalid("scope_id must be a non-empty string")
    if "/" in scope_id or "\\" in scope_id or scope_id in (".", ".."):
        raise errors.SpecInvalid(f"scope_id must be filesystem-safe; got {scope_id!r}")


def decisions_path(experiment_dir: Path, scope_kind: str, scope_id: str) -> Path:
    """Return the JSONL path for a scope's decision journal.

    Run scope lands under the per-experiment sidecar tree
    (``RepoLayout(experiment_dir).runs``); campaign scope lands inside the
    campaign's canonical scratch directory (``campaign_dir``). Both helpers
    create their parent directory idempotently — the same dir-creating
    layout access ``ops/monitor/tick_log`` makes for its ``.monitor.jsonl``
    path — so a first append into a fresh scope Just Works.

    Raises :class:`errors.SpecInvalid` on an unknown *scope_kind* or a
    non-filesystem-safe *scope_id*.
    """
    _validate_scope(scope_kind, scope_id)
    if scope_kind == "run":
        from hpc_agent._kernel.contract.layout import RepoLayout

        return RepoLayout(experiment_dir).runs / f"{scope_id}.decisions.jsonl"
    # scope_kind == "campaign" (validated above)
    from hpc_agent.meta.campaign.dirs import campaign_dir

    return campaign_dir(experiment_dir, scope_id) / "decisions.jsonl"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _append_jsonl_line(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON object as a line to *path* under an exclusive flock.

    Append-only: opens in ``"a"`` mode so a write can never rewrite or
    truncate a prior record. The advisory ``flock`` (real cross-process
    exclusion on both POSIX and win32 — see
    :func:`hpc_agent.infra.io.advisory_flock`) serializes concurrent
    appenders so two writers can't interleave bytes on the same line. The
    line is ``fsync``-ed so a source-of-truth decision survives a crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str) + "\n"
    with advisory_flock(_lock_path(path)), path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


def append_decision(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    block: str,
    response: str,
    evidence_digest: str | dict[str, Any] | None = None,
    proposal: str | list[Any] | dict[str, Any] | None = None,
    resolved: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Append one ``y``/nudge exchange to a scope's decision journal.

    Persists exactly the fields the design §2 schema enumerates (see the
    module docstring and ``docs/primitives/append-decision.md``). *ts* is
    auto-stamped (current UTC ISO-8601) when omitted — the one field no
    caller has any business asserting. Returns the record written (the
    caller can echo it back as confirmation).

    Append-only: this never reads-modifies-writes a prior record; a second
    call always adds a new line after the first.

    Raises :class:`errors.SpecInvalid` on a bad scope, an empty *block*, or
    an empty *response*.
    """
    _validate_scope(scope_kind, scope_id)
    if not block:
        raise errors.SpecInvalid("block must be a non-empty string (the block terminator id)")
    if not response:
        raise errors.SpecInvalid(
            "response must be a non-empty string ('y' for greenlight, or the nudge text)"
        )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts or utcnow_iso(),
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "block": block,
        "evidence_digest": evidence_digest if evidence_digest is not None else "",
        "proposal": proposal if proposal is not None else "",
        "response": response,
        "resolved": dict(resolved) if resolved else {},
        "provenance": dict(provenance) if provenance else {},
    }
    _append_jsonl_line(decisions_path(experiment_dir, scope_kind, scope_id), record)
    return record


def read_decisions(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    """Return every decision record for a scope, in append (chronological) order.

    Returns ``[]`` when the journal file does not exist yet (a scope with
    no recorded touchpoints). Blank lines and individually-corrupt lines
    are skipped with a warning rather than failing the whole read — one bad
    line must never strand the rest of an audit trail.

    Raises :class:`errors.SpecInvalid` on a bad scope.
    """
    _validate_scope(scope_kind, scope_id)
    path = decisions_path(experiment_dir, scope_kind, scope_id)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("decision_journal: skipping unreadable %s (%s)", path, exc)
        return records
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("decision_journal: skipping corrupt line %d in %s (%s)", lineno, path, exc)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records
