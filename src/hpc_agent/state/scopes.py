"""Scope substrate — lock state + a look ledger for caller-tagged scopes.

A *scope* is a caller-chosen tag (a filesystem-safe slug, nothing more —
the framework attaches NO vocabulary to it: it is not "holdout", "test",
"embargo", or any other named role; those are semantics that stay
caller-owned). This module is pure substrate: it records whether a scope
is currently locked, and an append-only ledger of every *look* — a run
whose results were reduced against the scope — so a caller can ask "how
many times, across how many distinct lineages, has this scope been
looked at?" without the framework interpreting a single metric.

Two stores, both under ``<experiment_dir>/.hpc/scopes/``:

* ``<tag>.decisions.jsonl`` — the scope's decision journal (``scope_kind
  ="scope"`` in :mod:`hpc_agent.state.decision_journal`). Lock/unlock are
  ordinary decision records carrying ``resolved.scope_action``; the
  newest lock/unlock record decides the current state (append-only —
  unlock never erases the lock history).
* ``<tag>.looks.jsonl`` — the look ledger. One append-only line per
  (scope, run_id) pair, deduped: a second look at the same run is a
  no-op. Each line stores IDENTITY (run_id, cmd_sha, lineage_root,
  reducer_block) — NEVER a metric value. A metric in the ledger would
  tempt interpretation; identity is all a caller needs to count.

Pure I/O: no ``_wire`` import (the ``ops`` layer owns the Pydantic
models and validates at the boundary), no SSH, no mapreduce — the same
posture :mod:`hpc_agent.state.decision_journal` keeps.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.decision_journal import (
    _append_jsonl_line,
    append_decision,
    read_decisions,
)
from hpc_agent.state.runs import _RUN_ID_RE

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "LOOK_SCHEMA_VERSION",
    "validate_tag",
    "is_scope_locked",
    "record_lock",
    "record_look",
    "lineage_root",
    "lineage_chain",
    "count_prior_looks",
    "looks_path",
]

_log = logging.getLogger(__name__)

# Bump only on a breaking look-record shape change; readers tolerate
# unknown extra keys (forward-compat) so additive fields do NOT need a bump.
LOOK_SCHEMA_VERSION = 1

# The block-terminator id every scope lock/unlock decision carries, and the
# ``resolved`` key + values that encode the action. The newest record whose
# ``resolved.scope_action`` is one of these decides the lock state.
_SCOPE_LOCK_BLOCK = "scope-lock"
_SCOPE_ACTION_KEY = "scope_action"
_LOCK = "lock"
_UNLOCK = "unlock"
_SCOPE_ACTIONS = frozenset({_LOCK, _UNLOCK})


def validate_tag(tag: str) -> None:
    """Validate a scope *tag* — slug-safe shape only, never vocabulary.

    Reuses the state layer's one filesystem-safe slug pattern
    (``state.runs._RUN_ID_RE`` == ``^[A-Za-z0-9._\\-]+$`` — the same class
    ``RunIdStrict``/``CampaignId`` pin on the wire) so a tag becomes a safe
    path segment and cannot escape the ``.hpc/scopes/`` tree. Shape is the
    ONLY constraint: the framework never checks a tag against a role
    vocabulary.

    Raises :class:`errors.SpecInvalid` on an empty or non-slug tag.
    """
    if not tag:
        raise errors.SpecInvalid("scope tag must be a non-empty string")
    if not _RUN_ID_RE.fullmatch(tag):
        raise errors.SpecInvalid(
            f"scope tag must be filesystem-safe (^[A-Za-z0-9._-]+$); got {tag!r}"
        )


def looks_path(experiment_dir: Path, tag: str) -> Path:
    """Return the JSONL path for a scope's look ledger (file may not exist).

    ``<experiment_dir>/.hpc/scopes/<tag>.looks.jsonl`` — the sibling of the
    scope's decision journal. Does not create the file; the append helper
    creates the parent directory idempotently on first write.
    """
    validate_tag(tag)
    from hpc_agent._kernel.contract.layout import RepoLayout

    return RepoLayout(experiment_dir).hpc / "scopes" / f"{tag}.looks.jsonl"


def is_scope_locked(experiment_dir: Path, tag: str) -> bool:
    """True iff a scope's most recent lock/unlock decision is a ``lock``.

    Scans the scope decision journal newest→oldest; the FIRST record whose
    ``resolved.scope_action`` is ``lock`` or ``unlock`` decides (the
    newest-first precedence idiom ``ops/block_gate.assert_greenlit_target``
    uses). No such record → unlocked. Append-only: an unlock never erases
    the lock history, so ``lock`` then ``unlock`` reads unlocked while both
    records remain on disk.

    Raises :class:`errors.SpecInvalid` on a non-slug tag.
    """
    validate_tag(tag)
    for record in reversed(read_decisions(experiment_dir, "scope", tag)):
        resolved = record.get("resolved")
        action = resolved.get(_SCOPE_ACTION_KEY) if isinstance(resolved, dict) else None
        if action in _SCOPE_ACTIONS:
            return bool(action == _LOCK)
    return False


def record_lock(experiment_dir: Path, tag: str, *, reason: str) -> dict[str, Any]:
    """Append a ``lock`` decision to a scope's decision journal.

    Locking is the SAFE direction (it only ever restricts), so there is no
    human-authorship bar here — it routes straight through the state
    layer's append + validation (:func:`decision_journal.append_decision`)
    with ``scope_kind="scope"``. The *reason* is stored as the record's
    ``response`` (the free-text WHY) and the action lands in
    ``resolved.scope_action``.

    Returns the decision record written.

    Raises :class:`errors.SpecInvalid` on a non-slug tag or an empty reason.
    """
    validate_tag(tag)
    if not reason:
        raise errors.SpecInvalid("record_lock reason must be a non-empty string")
    return append_decision(
        experiment_dir,
        scope_kind="scope",
        scope_id=tag,
        block=_SCOPE_LOCK_BLOCK,
        response=reason,
        resolved={_SCOPE_ACTION_KEY: _LOCK},
    )


def _read_looks(experiment_dir: Path, tag: str) -> list[dict[str, Any]]:
    """Return every look-ledger record for *tag*, in append order.

    ``[]`` when the ledger does not exist yet. Blank / individually-corrupt
    lines are skipped with a warning (one bad line must not strand the
    ledger) — the tolerant read idiom ``decision_journal.read_decisions``
    uses.
    """
    path = looks_path(experiment_dir, tag)
    records: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return records
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("scopes: skipping unreadable look ledger %s (%s)", path, exc)
        return records
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            _log.warning("scopes: skipping corrupt line %d in %s (%s)", lineno, path, exc)
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def record_look(
    experiment_dir: Path,
    tag: str,
    *,
    run_id: str,
    cmd_sha: str,
    lineage_root: str,
    reducer_block: str,
) -> dict[str, Any] | None:
    """Append one look at *tag* by *run_id* — deduped on (scope, run_id).

    A *look* is a run whose results were reduced against the scope. The
    ledger stores IDENTITY only — run_id, cmd_sha, the run's
    :func:`lineage_root`, and the reducer block that produced it — NEVER a
    metric value (a metric in the ledger would tempt interpretation; the
    framework counts looks, it never reads what they found).

    Read-before-append dedup: a second look at the SAME (scope, run_id) is a
    no-op and returns ``None``; the first write returns the record. So
    counting the same run twice can never inflate a look count.

    Raises :class:`errors.SpecInvalid` on a non-slug tag or empty run_id.
    """
    validate_tag(tag)
    if not run_id:
        raise errors.SpecInvalid("record_look run_id must be a non-empty string")
    for existing in _read_looks(experiment_dir, tag):
        if existing.get("scope") == tag and existing.get("run_id") == run_id:
            return None  # already looked at this run under this scope — no-op
    record: dict[str, Any] = {
        "schema_version": LOOK_SCHEMA_VERSION,
        "ts": utcnow_iso(),
        "scope": tag,
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "lineage_root": lineage_root,
        "reducer_block": reducer_block,
    }
    _append_jsonl_line(looks_path(experiment_dir, tag), record)
    return record


def _walk_supersedes(experiment_dir: Path, run_id: str) -> list[str]:
    """Walk a run's ``supersedes`` backward links, newest→root.

    The ONE walk definition both :func:`lineage_root` and
    :func:`lineage_chain` route through — one traversal of the identity
    decision "what is this run's lineage" (engineering-principles: one
    definition per identity decision), so the root and the chain can never
    disagree about where the walk stops or which id represents a cycle.

    ``RunRecord.supersedes`` points from a newer run to the older one it
    superseded (stamped by
    :func:`hpc_agent.ops.supersession.stamp_supersedes_on_new`); the walk
    starts at *run_id* and follows the link back to the ORIGINAL run at the
    chain root. A run with no ``supersedes`` (or no journal record) ends the
    walk as its own root.

    Returns the ids visited in walk order (``run_id`` first, root last). On a
    corrupt cycle (``A supersedes B supersedes A``) the walk cannot loop
    forever: it stops on the first revisited id and the returned list ends
    with the lexicographically smallest id seen — a deterministic
    entry-independent representative of the loop — so a caller reducing the
    list to a single root (``[-1]``) matches the historical ``min(visited)``
    answer byte-for-byte.
    """
    current = run_id
    visited: set[str] = set()
    chain: list[str] = []
    while True:
        if current in visited:
            # Cycle: end the chain with a deterministic representative of the
            # loop, preserving the historical min(visited) root semantics.
            chain.append(min(visited))
            return chain
        visited.add(current)
        chain.append(current)
        record = _load_run(experiment_dir, current)
        parent = (record.supersedes or "") if record is not None else ""
        if not parent:
            return chain
        current = parent


def lineage_root(experiment_dir: Path, run_id: str) -> str:
    """Return a run's lineage-chain root id (newest→root walk's last id).

    ``RunRecord.supersedes`` points from a newer run to the older one it
    superseded; walking it reaches the ORIGINAL run at the chain root — the
    run's stable lineage identity across every spec-changing supersession. A
    run with no ``supersedes`` (or no journal record) is its own lineage root.

    Behaviour-identical to the pre-extraction inline walk (including a cycle
    resolving to ``min(visited)``): both this and :func:`lineage_chain` route
    through :func:`_walk_supersedes`, so the root is exactly the chain's last
    element.
    """
    return _walk_supersedes(experiment_dir, run_id)[-1]


def lineage_chain(experiment_dir: Path, run_id: str) -> list[str]:
    """Return a run's supersession chain, ordered newest→root.

    The full ordered lineage :func:`lineage_root` collapses to its last id:
    ``run_id`` first, each older run it superseded next, the chain root last.
    A run with no ``supersedes`` (or no journal record) yields a single-element
    ``[run_id]``. Same cycle guard as :func:`lineage_root` — a corrupt loop
    ends the chain with the deterministic ``min(visited)`` representative
    rather than spinning.

    Property both share by construction: ``lineage_chain(...)[-1] ==
    lineage_root(...)`` for every run (cyclic or not), since both route
    through :func:`_walk_supersedes`.
    """
    return _walk_supersedes(experiment_dir, run_id)


def _load_run(experiment_dir: Path, run_id: str) -> Any:
    """Load a RunRecord (module seam for test doubles); ``None`` if absent."""
    from hpc_agent.state.journal import load_run

    return load_run(experiment_dir, run_id)


def count_prior_looks(experiment_dir: Path, tag: str) -> dict[str, int]:
    """Count the looks recorded against *tag*.

    Returns ``{"prior_looks": <total look records>, "distinct_lineages":
    <distinct lineage_root values>}`` — plain integers, no metric ever
    consulted. ``distinct_lineages`` collapses several supersession-chained
    reruns of the SAME experiment to one lineage, so a caller can tell "N
    looks across M genuinely-distinct experiments" apart.

    Raises :class:`errors.SpecInvalid` on a non-slug tag.
    """
    validate_tag(tag)
    looks = _read_looks(experiment_dir, tag)
    lineages = {str(r.get("lineage_root") or "") for r in looks}
    lineages.discard("")
    return {"prior_looks": len(looks), "distinct_lineages": len(lineages)}
