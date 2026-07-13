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
from typing import TYPE_CHECKING, Any

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

from ._shared import (
    _fresh_human_texts,
    _ha_word_tokens,
    _is_bare_ack,
    _newest_lock_ts,
    _read_interview_actors,
    _refuse_missing_authorship,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

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


# ── scope-unlock authorship gate ──────────────────────────────────────────────

# The block-terminator convention for a scope UNLOCK. Locking uses
# ``state.scopes._SCOPE_LOCK_BLOCK`` ("scope-lock") and needs no bar (the safe
# direction, ``record_lock`` routes straight through the state layer); an unlock
# RELAXES the restriction and is a HUMAN act, journaled under this distinct block
# so this gate can recognise — and refuse — a laundered one.
_SCOPE_UNLOCK_BLOCK = "scope-unlock"


def _assert_unlock_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship gate for a scope UNLOCK — the relax direction of the lock.

    A *scope* (``hpc_agent.state.scopes``) is a caller-tagged experiment scope;
    LOCKING it only restricts, so ``record_lock`` carries no bar. UNLOCKING
    re-opens it for another look — the one scope action that loosens a
    restriction, so it faces the same human-authorship bar the fabricated-
    task_generator gate does. An unlock is journaled as an ``append-decision``
    whose ``scope_kind=="scope"``, ``block=="scope-unlock"``, and
    ``resolved.scope_action=="unlock"`` (the state layer reads ``scope_action``
    newest-first to decide the lock state; this block name is the convention the
    gate keys on).

    Block convention, enforced both directions:

    * a ``scope-unlock`` block is refused for any ``scope_kind`` other than
      ``scope`` (it is a scope-only action), and
    * a ``scope`` unlock MUST carry ``block=="scope-unlock"`` — a laundered
      unlock cannot hide under the lock block.

    A LOCK (``scope_action=="lock"``) never reaches the bar; nor does any
    non-scope record.

    Authorship check, tiered exactly like :func:`_assert_human_authorship`:

    * **A bare ack cannot unlock.** ``response`` in :data:`_BARE_ACK_RESPONSES`
      (:func:`_is_bare_ack`) — a ``y`` / click carries no authored rationale, and
      relaxing a scope must be a deliberate human statement, never a rubber-stamp.
    * **With the harness utterance log present** (:func:`_harness_human_texts` —
      the shared lock tier), the rationale's word tokens must derive from a logged
      human utterance, not the agent-relayed ``response``. Without a log the
      non-bare ``response`` itself is the human's typed rationale (the v1 friction
      tier); an all-short rationale with no word tokens to check passes on the
      strength of being non-bare.

    Raises :class:`errors.SpecInvalid`.
    """
    is_unlock_block = spec.block == _SCOPE_UNLOCK_BLOCK
    action = resolved.get("scope_action") if isinstance(resolved, dict) else None
    is_unlock_action = action == "unlock"

    # The scope-unlock block is a scope-only convention.
    if is_unlock_block and spec.scope_kind != "scope":
        raise errors.SpecInvalid(
            f"block {_SCOPE_UNLOCK_BLOCK!r} is only valid for scope_kind='scope' "
            f"(a scope unlock); got scope_kind={spec.scope_kind!r}."
        )

    if not (is_unlock_action and spec.scope_kind == "scope"):
        return  # not a scope unlock — nothing to gate (a lock passes untouched)

    # A scope unlock must be journaled under the scope-unlock block.
    if not is_unlock_block:
        raise errors.SpecInvalid(
            "scope-unlock authorship gate: a scope unlock "
            "(resolved.scope_action='unlock') must be journaled with "
            f"block='{_SCOPE_UNLOCK_BLOCK}', not {spec.block!r} — the distinct block "
            "is how the gate recognises an unlock (a lock uses 'scope-lock')."
        )

    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "scope-unlock authorship gate: unlocking a scope re-opens it for "
            f"another look and is a HUMAN act — a bare {spec.response!r} (a 'y' / "
            "click) cannot unlock it. The human must type the rationale for "
            "re-opening the scope."
        )

    # B4 ts>=anchor: the rationale must derive from an utterance logged AT OR
    # AFTER the LOCK it re-opens — a standing prompt that happened to share a word
    # with the unlock, typed before the scope was ever locked, is not a decision
    # to re-open it (the philosophy-audit B4 exposure). Anchor = the scope's
    # newest lock record ts (None → unfiltered: no lock on file yet).
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    anchor = _newest_lock_ts(experiment_dir, spec.scope_id)
    harness_texts = _fresh_human_texts(experiment_dir, actor_ids=_actor_ids, anchor=anchor)
    if harness_texts is not None:
        human_words: set[str] = set()
        for text in harness_texts:
            human_words |= _ha_word_tokens(text)
        rationale_words = _ha_word_tokens(response)
        if rationale_words and not (rationale_words & human_words):
            _refuse_missing_authorship(
                "scope-unlock authorship gate: with the harness utterance log "
                "installed, the unlock rationale must derive from a logged human "
                "utterance (harness-captured) dated AT OR AFTER the lock it re-opens "
                f"(B4 ts>=anchor), not the agent-relayed response {spec.response!r}. "
                "Have the human state why the scope is being re-opened in a prompt. "
                "(Under >1 declared actors the pool is the SESSION ACTOR'S log only "
                "— MH4.)"
            )
