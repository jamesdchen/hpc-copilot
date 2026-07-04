"""``append-decision`` + ``read-decisions`` primitives — the decision journal.

Agent-facing CLI surface over
:mod:`hpc_agent.state.decision_journal`, the append-only store of every
``y``/nudge exchange (``docs/design/human-amplification-blocks.md`` §2).
The state layer stays pure I/O; these primitives own the ``_wire``
models, validate the boundary payload, and project the persisted records
into the envelope's ``data`` block.

``append-decision`` is deliberately **not idempotent**: the journal is an
audit log, so a replayed append records a second line rather than
deduping — there is no natural idempotency key (``ts`` is auto-stamped and
differs per call). ``read-decisions`` is a pure query.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.decision_journal import (
    AppendDecisionInput,
    AppendDecisionResult,
    DecisionRecord,
)
from hpc_agent._wire.queries.decision_journal import (
    ReadDecisionsInput,
    ReadDecisionsResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.decision_journal import append_decision as _append_decision
from hpc_agent.state.decision_journal import decisions_path as _decisions_path
from hpc_agent.state.decision_journal import read_decisions as _read_decisions


@primitive(
    name="append-decision",
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/.hpc/runs/<run_id>.decisions.jsonl "
            "| <experiment>/.hpc/campaigns/<campaign_id>/decisions.jsonl",
        )
    ],
    error_codes=[errors.SpecInvalid],
    # An append to an audit log is NOT replay-safe — retrying records a
    # duplicate line. Declared honestly so the registry doesn't advertise a
    # dedup guarantee the store doesn't make.
    idempotent=False,
    cli=CliShape(
        help=(
            "Append one y/nudge exchange to a run's or campaign's "
            "decision journal (append-only JSONL). The decision record — "
            "not the chat scroll — is the source of truth for why a run "
            "took the shape it did (design §2)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=AppendDecisionInput,
        schema_ref=SchemaRef(input="append_decision"),
    ),
    agent_facing=True,
)
def append_decision(*, experiment_dir: Path, spec: AppendDecisionInput) -> AppendDecisionResult:
    """Append the exchange described by *spec* to the decision journal.

    Auto-stamps ``ts`` (current UTC ISO-8601) and ``schema_version``; the
    agent supplies only what it resolved at the touchpoint. On a run-scoped
    greenlight (``response=="y"``) whose ``resolved`` omits ``next_block``, the
    successor is defaulted from the parked pending decision (see
    :func:`_default_next_block`) so the block-drive gate passes without the agent
    restating it. Returns the written record plus the running record count and the
    journal path.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Unknown ``scope_kind``, non-filesystem-safe ``scope_id``, empty
        ``block``, or empty ``response`` (the state layer's boundary
        guards).
    """
    experiment_dir = Path(experiment_dir)
    resolved = _default_next_block(experiment_dir, spec)
    _assert_brief_provenance(experiment_dir, spec, resolved)
    record = _append_decision(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        block=spec.block,
        response=spec.response,
        evidence_digest=spec.evidence_digest,
        proposal=spec.proposal,
        resolved=resolved,
        provenance=spec.provenance,
    )
    count = len(_read_decisions(experiment_dir, spec.scope_kind, spec.scope_id))
    path = _decisions_path(experiment_dir, spec.scope_kind, spec.scope_id)
    return AppendDecisionResult(
        path=str(path),
        record=DecisionRecord.model_validate(record),
        count=count,
    )


def _default_next_block(experiment_dir: Path, spec: AppendDecisionInput) -> dict[str, Any] | None:
    """Default ``resolved["next_block"]`` from the parked pending decision.

    The block-drive gate (``ops/block_gate.assert_greenlit_target``) requires a
    greenlight's ``resolved["next_block"]`` to name the block it authorizes — the
    successor the predecessor block already computed. Proving run #2 surfaced the
    papercut: the agent had to supply that field by hand, and omitting it made the
    next block's gate reject the advance, forcing an append→reject→re-append
    round-trip (two ``s1`` greenlight records 32s apart, the first missing
    ``next_block``).

    Two derivations, in order (v2 — proving-run-3 re-fire):

    1. **Parked pending decision** — when ``block_drive`` drove the chain, its
       ``_park`` stored ``resume_cursor["next_verb"]`` in the run's
       ``pending_decision``. (Requires a RunRecord + the block-drive mode.)
    2. **Static chain table** — the skills' preferred mode invokes the block's
       MCP tool directly: no driver, no park, and at S1→S2 no RunRecord even
       exists, so v1's derivation never fired and the papercut re-appeared in
       run #3. Fall back to ``infra/block_chain.ORDER`` — the record's own
       ``block`` field names the block that terminated; its chain successor is
       the machine-computed next verb. Mode-independent. Short forms
       (``"s1"``, the run-#2/#3 journaling convention) match by suffix; an
       ambiguous or chain-final block derives nothing.

    Never overrides an explicit value — a nudge that redirects the successor,
    or an agent that set it, stays authoritative. Non-run scopes and non-``y``
    responses are returned untouched.
    """
    resolved = spec.resolved
    if spec.scope_kind != "run" or str(spec.response or "") != "y":
        return resolved
    if not isinstance(resolved, dict) or resolved.get("next_block"):
        return resolved
    # Lazy import: ops depends on state, but keep it local to avoid any
    # import-time coupling with the run-record store.
    from hpc_agent.state.journal import read_pending_decision

    pending = read_pending_decision(spec.scope_id, experiment_dir=experiment_dir)
    cursor = pending.get("resume_cursor") if isinstance(pending, dict) else None
    successor = cursor.get("next_verb") if isinstance(cursor, dict) else None
    if isinstance(successor, str) and successor:
        return {**resolved, "next_block": successor}
    successor = _chain_successor(spec.block)
    if successor:
        return {**resolved, "next_block": successor}
    return resolved


def _collect_brief_names(node: Any, acc: set[str]) -> None:
    """Walk *node* collecting every dict KEY and every string SCALAR into *acc*.

    The provenance gate asks "does this ``resolved`` field name appear anywhere
    in the brief?" without modeling the brief's shape (design §6 rule 9: "walk
    the brief dict for the KEY name, don't over-model its shape"). A field name
    can surface two ways: as a dict key (``brief["resolved"]["cluster"]``) or as
    a string value (an ambiguity entry ``{"field": "result_dir_template", ...}``
    names the field in a VALUE). Both are collected as whole strings, so
    membership is exact — a field name mentioned only as a substring of prose
    never spuriously satisfies the gate.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                acc.add(key)
            _collect_brief_names(value, acc)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_brief_names(item, acc)
    elif isinstance(node, str):
        acc.add(node)


def _prior_nudge_named(experiment_dir: Path, spec: AppendDecisionInput, key: str) -> bool:
    """True iff a PRIOR non-greenlight record for this run names *key* in its text.

    Path (b) of the gate: a nudge (``response != "y"``) that explicitly named the
    field authorizes the human's intent for it even though no block brief
    recommended it. Reads the run's decision journal (append-only, so every
    record already present predates this append) and substring-matches the key
    name, case-insensitively, in each nudge's ``response`` text.
    """
    records = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)
    needle = key.lower()
    for rec in records:
        response = str(rec.get("response") or "")
        if response == "y":
            continue
        if needle in response.lower():
            return True
    return False


