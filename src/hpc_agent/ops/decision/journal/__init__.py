"""
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

Four trust-seam gates run before an append is persisted: the code-derived
field gate (:func:`_assert_no_code_derived_fields`, run #6 F1), the rule-9
brief-provenance gate (:func:`_assert_brief_provenance`), the
human-authorship gate (:func:`_assert_human_authorship`, proving run #4) and
the scope-unlock authorship gate (:func:`_assert_unlock_authorship`) — a scope
unlock RELAXES a caller restriction, so a bare ``y`` cannot enact it.
The authorship gate's evidence source is TIERED: when the harness-side
``UserPromptSubmit`` capture hook
(:mod:`hpc_agent._kernel.hooks.utterance_capture`) has logged utterances for
this repo, value tokens must derive from that log — text the harness, not
the agent, recorded — and the gate is a lock. Without a log (hook not
installed, older sessions) it falls back to journal ``response`` fields,
which the driving agent itself writes: FRICTION, not proof — what it
mechanically kills there is the observed rationalization class
(hand-injected fields, bare-``y`` laundering), not deliberate fabrication
of a human quote.

This module is the thin dispatching FACADE of the ``journal`` package: it hosts
the two agent-facing primitives (``append-decision`` / ``read-decisions``) and
re-exports every symbol the pre-split ``journal.py`` module exposed, so the
import path ``hpc_agent.ops.decision.journal`` is unchanged. The twelve
authorship gates live in the sibling submodules; the shared substrate lives in
``_shared``."""

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

from ._shared import (
    _AUTHORSHIP_EVIDENCE_MISSING,
    _BARE_ACK_RESPONSES,
    _FREE_TEXT_CALLER_FIELDS,
    _HA_MULTIPLIERS,
    _HA_NUM_RE,
    _HA_WORD_RE,
    _HEX_RUN_RE,
    _SCHEMA_ENUM_KEYS,
    _actor_scoped_human_texts,
    _assert_actor_policy,
    _collect_value_numbers,
    _collect_value_string_tokens,
    _conclusion_dossier_resolver,
    _contiguous_int_run,
    _fresh_authored_text,
    _fresh_human_texts,
    _ha_word_tokens,
    _harness_human_texts,
    _human_derivable,
    _human_number_pool,
    _is_bare_ack,
    _names_citation_sha_prefix,
    _names_slug,
    _names_target_sha_prefix,
    _newest_lock_ts,
    _read_interview_actors,
    _refuse_missing_authorship,
    _registration_authored_text,
    _session_actor,
    _target_record_ts,
)
from .brief_provenance import (
    _assert_brief_provenance,
    _collect_brief_names,
    _prior_nudge_named,
)
from .challenge import (
    _assert_challenge_authorship,
    _assert_challenge_filing_full,
    _assert_challenge_verdict_authorship,
    _challenge_filing_attestor,
    _challenge_filing_citations,
    _recompute_challenge_view_sha,
)
from .code_derived import (
    _assert_no_code_derived_fields,
)
from .conclusion import (
    _assert_conclusion_authorship,
    _assert_conclusion_full,
    _assert_conclusion_revoke_floor,
)
from .human_authorship import (
    _assert_human_authorship,
)
from .overnight_consent import (
    _assert_overnight_consent_authorship,
    _bound_consent_records,
    _compose_overnight_consent,
)
from .registration import (
    _REGISTRATION_REQUIRED_KEYS,
    _assert_conformance_baseline_membership,
    _assert_conformance_verdict_authorship,
    _assert_registration_authorship,
    _assert_registration_full,
    _assert_registration_review_floor,
    _assert_revoke_floor,
    _field_present,
    _names_any_sha_prefix,
    _names_sha_prefix,
    _valid_review_horizon,
)
from .reproduction import (
    _assert_reproduction_verdict_authorship,
    _match_ledger_sha_prefix,
)
from .scope_lock import (
    _SCOPE_UNLOCK_BLOCK,
    _assert_unlock_authorship,
)
from .signoff import (
    _SIGNOFF_BLOCK,
    _SIGNOFF_IDENT_RE,
    _assert_signoff_authorship,
    _assert_signoff_render_current,
    _assert_signoff_reviewer_not_author,
    _read_interview_audited_source,
    _read_signoff_source_text,
    _resolve_signoff_audit_config,
    _section_specific_tokens,
    _signoff_fresh_human_texts,
    _signoff_token_names,
)


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
    resolved = _compose_overnight_consent(experiment_dir, spec, resolved)
    _assert_no_code_derived_fields(resolved)
    _assert_brief_provenance(experiment_dir, spec, resolved)
    _assert_human_authorship(experiment_dir, spec, resolved)
    _assert_unlock_authorship(experiment_dir, spec, resolved)
    _assert_signoff_authorship(experiment_dir, spec, resolved)
    _assert_registration_authorship(experiment_dir, spec, resolved)
    _assert_reproduction_verdict_authorship(experiment_dir, spec, resolved)
    _assert_conclusion_authorship(experiment_dir, spec, resolved)
    _assert_challenge_authorship(experiment_dir, spec, resolved)
    _assert_overnight_consent_authorship(experiment_dir, spec, resolved)
    # Multi-human (docs/design/multi-human.md MH4/MH8): resolve the session actor
    # server-side (NEVER a caller-suppliable spec field — the model must not choose
    # its identity), enforce the MH8 delegation policy for this block, and stamp the
    # resolved actor as the record's ``attestor_id``. All three are NO-OPS under
    # zero/one declared actor (``_session_actor`` → None, ``_assert_actor_policy``
    # returns silently, ``attestor_id=None`` is omitted on disk) — byte-identical.
    _actor_ids, _actor_policy = _read_interview_actors(experiment_dir)
    attestor_id = _session_actor(experiment_dir, _actor_ids) if len(_actor_ids) > 1 else None
    _assert_actor_policy(_actor_ids, _actor_policy, spec.block, attestor_id)
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
        attestor_id=attestor_id,
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


