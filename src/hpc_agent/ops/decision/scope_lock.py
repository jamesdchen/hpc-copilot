"""``scope-lock`` + ``scope-status`` primitives — caller-tagged scope state.

Agent-facing CLI surface over :mod:`hpc_agent.state.scopes`, the lock-state +
look-ledger substrate for caller-tagged experiment scopes. The state layer
stays pure I/O; these primitives own the ``_wire`` models, validate the
boundary payload, and project the persisted state into the envelope's ``data``
block — the same posture the ``append-decision`` / ``read-decisions`` pair
keeps over :mod:`hpc_agent.state.decision_journal`.

``scope-lock`` is the SAFE direction — locking only ever restricts, so it
carries no human-authorship bar. The UNLOCK direction has no verb here: it is a
human act journaled through ``append-decision`` (``block="scope-unlock"``,
``resolved.scope_action="unlock"``), where the scope-unlock authorship gate
(:func:`hpc_agent.ops.decision.journal._assert_unlock_authorship`) refuses a
laundered one. ``scope-status`` is a pure read.

The framework attaches NO vocabulary to a tag (it is not "holdout" / "test" /
"embargo"); shape is the only constraint, and no statistic is ever consulted —
the look ledger counts identities, never reads what a look found.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.scope_lock import (
    ScopeLockInput,
    ScopeLockResult,
    ScopeLooks,
    ScopeStatusEntry,
    ScopeStatusInput,
    ScopeStatusResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state import scopes as _scopes

if TYPE_CHECKING:
    from collections.abc import Iterable

# The decision-journal actions that make up a scope's lock history — mirrors
# the state layer's ``_SCOPE_ACTIONS`` for the ``lock_history_len`` count.
_LOCK_ACTIONS = frozenset({"lock", "unlock"})


@primitive(
    name="scope-lock",
    verb="mutate",
    side_effects=[SideEffect("file_write", "<experiment>/.hpc/scopes/<tag>.decisions.jsonl")],
    error_codes=[errors.SpecInvalid],
    # Idempotent-in-EFFECT: a re-lock appends an audit line but leaves the lock
    # STATE unchanged (``already_locked`` reports it). Keyed on the tag.
    idempotent=True,
    idempotency_key="scope",
    cli=CliShape(
        help=(
            "Lock a caller-tagged experiment scope. Locking is the SAFE "
            "direction (it only restricts), so there is no authorship bar. "
            "Idempotent-in-effect: re-locking an already-locked scope appends "
            "an audit record but leaves the lock state unchanged "
            "(already_locked=true). Unlocking is a HUMAN act journaled via "
            "append-decision (block=scope-unlock), not a verb here."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ScopeLockInput,
        schema_ref=SchemaRef(input="scope_lock"),
    ),
    agent_facing=True,
)
def scope_lock(*, experiment_dir: Path, spec: ScopeLockInput) -> ScopeLockResult:
    """Lock the scope named by *spec*; append the lock to its decision journal.

    Validates the tag (shape only — never a role vocabulary), reports whether
    the scope was ALREADY locked, then appends the lock via
    :func:`hpc_agent.state.scopes.record_lock` (the safe direction, no
    authorship gate). Returns the post-call state plus the journal path.

    Raises
    ------
    :class:`errors.SpecInvalid`
        A non-slug ``scope`` tag or an empty ``reason`` (the state layer's
        boundary guards).
    """
    experiment_dir = Path(experiment_dir)
    tag = spec.scope
    # A primitive owns its invariants — revalidate the boundary input even
    # though the wire model's pattern already checked shape.
    _scopes.validate_tag(tag)
    already_locked = _scopes.is_scope_locked(experiment_dir, tag)
    _scopes.record_lock(experiment_dir, tag, reason=spec.reason)

    from hpc_agent.state.decision_journal import decisions_path

    return ScopeLockResult(
        scope=tag,
        locked=True,
        already_locked=already_locked,
        path=str(decisions_path(experiment_dir, "scope", tag)),
    )


@primitive(
    name="scope-status",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report the lock + look state of one caller-tagged scope, or every "
            "scope under .hpc/scopes/ when the tag is omitted. Pure read: "
            "{scope: {locked, looks: {prior_looks, distinct_lineages}, "
            "lock_history_len}}. Look counts are IDENTITY counts — no metric is "
            "ever consulted."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ScopeStatusInput,
        schema_ref=SchemaRef(input="scope_status"),
    ),
    agent_facing=True,
)
def scope_status(*, experiment_dir: Path, spec: ScopeStatusInput) -> ScopeStatusResult:
    """Report the lock + look state of one scope, or every scope.

    A pure read (no side effects): for each tag it returns whether the scope is
    currently locked (:func:`hpc_agent.state.scopes.is_scope_locked`), the look
    counts (:func:`hpc_agent.state.scopes.count_prior_looks` — ``prior_looks``
    and ``distinct_lineages``, plain integers over identities), and the
    lock-history length (the number of lock/unlock records on the append-only
    journal). With ``scope`` omitted it discovers every tag under
    ``.hpc/scopes/``; a missing tree reports ``{}``.

    Raises
    ------
    :class:`errors.SpecInvalid`
        A non-slug ``scope`` tag.
    """
    experiment_dir = Path(experiment_dir)
    tags: Iterable[str] = [spec.scope] if spec.scope else _discover_tags(experiment_dir)
    entries: dict[str, ScopeStatusEntry] = {}
    for tag in tags:
        _scopes.validate_tag(tag)
        entries[tag] = ScopeStatusEntry(
            locked=_scopes.is_scope_locked(experiment_dir, tag),
            looks=ScopeLooks.model_validate(_scopes.count_prior_looks(experiment_dir, tag)),
            lock_history_len=_lock_history_len(experiment_dir, tag),
        )
    return ScopeStatusResult(scopes=entries)


def _discover_tags(experiment_dir: Path) -> list[str]:
    """Every scope tag with an on-disk store under ``.hpc/scopes/``, sorted.

    A tag is present when it has a decision journal OR a look ledger. Returns
    ``[]`` when the tree does not exist — a pure read never scaffolds it.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    scopes_dir = RepoLayout(experiment_dir).hpc / "scopes"
    if not scopes_dir.is_dir():
        return []
    tags: set[str] = set()
    for suffix in (".decisions.jsonl", ".looks.jsonl"):
        for path in scopes_dir.glob(f"*{suffix}"):
            tags.add(path.name[: -len(suffix)])
    return sorted(tags)


def _lock_history_len(experiment_dir: Path, tag: str) -> int:
    """Count the lock/unlock decision records in *tag*'s journal (append-only)."""
    from hpc_agent.state.decision_journal import read_decisions

    count = 0
    for record in read_decisions(experiment_dir, "scope", tag):
        resolved = record.get("resolved")
        action = resolved.get("scope_action") if isinstance(resolved, dict) else None
        if action in _LOCK_ACTIONS:
            count += 1
    return count