def _assert_brief_provenance(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Provenance gate (conduct rule 9): refuse a greenlight that diverts a
    ``resolved`` field the brief never recommended.

    Proving run #3: the agent hand-injected ``result_dir_template`` into a retry
    spec with no brief recommending it and no human nudge naming it — a silent
    LLM default. This gate mechanizes "never fabricate or divert a ``resolved``
    field the brief didn't recommend" (docs/design/proving-run-2-hardening.md §6).

    On a run-scoped greenlight (``response=="y"``), loads the LATEST brief the
    block persisted (``state.decision_briefs``, short-block-name tolerant). For
    every key in *resolved* it requires ONE of three legitimate provenances:

    * (a) the key appears anywhere in the persisted brief (a recommendation), or
    * (b) a prior nudge in this run's decision journal named the key, or
    * (c) the key is listed in this record's ``provenance["overrides"]``.

    ``next_block`` and other ``_META_KEYS`` routing tokens are exempt (they are
    machine-owned, not agent judgment).

    **Fail-open on absence, never on presence** (design constraint): no persisted
    brief for this ``(run, block)`` → the gate passes untouched (old runs,
    campaign scope, tests that never persist a brief). A PRESENT brief is always
    enforced.

    Raises :class:`errors.SpecInvalid` naming the diverging key(s) and the three
    legitimate paths.
    """
    if spec.scope_kind != "run" or str(spec.response or "") != "y":
        return
    if not isinstance(resolved, dict) or not resolved:
        return

    from hpc_agent._kernel.lifecycle.block_drive import _META_KEYS
    from hpc_agent.state.decision_briefs import latest_brief_for_block

    brief_record = latest_brief_for_block(experiment_dir, spec.scope_id, spec.block)
    if brief_record is None:
        # Fail-open on ABSENCE: nothing to diff against (design constraint 3).
        return

    brief_names: set[str] = set()
    _collect_brief_names(brief_record.get("brief"), brief_names)
    overrides = spec.provenance.get("overrides") if isinstance(spec.provenance, dict) else None
    override_keys = (
        {str(k) for k in overrides} if isinstance(overrides, (list, tuple, set)) else set()
    )

    diverging: list[str] = []
    for key in resolved:
        if key in _META_KEYS:
            continue
        if key in brief_names:  # (a) the brief recommended it
            continue
        if key in override_keys:  # (c) an explicit provenance override
            continue
        if _prior_nudge_named(experiment_dir, spec, key):  # (b) a nudge named it
            continue
        diverging.append(key)

    if diverging:
        raise errors.SpecInvalid(
            "provenance gate (conduct rule 9): greenlight for "
            f"{spec.block!r} diverts resolved field(s) {sorted(diverging)} that the "
            "persisted brief never recommended. A resolved field must be justified "
            "by ONE of: (a) the block's brief recommends it, (b) a prior nudge in "
            "this run's decision journal names it, or (c) it is listed in "
            "provenance.overrides. Do not hand-inject a spec field the brief did "
            "not surface (proving-run-2-hardening §6)."
        )


def _chain_successor(block: str) -> str | None:
    """The chain-table successor of *block*, or None when underivable.

    Matches the journaled block name against ``infra/block_chain.ORDER``
    verbatim first, then by ``-<short>`` suffix (records journal ``"s1"`` for
    ``submit-s1``). Multiple suffix matches (never true today — pinned by the
    derivation test) or a chain-final block return None: the default must never
    guess.
    """
    from hpc_agent.infra.block_chain import ORDER

    name = (block or "").strip().lower()
    if not name:
        return None
    matches: list[tuple[list[str], int]] = []
    for chain in ORDER.values():
        for idx, verb in enumerate(chain):
            if verb == name or verb.endswith(f"-{name}"):
                matches.append((chain, idx))
    if len(matches) != 1:
        return None
    chain, idx = matches[0]
    return chain[idx + 1] if idx + 1 < len(chain) else None


@primitive(
    name="read-decisions",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(
        help=(
            "Read a run's or campaign's decision journal — the "
            "append-ordered y/nudge audit trail (design §2)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ReadDecisionsInput,
        schema_ref=SchemaRef(input="read_decisions"),
    ),
    agent_facing=True,
)
def read_decisions(*, experiment_dir: Path, spec: ReadDecisionsInput) -> ReadDecisionsResult:
    """Read every decision record for a scope, oldest first.

    Returns an empty ``records`` list for a scope with no recorded
    touchpoints. Blank / individually-corrupt lines are skipped (a bad
    line never strands the rest of the trail).

    Raises
    ------
    :class:`errors.SpecInvalid`
        Unknown ``scope_kind`` or non-filesystem-safe ``scope_id``.
    """
    experiment_dir = Path(experiment_dir)
    raw = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)
    path = _decisions_path(experiment_dir, spec.scope_kind, spec.scope_id)
    return ReadDecisionsResult(
        path=str(path),
        records=[DecisionRecord.model_validate(r) for r in raw],
        count=len(raw),
    )