__all__ = [
    "_AUTHORSHIP_EVIDENCE_MISSING",
    "_BARE_ACK_RESPONSES",
    "_FREE_TEXT_CALLER_FIELDS",
    "_HA_MULTIPLIERS",
    "_HA_NUM_RE",
    "_HA_WORD_RE",
    "_HEX_RUN_RE",
    "_REGISTRATION_REQUIRED_KEYS",
    "_SCHEMA_ENUM_KEYS",
    "_SCOPE_UNLOCK_BLOCK",
    "_SIGNOFF_BLOCK",
    "_SIGNOFF_IDENT_RE",
    "_actor_scoped_human_texts",
    "_assert_actor_policy",
    "_assert_brief_provenance",
    "_assert_challenge_authorship",
    "_assert_challenge_filing_full",
    "_assert_challenge_verdict_authorship",
    "_assert_conclusion_authorship",
    "_assert_conclusion_full",
    "_assert_conclusion_revoke_floor",
    "_assert_conformance_baseline_membership",
    "_assert_conformance_verdict_authorship",
    "_assert_human_authorship",
    "_assert_no_code_derived_fields",
    "_assert_overnight_consent_authorship",
    "_assert_registration_authorship",
    "_assert_registration_full",
    "_assert_registration_review_floor",
    "_assert_reproduction_verdict_authorship",
    "_assert_revoke_floor",
    "_assert_signoff_authorship",
    "_assert_signoff_render_current",
    "_assert_signoff_reviewer_not_author",
    "_assert_unlock_authorship",
    "_bound_consent_records",
    "_chain_successor",
    "_challenge_filing_attestor",
    "_challenge_filing_citations",
    "_collect_brief_names",
    "_collect_value_numbers",
    "_collect_value_string_tokens",
    "_compose_overnight_consent",
    "_conclusion_dossier_resolver",
    "_contiguous_int_run",
    "_default_next_block",
    "_field_present",
    "_fresh_authored_text",
    "_fresh_human_texts",
    "_ha_word_tokens",
    "_harness_human_texts",
    "_human_derivable",
    "_human_number_pool",
    "_is_bare_ack",
    "_match_ledger_sha_prefix",
    "_names_any_sha_prefix",
    "_names_citation_sha_prefix",
    "_names_sha_prefix",
    "_names_slug",
    "_names_target_sha_prefix",
    "_newest_lock_ts",
    "_prior_nudge_named",
    "_read_interview_actors",
    "_read_interview_audited_source",
    "_read_signoff_source_text",
    "_recompute_challenge_view_sha",
    "_refuse_missing_authorship",
    "_registration_authored_text",
    "_resolve_signoff_audit_config",
    "_section_specific_tokens",
    "_session_actor",
    "_signoff_fresh_human_texts",
    "_signoff_token_names",
    "_target_record_ts",
    "_valid_review_horizon",
    "append_decision",
    "read_decisions",
]
