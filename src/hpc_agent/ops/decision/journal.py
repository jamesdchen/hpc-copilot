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
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

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
from hpc_agent.infra.env_flags import env_actor
from hpc_agent.state.decision_journal import append_decision as _append_decision
from hpc_agent.state.decision_journal import decisions_path as _decisions_path
from hpc_agent.state.decision_journal import read_decisions as _read_decisions
from hpc_agent.state.registration import (
    CONFORMANCE_VERDICT_BLOCK,
    REGISTRATION_BLOCK,
    REGISTRATION_BLOCK_FAMILY,
    REGISTRATION_REVIEW_BLOCK,
    REVOKE_BLOCK,
    SUBJECT_KIND,
)

# ── E2: the authorship-refusal marker (docs/design/mcp-elicitation.md D4/E2) ──

# The machine-readable discriminator the MCP elicitation hook keys on to detect
# an authorship/sign-off refusal WITHOUT parsing prose. It rides the additive,
# contractually-open ``failure_features`` block: ``cli/_helpers.py::_err_from_hpc``
# lifts ``getattr(exc, "failure_features", None)`` verbatim into the ok:false
# envelope and the MCP ``structuredContent`` preserves it. Precedence is the seam
# that makes this safe: a NON-None ``exc.failure_features`` WINS over the
# synthesized ``_spec_invalid_failure_features`` default (that default fires only
# when the attribute is ABSENT), so the marker is never clobbered — and, because
# that default synthesizes a ``failure_features`` block for EVERY spec_invalid,
# consumers must key on the distinct ``authorship_evidence`` KEY, never on the
# block's mere presence. The gate stays 100% harness-agnostic: it names a refusal
# CAUSE ("the human's authorship evidence is missing"), never a transport — the
# retry-after-elicit seam lives in the MCP layer (D4), not here.
_AUTHORSHIP_EVIDENCE_MISSING = {"authorship_evidence": "missing"}


def _refuse_missing_authorship(message: str) -> NoReturn:
    """Raise a ``spec_invalid`` refusal carrying the E2 authorship-missing marker.

    Used ONLY for the authorship-BAR raise sites — the refusals a freshly typed
    human sign-off / rationale would resolve (a bare ack, an un-named section
    slug, an unengaged human-required section, a required-caller value with no
    human-attributed utterance). Structural / setup refusals in the same gates (a
    stale hash, an unresolvable source / template, a missing or stale render, a
    moved view ingredient, a malformed ``resolved``, a block-convention
    violation) are deliberately NOT marked: re-eliciting an utterance cannot fix
    them, so an MCP retry-once keyed on the marker would be a guaranteed-failing
    round-trip (D4's retry re-checks the gate against the now-present utterance).
    """
    exc = errors.SpecInvalid(message)
    exc.failure_features = dict(_AUTHORSHIP_EVIDENCE_MISSING)  # type: ignore[attr-defined]
    raise exc


# ── multi-human actor substrate (docs/design/multi-human.md MH1/MH4/MH6/MH8) ──
#
# Every helper here is a NO-OP under the single-actor world (zero or one declared
# actor): the identity comparisons and policy consultation only mean something in
# a group, and the byte-identity pin (enforcement rows) requires that a session
# with fewer than two declared actors behaves byte-for-byte as before multi-human
# — no new refusal, no attestor_id stamp, no policy read. The guard is ALWAYS the
# ``len(ids) > 1`` census: the plumbing may resolve a session actor for stamping,
# but no COMPARISON fires without the >1 declaration.


def _read_interview_actors(experiment_dir: Path) -> tuple[list[str], dict[str, list[str]]]:
    """The interview.json ``actors`` block as ``(ids, policy)`` — or ``([], {})``.

    Reads the same interview.json the sign-off / audit gates already read
    (``_read_interview_audited_source`` posture: campaign-dir root first,
    ``.hpc/interview.json`` defensively; a corrupt / non-object file, or an absent
    / malformed ``actors`` block, is tolerated as "no actors declared" → the
    single-actor world, byte-identical). ``ids`` is the declared actor slugs;
    ``policy`` is the optional ``{block: [slug, ...]}`` delegation mapping (MH8),
    ``{}`` when absent. Core never interprets the slugs — it compares identity.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        block = doc.get("actors")
        if not isinstance(block, dict):
            return [], {}
        raw_ids = block.get("ids")
        ids = [str(i) for i in raw_ids if isinstance(i, str)] if isinstance(raw_ids, list) else []
        raw_policy = block.get("policy")
        policy: dict[str, list[str]] = {}
        if isinstance(raw_policy, dict):
            for key, allowed in raw_policy.items():
                if isinstance(key, str) and isinstance(allowed, list):
                    policy[key] = [str(a) for a in allowed if isinstance(a, str)]
        return ids, policy
    return [], {}


def _session_actor(experiment_dir: Path, ids: list[str] | None = None) -> str | None:
    """The resolved session actor slug, or ``None`` when unattributed (MH4).

    Reads ``HPC_ACTOR`` out-of-band (``infra/env_flags.py::env_actor`` — the slug
    arrives from OUTSIDE the model's tool surface, exactly like the utterance
    text, and is validated as a filesystem-safe slug there). The resolved slug is
    accepted ONLY when it is one of the *declared* ``actors.ids`` — an
    ``HPC_ACTOR`` naming an undeclared actor is UNRESOLVABLE (``None``), never a
    silently-trusted identity. ``None`` also when the env is unset/blank/invalid,
    or when no actors are declared. Core NEVER verifies who set the env var (the
    harness-asserted attribution tier); it only compares the opaque slug.
    """
    if ids is None:
        ids, _ = _read_interview_actors(experiment_dir)
    actor = env_actor()
    if actor is not None and actor in set(ids):
        return actor
    return None


def _actor_scoped_human_texts(experiment_dir: Path, ids: list[str]) -> list[str] | None:
    """The harness-captured evidence pool, actor-scoped under >1 declared actors (MH4).

    * Zero/one declared actor → the union read (``_harness_human_texts`` with no
      actor), byte-identical to the pre-multi-human evidence tier.
    * >1 declared, session actor resolves → the SESSION ACTOR'S suffixed log ONLY
      (``read_utterances(actor=<slug>)`` via ``_harness_human_texts`` — the
      anti-laundering exclusion: actor A's agent cannot commit a value only actor
      B ever typed, and the unsuffixed anonymous log never satisfies a scoped
      check).
    * >1 declared, session UNATTRIBUTED (no resolvable actor) → ``None`` so the
      gate falls to the journal-response FRICTION tier: anonymous text must never
      satisfy an actor-specific evidence check (the MT8 contract sentence, now
      enforced). It does NOT fall back to the union — that would re-open
      laundering.
    """
    if len(ids) > 1:
        actor = _session_actor(experiment_dir, ids)
        if actor is None:
            return None
        return _harness_human_texts(experiment_dir, actor=actor)
    return _harness_human_texts(experiment_dir)


# ── B4 ts>=anchor fix-wave: ONE shared utterance-freshness filter ─────────────
#
# The finding-10 pattern (``_signoff_fresh_human_texts``) generalized: every
# authorship gate whose evidence is NAMING over the utterance log — scope-unlock
# and the four naming-only revoke/verdict floors — bounds the harness-utterance
# pool to records logged AT OR AFTER the target's own timestamp. Without the
# bound the naming leg is permanently satisfied by the very utterance that
# CREATED the target (the human named every id at creation), so a later
# agent-composed revoke/verdict/unlock rides through (the B4 exposure, philosophy
# audit 2026-07 sweep log). The sha-prefix-bound FILING gates need no anchor (an
# 8-hex prefix cannot pre-exist the artifact it fingerprints — temporal binding
# by vocabulary impossibility); the overnight-consent gate parks on USER RULING 3
# (no natural anchor). Both are left on the unbounded reader with a documented
# exemption (the route-through contract test pins this).


def _fresh_human_texts(
    experiment_dir: Path, *, actor_ids: list[str], anchor: float | None
) -> list[str] | None:
    """Actor-scoped harness utterances filtered to ``ts >= anchor`` (finding-10, generalized).

    THE shared temporal filter every naming-over-the-log authorship gate routes
    through — scope-unlock and the four revoke/verdict floors (the B4 fix-wave),
    plus :func:`_signoff_fresh_human_texts`, which supplies the render mtime as
    *anchor*. A human can only attest a target that EXISTED when they typed, so an
    utterance older than the target's own timestamp is not attestation.

    Tiering mirrors :func:`_actor_scoped_human_texts`:

    * ``None`` — no log at all, or an unattributed >1-actor session — signals the
      caller to fall to the journal-response FRICTION tier (byte-identical to the
      unfiltered scoped read's ``None``).
    * *anchor* ``None`` — the anchor could not be determined — returns the
      UNFILTERED pool: the missing-anchor case a re-elicited utterance cannot fix
      (each gate's own existence refusal owns a never-created target), mirroring
      :func:`_signoff_fresh_human_texts`'s absent-render posture.
    * otherwise — only utterances with a parseable ``ts`` at or after
      ``int(anchor)`` (utterance ``ts`` is seconds-resolution). An EMPTY list —
      the log exists but nothing fresh names the target — makes the gate refuse
      (the authorship marker / popup cue). A record with no parseable ``ts`` is
      EXCLUDED (conservative).
    """
    from hpc_agent.infra.time import parse_iso_utc
    from hpc_agent.state.utterances import read_utterances

    if len(actor_ids) > 1:
        actor = _session_actor(experiment_dir, actor_ids)
        if actor is None:
            return None
        records = read_utterances(experiment_dir, actor=actor)
    else:
        records = read_utterances(experiment_dir)
    if not records:
        return None
    if anchor is None:
        return [str(r.get("text") or "") for r in records]
    floor = int(anchor)
    fresh: list[str] = []
    for rec in records:
        ts = rec.get("ts")
        if not isinstance(ts, str) or not ts:
            continue
        try:
            when = parse_iso_utc(ts).timestamp()
        except (TypeError, ValueError):
            continue
        if when >= floor:
            fresh.append(str(rec.get("text") or ""))
    return fresh


def _fresh_authored_text(experiment_dir: Path, response: str, *, anchor: float | None) -> str:
    """The human-authored text a NAMING-only revoke/verdict gate reads, ts-bound.

    The :func:`_registration_authored_text` posture with the B4 ``ts >= anchor``
    filter layered in: with the harness utterance log present the naming token
    must derive from an utterance logged at or after *anchor* (the target
    record's own ts — a revoke/verdict must post-date what it resolves). Absent
    the log, or an unattributed >1-actor session, the agent-relayed ``response``
    is the friction tier (byte-identical to the pre-B4 fallback). An *anchor* of
    ``None`` (no parseable target ts) leaves the pool unfiltered; the gate's own
    existence check owns the never-created case.
    """
    actor_ids, _ = _read_interview_actors(experiment_dir)
    fresh = _fresh_human_texts(experiment_dir, actor_ids=actor_ids, anchor=anchor)
    if fresh is not None:
        return "\n".join(fresh)
    return response


def _target_record_ts(
    experiment_dir: Path,
    *,
    scope_kind: str,
    scope_id: str,
    filing_block: str,
    id_field: str,
    target_id: str,
) -> float | None:
    """Epoch-seconds ts of the NEWEST filing that created *target_id*, or ``None``.

    The B4 anchor for a revoke/verdict gate: the record it resolves must already
    exist, so that filing's own ``ts`` bounds the utterance pool the naming leg
    reads (a revoke authored before the filing existed is the creation utterance
    re-read, not attestation). Scans *target_id*'s journal thread for the newest
    record on *filing_block* whose ``resolved[id_field] == target_id`` and parses
    its ``ts``. ``None`` when no such parseable filing exists — the caller then
    leaves the pool unfiltered (a re-elicit cannot conjure a filing that was never
    made; each floor's existence refusal owns that case).
    """
    from hpc_agent.infra.time import parse_iso_utc

    newest: float | None = None
    for rec in _read_decisions(experiment_dir, scope_kind, scope_id):
        if rec.get("block") != filing_block:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get(id_field) != target_id:
            continue
        ts = rec.get("ts")
        if not isinstance(ts, str) or not ts:
            continue
        try:
            when = parse_iso_utc(ts).timestamp()
        except (TypeError, ValueError):
            continue
        if newest is None or when > newest:
            newest = when
    return newest


def _newest_lock_ts(experiment_dir: Path, scope_id: str) -> float | None:
    """Epoch-seconds ts of the scope's NEWEST lock record, or ``None`` (B4 anchor).

    An unlock re-opens a scope, so its rationale must post-date the LOCK it
    re-opens: the newest ``scope-lock`` record whose ``resolved.scope_action`` is
    ``lock`` is the anchor. ``None`` when the scope has no lock record on file —
    the pool is then unfiltered (nothing yet to have post-dated; the unlock's
    bare-ack + overlap legs still gate it).
    """
    from hpc_agent.infra.time import parse_iso_utc
    from hpc_agent.state.scopes import _SCOPE_LOCK_BLOCK

    newest: float | None = None
    for rec in _read_decisions(experiment_dir, "scope", scope_id):
        if rec.get("block") != _SCOPE_LOCK_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get("scope_action") != "lock":
            continue
        ts = rec.get("ts")
        if not isinstance(ts, str) or not ts:
            continue
        try:
            when = parse_iso_utc(ts).timestamp()
        except (TypeError, ValueError):
            continue
        if newest is None or when > newest:
            newest = when
    return newest


def _assert_actor_policy(
    ids: list[str], policy: dict[str, list[str]], block: str, actor: str | None
) -> None:
    """MH8 delegation gate: refuse a policy-restricted block by a non-member actor.

    Pure lists+mappings core COMPARES, never evaluates (the domain-packs pattern
    applied to people). Silent unless >1 actor is declared AND ``policy`` maps
    *block* to a member list: then the session *actor* must be ``in`` that list —
    a non-member (including an unresolvable ``None`` session actor) is refused,
    naming the block and the allowed set. A block absent from the policy is
    unrestricted (policy is opt-in per block); no ``actors`` / ``policy`` at all
    is silent (byte-identical). IDENTITY + COUNTING only — core never learns WHY
    the lab granted a block to an actor.
    """
    if len(ids) <= 1:
        return
    allowed = policy.get(block)
    if not allowed:
        return  # no policy entry for this block → unrestricted
    if actor is None or actor not in set(allowed):
        raise errors.SpecInvalid(
            f"actor-policy gate (MH8): block {block!r} is delegated by "
            f"actors.policy to {sorted(allowed)!r}; the session actor "
            f"{actor!r} is not a member and may not author it. Configure "
            "HPC_ACTOR to a delegated actor, or amend actors.policy in "
            "interview.json (a policy entry is caller-declared delegation, "
            "never a role core interprets)."
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


def _assert_no_code_derived_fields(resolved: dict[str, Any] | None) -> None:
    """Refuse a ``resolved`` dict that hand-commits a CODE-DERIVED field.

    Run #6 finding F1: the driving agent hand-authored the sidecar's
    ``executor`` as the bare extension-less token ``monte_carlo_pi``; the
    dispatcher shelled it verbatim and exited 127 (canary_failed). The
    ``revise-resolved`` patch surface already refuses derived fields, but the
    journal's ``resolved`` was still an authorable side door — a greenlight
    committing ``executor``/``job_env``/… laundered a hand-authored derived
    value into the approved spec the driver then carries (§4 carry_fields).

    The refusal set is
    :data:`~hpc_agent.ops.submit.field_partition.JOURNAL_UNAUTHORABLE_FIELDS`
    (bound through the ``field_ownership`` facade, never copied) — the
    code-derived partition MINUS the three names a committed ``resolved``
    legitimately carries (``run_id``: a status/aggregate INPUT field;
    ``cmd_sha``: the §4 identity fast-path token ``block_drive`` reads;
    ``total_tasks``: count echoes are cross-checked against ``tasks.total()``
    downstream, finding 21). Scoping by audit keeps the guard fireable
    without breaking any green path (engineering-principles).

    Applies to EVERY append (any scope, any response): a derived value has no
    business in the journal regardless of how it got there. Raises
    :class:`errors.SpecInvalid` naming the field(s) and the sanctioned rail
    (``revise-resolved`` with the INPUT field to patch instead).
    """
    if not isinstance(resolved, dict) or not resolved:
        return
    # Bind (never copy) the partition through the top-level facade — the
    # direct ``hpc_agent.ops.submit.field_partition`` spelling trips the
    # subject-import lint from inside the ``decision`` subject.
    from hpc_agent.ops import field_ownership as _field_ownership

    offending = sorted(k for k in resolved if k in _field_ownership.JOURNAL_UNAUTHORABLE_FIELDS)
    if offending:
        raise errors.SpecInvalid(
            f"append-decision: resolved field(s) {offending} are CODE-DERIVED — "
            "the framework recomputes them from the input delta (executor from "
            "the interview's materialized entry, job_env/modules/conda_* from "
            "the cluster's clusters.yaml entry, ssh_target/backend/remote_path "
            "from the cluster). Hand-committing one is the run-#6 F1 bug (a "
            "hand-authored bare `executor` shelled verbatim → exit 127). Do not "
            "journal the derived value: name the INPUT field that should change "
            "via `hpc-agent revise-resolved` (e.g. to change the executor, "
            "patch `entry_point`; to change activation, patch `cluster`) and "
            "commit THAT delta instead."
        )


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
    import re

    records = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)
    needle = key.lower()
    for rec in records:
        response = str(rec.get("response") or "")
        if response == "y":
            continue
        # Token-exact, not substring (#26): a nudge must NAME the field. The
        # substring form let an unrelated mention authorize a diverted field —
        # e.g. key "seed" matched "seeds 0-19", or "run" matched "running". Split
        # on non-identifier chars and require the whole key as a token.
        if needle in set(re.split(r"[^a-z0-9_]+", response.lower())):
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
    field the brief didn't recommend" (docs/design/history/proving-run-2-hardening.md §6).

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


# ── human-authorship gate (conduct rule 9 extension, proving run #4) ──────────

# REQUIRED_CALLER fields whose value is free-text intent (no structured tokens
# to extract) — checked with the softer non-bare-response / word-overlap rule.
# Everything else in REQUIRED_CALLER_FIELDS (today: task_generator) is checked
# by deterministic token derivation — both the NUMBER tokens it asserts and,
# since finding 25, its non-numeric CATEGORICAL string claims.
_FREE_TEXT_CALLER_FIELDS = frozenset({"goal"})

# Responses that are a bare acknowledgement — they carry no authored content,
# so they cannot commit a required-caller value that appears only in the
# agent's proposal. Compared after lowercasing and squashing non-letters.
_BARE_ACK_RESPONSES = frozenset(
    {
        "y",
        "yes",
        "yep",
        "yeah",
        "ok",
        "okay",
        "sure",
        "fine",
        "go",
        "go ahead",
        "proceed",
        "continue",
        "confirm",
        "confirmed",
        "approve",
        "approved",
        "lgtm",
        "do it",
        "sounds good",
        "looks good",
    }
)


def _is_bare_ack(response: str) -> bool:
    """True when *response* is a bare acknowledgement (``y`` / ``ok`` / ...)."""
    norm = re.sub(r"[^a-z]+", " ", (response or "").lower()).strip()
    return norm in _BARE_ACK_RESPONSES


# Number tokens in human text / field values, adapted from verify-relay's
# claim-extraction idiom: ints, floats, comma- or underscore-grouped values,
# plus an attached k/M/B magnitude suffix ("1M samples" states 1_000_000).
# A comma counts as GROUPING only in 3-digit groups ("1,000,000"); anything
# else is an enumeration separator — proving run #5's `\d[\d,_]*` collapsed
# the human's typed "seeds 0,1,2,...,19" into one giant token, and the gate
# then made them retype it space-separated.
_HA_NUM_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d[\d_]*(?:\.\d+)?)([kKmMbB])?(?![A-Za-z0-9_])"
)
_HA_MULTIPLIERS = {"k": 1_000.0, "m": 1_000_000.0, "b": 1_000_000_000.0}

# Word tokens (>= 4 chars) for the free-text overlap check.
_HA_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


def _ha_word_tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _HA_WORD_RE.finditer(text or "")}


def _harness_human_texts(experiment_dir: Path, actor: str | None = None) -> list[str] | None:
    """The logged human utterances' texts, or ``None`` when none were captured.

    The harness-captured evidence tier BOTH authorship gates share
    (:func:`_assert_human_authorship`, :func:`_assert_unlock_authorship`): the
    ``UserPromptSubmit`` capture hook (:mod:`hpc_agent._kernel.hooks.utterance_capture`)
    writes each human prompt to :func:`hpc_agent.state.utterances.read_utterances`
    out-of-band, so this is text a human verifiably typed — not the
    agent-authored journal ``response``. ``None`` (no log / older session)
    signals the caller to fall back to the journal-response friction tier;
    a present-but-empty case reads the same.

    *actor* (MH4): ``None`` → the UNION of every log (today's identity-less read,
    byte-identical). An actor slug → that actor's suffixed log ONLY — the
    anti-laundering scoped read the >1-actor authorship tier uses
    (:func:`_actor_scoped_human_texts` is the caller that decides which). The
    unsuffixed anonymous log is EXCLUDED from a scoped read by
    ``read_utterances`` itself.
    """
    from hpc_agent.state.utterances import read_utterances

    utterances = read_utterances(experiment_dir, actor=actor)
    if not utterances:
        return None
    return [str(u.get("text") or "") for u in utterances]


def _human_number_pool(texts: list[str]) -> tuple[set[str], set[float]]:
    """Every number a human utterance stated, as normalized strings + floats.

    Grouping commas/underscores are normalized away; an attached magnitude
    suffix contributes the expanded value too (``1M`` → ``1`` and
    ``1000000``), so "50 seeds at 1M samples" supports ``samples=1_000_000``.
    """
    strings: set[str] = set()
    floats: set[float] = set()
    for text in texts:
        for m in _HA_NUM_RE.finditer(text or ""):
            norm = m.group(1).replace(",", "").replace("_", "")
            strings.add(norm)
            try:
                val = float(norm)
            except ValueError:
                continue
            floats.add(val)
            suffix = m.group(2)
            if suffix:
                expanded = val * _HA_MULTIPLIERS[suffix.lower()]
                floats.add(expanded)
                strings.add(str(int(expanded)) if expanded == int(expanded) else str(expanded))
    return strings, floats


def _contiguous_int_run(obj: Any) -> tuple[int, int] | None:
    """``(lo, hi)`` when *obj* is a list of ≥3 consecutive ascending ints.

    Proving run #5: ``items_x_seeds`` materializes the sweep as ``seeds:
    [0..19]``, but a human states it as "20 seeds" or "seeds 0 through 19" —
    never by enumerating twenty integers (the gate forced exactly that, and
    the friction was the finding). Interior members of a consecutive run are
    DERIVED from its endpoints, not independently asserted, so only lo / hi /
    length face the derivability check. A non-consecutive list (``[0, 5, 10]``)
    still asserts every member.
    """
    if not isinstance(obj, (list, tuple)) or len(obj) < 3:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in obj):
        return None
    if any(b - a != 1 for a, b in zip(obj, obj[1:], strict=False)):
        return None
    return int(obj[0]), int(obj[-1])


def _collect_value_numbers(obj: Any, out: dict[str, float]) -> None:
    """Gather every number token a required-caller field VALUE asserts.

    Scalars contribute their value; strings contribute their embedded number
    tokens (grouping commas/underscores normalized, so ``samples=1_000_000``
    asserts ``1000000``); dicts contribute their values (keys are schema
    vocabulary, not claims). Bools are skipped. A consecutive-int list
    (:func:`_contiguous_int_run`) asserts only its endpoints and length — the
    range form a human actually states.
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        val = float(obj)
        norm = str(int(val)) if val == int(val) else str(val)
        out.setdefault(norm, val)
        return
    if isinstance(obj, str):
        for m in _HA_NUM_RE.finditer(obj):
            norm = m.group(1).replace(",", "").replace("_", "")
            try:
                out.setdefault(norm, float(norm))
            except ValueError:
                continue
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_value_numbers(v, out)
        return
    if isinstance(obj, (list, tuple)):
        run = _contiguous_int_run(obj)
        if run is not None:
            lo, hi = run
            for member in (lo, hi, len(obj)):
                _collect_value_numbers(member, out)
            return
        for v in obj:
            _collect_value_numbers(v, out)


# Discriminator keys whose VALUE is a schema-union arm name, not a caller
# claim ("kind": "items_x_seeds" / "cartesian_product"). Its value is
# framework vocabulary — the human never types it — so it is exempt from the
# categorical authorship check (finding 25). Dict KEYS everywhere are schema
# vocabulary too and are never collected; only VALUE string leaves are.
_SCHEMA_ENUM_KEYS = frozenset({"kind"})


def _collect_value_string_tokens(obj: Any, out: set[str]) -> None:
    """Gather word tokens from every caller-CLAIM string VALUE leaf.

    Proving run #5 finding 25: :func:`_collect_value_numbers` checks only the
    NUMBER tokens a required-caller value asserts, so a fabricated
    CATEGORICAL/string param (a ``dataset`` axis the human never named) rode
    through whenever the numbers happened to derive. This gathers the
    non-numeric claim tokens the number check ignores, so they face the same
    human-derivability bar.

    Two positions are schema vocabulary — never a claim — and are excluded:

    * dict KEYS (``params`` / ``items`` / ``seeds``, a cartesian axis NAME) —
      the schema's own field names. Only VALUE leaves are collected, so a
      key never spuriously satisfies (or triggers) the check.
    * the value of a discriminator key (``kind``: ``items_x_seeds``) — it
      names a union arm, not a claim (:data:`_SCHEMA_ENUM_KEYS`).

    Bools/numbers are skipped (:func:`_collect_value_numbers` owns them); a
    pure-number string (``"0-49"``) contributes no word tokens and so is
    silently a no-op here.
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        return
    if isinstance(obj, str):
        out |= _ha_word_tokens(obj)
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key in _SCHEMA_ENUM_KEYS:
                continue  # the discriminator value is schema vocabulary, not a claim
            _collect_value_string_tokens(value, out)
        return
    if isinstance(obj, (list, tuple)):
        for item in obj:
            _collect_value_string_tokens(item, out)


def _human_derivable(val: float, norm: str, strings: set[str], floats: set[float]) -> bool:
    """True when a value number is derivable from the human number pool.

    Derivable means: stated verbatim (normalized-string or float equality),
    zero (a 0-based range start is derived, never asserted), or an integral
    off-by-one of a stated count (``seeds (0-19)`` derives from "20 seeds":
    19 == 20 - 1 — the range-endpoint form of a stated count).
    """
    if norm in strings or val in floats:
        return True
    if val == 0:
        return True
    return val == int(val) and ((val + 1) in floats or (val - 1) in floats)


def _assert_human_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship gate: refuse committing a REQUIRED_CALLER field whose
    value has no human-attributed utterance on record.

    Proving run #4: the driving agent FABRICATED a ``task_generator`` ("20
    seeds × 1M samples") by reading the executor, presented it as a
    recommendation, and the human's bare ``y`` laundered it into ``resolved``
    as "caller-supplied". The field partition's no-fabricate lock
    (:mod:`hpc_agent.ops.submit.field_partition`) held — no safe_default can
    exist for a required-caller field — but nothing distinguished
    human-authored from agent-authored caller values at the commit point.
    This gate closes that seam at ``append-decision``, beside the rule-9
    brief-provenance gate.

    **Trigger** — the record's ``resolved`` introduces a
    :data:`~hpc_agent.ops.submit.field_partition.REQUIRED_CALLER_FIELDS`
    member (imported, never redefined; today ``goal`` / ``task_generator``)
    for the FIRST time in this scope's journal. A field already present in a
    prior record's ``resolved`` was gated when it was committed — subsequent
    decisions are unaffected.

    **Check** — the value must be derivable from HUMAN text, taken from the
    strongest source available:

    * **Utterance log present** (the ``UserPromptSubmit`` capture hook,
      :mod:`hpc_agent._kernel.hooks.utterance_capture`, has logged prompts
      for this repo — :func:`hpc_agent.state.utterances.read_utterances`):
      the human texts are the LOGGED UTTERANCES, written by the harness
      out-of-band. Journal ``response`` fields — agent-authored — carry no
      authorship weight in this mode: a substantive ``response`` cannot
      commit a free-text field, and response numbers cannot support a
      structured one. This is the lock the v1 gate staged.
    * **No utterance log** (hook not installed / older sessions —
      back-compat fail-open): this record's ``response`` plus every prior
      record's ``response`` in the scope's journal (a prior nudge that
      stated the sweep authorizes a later bare ``y``).

    Per-field rules, against the chosen human texts:

    * Structured fields (``task_generator``): every number token the value
      asserts must be human-derivable (:func:`_human_derivable` — verbatim,
      magnitude-suffixed like ``1M``, zero, or a range endpoint of a stated
      count). A consecutive-int list asserts only its endpoints and length
      (:func:`_contiguous_int_run` — "20 seeds" derives ``seeds=[0..19]``;
      proving run #5's finding was the gate demanding the enumeration). Its
      non-numeric CATEGORICAL string claims must ALSO overlap the human word
      pool (:func:`_collect_value_string_tokens`, finding 25) — schema
      vocabulary (dict keys, the ``kind`` discriminator value) exempt — so a
      fabricated ``dataset`` axis cannot ride a passing number check. A value
      with no number OR string claims falls back to the free-text rule.
    * Free-text fields (``goal``): the value's word tokens must overlap some
      human text; in journal-response mode only, a non-bare committing
      ``response`` (:func:`_is_bare_ack`) also commits it.

    **Fail-open** only in journal-response mode where the journal genuinely
    lacks response text: prior records exist and NONE carries a ``response``
    key (old-schema journals). An empty/absent value is not a commit and is
    never gated.

    **Honesty — the trust model is tiered, not uniform.** With the capture
    hook installed, the authorship evidence is HARNESS-captured: the hook
    fires before the model sees the prompt, so the utterance log is text a
    human verifiably typed, and this gate is a lock — "a guard the LLM
    itself satisfies is not a guard" (engineering-principles) no longer
    applies to the evidence source. Without the hook, the fallback evidence
    (journal ``response`` fields) is agent-authored, and the v1 friction
    posture stands: it kills the OBSERVED failure — bare-``y`` laundering,
    rationalization rather than fraud — but a determined agent could still
    fabricate a human quote. Refusing the fallback outright would break
    every pre-hook install, so back-compat wins there.

    Raises :class:`errors.SpecInvalid` naming the field(s), the underivable
    token(s), the evidence source consulted, and the remedy (the human
    states the sweep in a prompt / their reply).
    """
    if not isinstance(resolved, dict) or not resolved:
        return

    # Import (never redefine) the required-caller partition — one source of
    # truth with the no-fabricate Ambiguity lock (field_partition docstring).
    # Reached through the top-level ``field_ownership`` facade via the package
    # alias form: the direct ``hpc_agent.ops.submit.field_partition`` spelling
    # trips the subject-import lint from inside the ``decision`` subject (see
    # scripts/lint_subject_imports.py and the harvest_guard precedent).
    from hpc_agent.ops import field_ownership as _field_ownership

    candidates = [
        f
        for f in sorted(_field_ownership.REQUIRED_CALLER_FIELDS)
        if f in resolved and resolved[f] not in (None, "", {}, [])
    ]
    if not candidates:
        return

    prior = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)

    # Tiered evidence source: prefer the harness-captured utterance log (the
    # lock) over agent-authored journal responses (the friction fallback). Under
    # >1 declared actors the pool is the SESSION ACTOR'S log only (MH4 — actor A's
    # agent cannot commit a value only actor B ever typed); an unattributed
    # >1-actor session falls to the friction tier (never the anonymous union).
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    harness_texts = _actor_scoped_human_texts(experiment_dir, _actor_ids)
    harness_captured = harness_texts is not None

    if not harness_captured and prior and not any("response" in rec for rec in prior):
        # Fail-open (journal-response mode only): an old-schema journal with
        # no response text at all — there is no human record to derive from
        # OR to contradict. With an utterance log the stronger source exists,
        # so this escape hatch never applies.
        return

    first_commits = [
        f
        for f in candidates
        if not any(isinstance(rec.get("resolved"), dict) and f in rec["resolved"] for rec in prior)
    ]
    if not first_commits:
        return

    if harness_texts is not None:
        # The lock: only text the HARNESS recorded counts as human. The
        # spec's ``response`` (and prior responses) are agent-relayed and
        # carry no authorship weight — exactly the laundering channel the
        # v1 gate could not close.
        human_texts = harness_texts
        response_commits = False
        source_desc = "logged human utterance for this repo (harness-captured)"
        remedy = "the human states it in a prompt (captured to the utterance log)"
    else:
        human_texts = [str(spec.response or "")]
        human_texts.extend(str(rec.get("response") or "") for rec in prior)
        response_commits = not _is_bare_ack(str(spec.response or ""))
        source_desc = "human response in this scope's journal"
        remedy = "the human restates it in their reply"

    human_num_strings, human_num_floats = _human_number_pool(human_texts)
    human_words: set[str] = set()
    for text in human_texts:
        human_words |= _ha_word_tokens(text)

    problems: list[str] = []
    for field in first_commits:
        value = resolved[field]
        if field not in _FREE_TEXT_CALLER_FIELDS:
            value_numbers: dict[str, float] = {}
            _collect_value_numbers(value, value_numbers)
            # Finding 25: the number check alone let a fabricated CATEGORICAL
            # param (a dataset name the human never uttered) pass whenever the
            # numbers derived. Hold the value's non-numeric claim tokens to the
            # same human-derivability bar — schema vocabulary (dict keys, the
            # ``kind`` discriminator value) is excluded by the collector.
            value_strings: set[str] = set()
            _collect_value_string_tokens(value, value_strings)
            if value_numbers or value_strings:
                missing = sorted(
                    norm
                    for norm, val in value_numbers.items()
                    if not _human_derivable(val, norm, human_num_strings, human_num_floats)
                )
                missing += sorted(value_strings - human_words)
                if missing:
                    problems.append(
                        f"{field} is human-authored: {spec.response!r} cannot "
                        "commit a value that appears only in the agent's proposal — "
                        f"ask the human for the sweep (or {remedy}); value "
                        f"token(s) {missing} derive from no {source_desc}"
                    )
                continue
            # No number OR string claims — fall through to the free-text rule below.
        if response_commits:
            continue  # journal-response mode: a substantive human reply commits it
        overlap_text = value if isinstance(value, str) else json.dumps(value, default=str)
        if _ha_word_tokens(overlap_text) & human_words:
            continue  # the human's own words state it (per the evidence source)
        problems.append(
            f"{field} is human-authored: {spec.response!r} cannot commit a "
            "value that appears only in the agent's proposal — ask the human to "
            f"state the {field} (or {remedy}); the value derives from no "
            f"{source_desc}"
        )

    if problems:
        _refuse_missing_authorship("human-authorship gate (conduct rule 9): " + "; ".join(problems))


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


# ── notebook sign-off authorship gate (D5 three locks + D-attention, T8) ──────

# The block-terminator convention for a notebook section SIGN-OFF. A sign-off
# ATTESTS that a human reviewed a section AT A SPECIFIC HASH; it is a HUMAN
# attestation over the ``notebook`` scope, journaled under this distinct block so
# the gate can recognise — and lock — it (mirrors the ``scope-unlock`` block
# convention). Lock 1 (no affordance) is organizational: there is NO sign-off
# verb, chain, or next_block — append-decision under this block is the ONLY write
# path (pinned by the contract test in tests/contracts/).
_SIGNOFF_BLOCK = "notebook-sign-off"

# Identifier-shaped tokens: the substrate for the raised human-required bar and
# the diff-token pool. Mirrors T5's assertion/diff vocabulary (plain identifiers).
_SIGNOFF_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _signoff_token_names(text: str) -> set[str]:
    """The identifier tokens in *text*, lowercased (the #26 token-exact idiom).

    Splits on non-identifier chars exactly like :func:`_prior_nudge_named`, so a
    sign-off response must NAME a thing, never merely contain it as a substring.
    """
    return set(re.split(r"[^a-z0-9_]+", (text or "").lower())) - {""}


def _names_slug(response: str, slug: str) -> bool:
    """True iff *response* names *slug* as a whole token (slug chars respected).

    A slug legitimately carries ``.`` / ``-`` (the ``_RUN_ID_RE`` class), which
    the identifier split would fragment — so the whole slug is matched with a
    boundary regex that treats the slug's own character class as word chars.
    """
    if not slug:
        return False
    pattern = re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(slug) + r"(?![A-Za-z0-9._-])")
    return bool(pattern.search(response or ""))


def _signoff_fresh_human_texts(
    experiment_dir: Path,
    *,
    actor_ids: list[str],
    audit_id: str,
    section: str,
    view_sha: str,
) -> list[str] | None:
    """The utterance-log evidence pool for ONE sign-off, TEMPORALLY BOUND.

    The actor-scoped log read (:func:`_actor_scoped_human_texts` semantics)
    plus the run-#12 finding-10 filter: a human can only attest a view that
    existed when they typed, so a candidate utterance must be logged at or
    after the signed view's render file was written (its mtime, floored to
    whole seconds — utterance ``ts`` is seconds-resolution). This kills the
    standing-sign-off class: a kickoff / resume prompt that happened to name
    the slug and a diff identifier minutes before the render existed is not
    attestation, and letting it pass is what kept the sign-off popup from
    ever firing (the gate passed instead of refusing).

    ``None`` — no log at all, or an unattributed >1-actor session — falls to
    the friction tier exactly like the unscoped read. An EMPTY list is
    different: the log exists but nothing fresh names this sign-off, so the
    gate refuses with the authorship marker (the popup's cue). An absent /
    unstatable render SKIPS the filter (returns the unfiltered pool): the
    missing-render refusal belongs to the UNMARKED trusted-display lock,
    where re-eliciting an utterance cannot fix it. A record with no
    parseable ``ts`` is excluded — conservative; the popup remedies.

    The temporal filter itself is the ONE shared :func:`_fresh_human_texts`
    helper (the B4 fix-wave generalized this finding-10 pattern); this function
    only computes the render-mtime *anchor* and delegates. An absent /
    unstatable render yields ``anchor=None`` → the unfiltered pool, exactly the
    original missing-render posture.
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    render = _notebook_view.render_path(
        experiment_dir, audit_id=audit_id, section=section, view_sha=view_sha
    )
    try:
        anchor: float | None = int(render.stat().st_mtime)
    except OSError:
        anchor = None
    return _fresh_human_texts(experiment_dir, actor_ids=actor_ids, anchor=anchor)


def _section_specific_tokens(section_view: Any) -> set[str]:
    """The identifier pool the raised human-required bar checks a sign-off against.

    Drawn from the section's DIFF-CHANGED lines (``+``/``-`` bodies, skipping the
    unified-diff ``+++``/``---`` file headers and ``@@`` hunk markers) AND — the
    full-view-recompute addition — from its LINT FLAGS (the identifier tokens in
    each finding's ``detail`` + ``evidence``), so a section made human-required
    SOLELY by a lint flag (an inherited section with no diff and no assertions, e.g.
    a data path under ``input_roots`` that vanished) still demands the human ENGAGE
    the flagged specific, not offer generic praise. Falls back to the section's
    declared ASSERTION identifiers when both the diff and the flags are empty (a
    human-required-but-inherited section whose assertions are ungreen has no diff
    tokens); when ALL are empty the bar reduces to the slug-naming floor already
    enforced (a token that does not exist cannot be demanded).
    """
    tokens: set[str] = set()
    for line in section_view.diff:
        if not line or line.startswith(("+++", "---", "@@")):
            continue
        if line[0] in "+-":
            tokens |= {m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(line[1:])}
    for flag in section_view.lint_flags:
        detail = str(flag.get("detail") or "") if isinstance(flag, dict) else ""
        evidence = flag.get("evidence") if isinstance(flag, dict) else None
        evidence_text = json.dumps(evidence, default=str) if evidence else ""
        tokens |= {
            m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(f"{detail} {evidence_text}")
        }
    if not tokens:
        for assertion in section_view.assertions:
            tokens |= {m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(assertion.test)}
    return tokens


def _read_interview_audited_source(
    experiment_dir: Path, audit_id: str | None
) -> dict[str, Any] | None:
    """The interview.json ``audited_source`` block matching *audit_id*, or ``None``.

    The canonical location is the campaign-dir root (where ``interview`` writes
    it); ``.hpc/interview.json`` is accepted defensively (the ``detect_entry_point``
    posture). A corrupt / non-object file is tolerated as "absent" here — the
    caller then refuses on an unresolvable SOURCE, which is the load-bearing loud
    failure; a duplicate refusal on the JSON shape would only muddy the message.
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = experiment_dir / rel
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        block = doc.get("audited_source")
        if isinstance(block, dict) and block.get("audit_id") == audit_id:
            return block
    return None


def _read_signoff_source_text(experiment_dir: Path, rel: str, *, required: bool) -> str | None:
    """Read a source/template ``.py`` at *rel* (relative to *experiment_dir*).

    A missing/unreadable REQUIRED source raises (a sign-off that cannot be
    recomputed is refused, never skipped); a missing template returns ``None`` so
    the caller conservatively treats a template-less audit as HUMAN-REQUIRED.
    """
    path = experiment_dir / rel
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        if required:
            raise errors.SpecInvalid(
                f"notebook sign-off gate: audited source {rel!r} is unreadable "
                f"({exc}). A sign-off RECOMPUTES the section hash from the .py on "
                "disk — an unresolvable source is refused, never skipped."
            ) from exc
        return None


def _resolve_signoff_audit_config(
    experiment_dir: Path, resolved: dict[str, Any]
) -> tuple[str, str, Any]:
    """Resolve ``(source_relpath, template_relpath, AuditConfig)`` for a sign-off.

    The full-view-recompute upgrade resolves the whole CANONICAL audit
    configuration, not just the source/template text:

    * **source / template relpaths** — an explicit ``resolved["source"]`` /
      ``resolved["template"]`` wins; otherwise the interview.json
      ``audited_source`` block (matched by ``audit_id``) supplies them. BOTH must
      resolve now: recomputing ``view_sha`` needs the template as much as the
      source (the diff-from-template is a view ingredient), and every sanctioned
      ``view_sha`` was produced against a real template (``notebook-audit-view``
      requires one), so a template that cannot be resolved means the signed view
      is not reproducible — refused loudly, never a conservative silent pass.
    * **lint roots + attention order** — the recorded audit configuration read
      from the same ``audited_source`` block (``read_recorded_config``); a block
      predating the config fields yields the conservative defaults (empty roots,
      source order), exactly the posture the gate used before the config was
      persisted.

    An unresolvable source or template is REFUSED loudly (this is the opted-in
    surface: recompute or refuse, never pass).
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    audit_id = resolved.get("audit_id")
    src_rel = resolved.get("source")
    tmpl_rel = resolved.get("template")
    if not src_rel or not tmpl_rel:
        block = _read_interview_audited_source(experiment_dir, audit_id)
        if block is not None:
            src_rel = src_rel or block.get("source")
            tmpl_rel = tmpl_rel or block.get("template")
    # ONE-SHOT refusal (run-#12 latency exhibit: agents discovered source and
    # template one bounce at a time, three appends per sign-off): name EVERY
    # unresolvable ingredient in a single refusal, with the complete resolved
    # skeleton the retry needs.
    unresolved = [
        name
        for name, value in (("source", src_rel), ("template", tmpl_rel))
        if not isinstance(value, str) or not value
    ]
    if unresolved:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: could not resolve {' + '.join(unresolved)} for "
            f"audit_id={audit_id!r} — not in resolved{{...}} and no matching "
            "interview.json audited_source block. The gate recomputes the section "
            "hash from the source and rebuilds the canonical view against the "
            "template, so both are required. Retry with the COMPLETE resolved "
            "skeleton: {audit_id, section, section_sha, view_sha, "
            "source: <audited .py relpath>, template: <template .py relpath>}."
        )
    assert isinstance(src_rel, str) and isinstance(tmpl_rel, str)  # narrowed above
    cfg = _notebook_view.read_recorded_config(experiment_dir, audit_id)
    return src_rel, tmpl_rel, cfg


def _assert_signoff_render_current(
    experiment_dir: Path,
    *,
    audit_id: str,
    section: str,
    view_sha: str,
    recomputed_section_sha: str,
) -> None:
    """The TRUSTED-DISPLAY lock: the render for what-the-human-saw must be CURRENT.

    The audit view an agent relays in chat is model-carried and unforceable; the
    trusted artifact is the CONTENT-ADDRESSED render file code wrote
    (``ops/notebook/render_store.py``, the v1.5 trusted-display lock, user-approved
    2026-07-07). This gate leg makes a sign-off unlandable unless that artifact
    exists on disk AND was produced against CURRENT source.

    Recorded boundary (the v1.5 drift log): the gate CANNOT recompute ``view_sha``
    — the view depends on lint findings the gate does not have (the ``view_sha``-is-
    provenance paragraph). So the check is a cross-reference, not a re-derivation:
    the render addressed by the RESOLVED ``view_sha`` must exist, parse, and its
    header must agree on ``view_sha`` + ``section``, and — the freshness leg — its
    header ``section_sha`` must equal the gate's FRESHLY RECOMPUTED section sha. An
    edit after the render moves the recomputed sha, so a stale render's header sha
    no longer matches and the sign-off is refused (the record's own asserted sha is
    already covered by the ``attestation.bind`` lock; this closes the case where the
    record sha was updated but the render step was never re-run).

    The check is reached through the top-level ``notebook_view`` facade — the direct
    ``hpc_agent.ops.notebook.render_store`` spelling trips the subject-import lint
    from inside the ``decision`` subject (the ``audit_view``/``field_ownership``
    precedent). Same trust model as every store: the render file is code-written, so
    tool-surface enforcement is the guarantee and filesystem forgery is out of scope
    (the honest-limit paragraph). Applied to redundant (auto-cleared) sign-offs too:
    the human claims to have reviewed, so the trusted artifact must exist.

    Raises :class:`errors.SpecInvalid` naming the missing/stale render path.
    """
    from hpc_agent.ops import notebook_view as _notebook_view

    path = _notebook_view.render_path(
        experiment_dir, audit_id=audit_id, section=section, view_sha=view_sha
    )
    header = _notebook_view.read_render_header(path)
    if header is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): no parseable render "
            f"artifact for what-the-human-saw at {path} — the audit view relayed in "
            "chat is model-carried and unforceable, so a sign-off requires the "
            "code-written, content-addressed render file. Re-run notebook-audit-view "
            "to produce it against the current source, then sign again."
        )
    if header.get("view_sha") != view_sha or header.get("section") != section:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): the render artifact at "
            f"{path} does not match the signed view (its header names "
            f"section={header.get('section')!r} / view_sha={header.get('view_sha')!r}, "
            f"the sign-off binds section={section!r} / view_sha={view_sha!r}). "
            "Re-run notebook-audit-view for this section and sign the fresh view."
        )
    if header.get("section_sha") != recomputed_section_sha:
        raise errors.SpecInvalid(
            "notebook sign-off gate (trusted-display lock): the render artifact at "
            f"{path} is STALE — its header section_sha ({header.get('section_sha')}) "
            f"does not match the current source ({recomputed_section_sha}). The source "
            "was edited after the render, so what-the-human-saw no longer reflects the "
            "code being signed. Re-run notebook-audit-view against the current source, "
            "then sign again."
        )


def _assert_signoff_reviewer_not_author(
    experiment_dir: Path, *, audit_id: str, section: str, section_sha: str
) -> None:
    """MH6 reviewer≠author gate — refuse a self-sign under >1 declared actors.

    Active ONLY when interview.json declares >1 actor (MH1); otherwise the gate
    does not exist and this returns silently, byte-identical to today (no draft
    lookup, no actor resolution, no new refusal). Under >1 actor, three refusals,
    all the loud/dangling-reference posture (NOT D7 silence, NOT the E2
    authorship-missing marker — a re-elicited utterance cannot fix a config /
    attribution gap; the remedy is a config or a recorded draft, not a sentence):

    * **No resolvable session actor** — an anonymous sign-off in a
      declared-multi-actor experiment is the laundering channel (sign as nobody,
      be everybody). Refused naming ``HPC_ACTOR``.
    * **No current draft attribution** — the author is the ``attestor_id`` of the
      newest ``notebook-draft`` attestation whose ``content_sha`` equals the
      FRESHLY RECOMPUTED *section_sha* (:func:`state.notebook_audit.read_draft_author`
      — routed through the ONE reducer, so a redrafted section's stale draft is no
      attribution). A missing attribution is REFUSED, not skipped: an unattributed
      section makes self-review undetectable by omission (draft, skip the draft
      record, self-sign). The refusal names the remedy (record the draft).
    * **signer == author** — the drafter's actor cannot sign their own section.
      Pure identity over opaque slugs (Q1-clean); core never knows WHY the lab
      wants this, it compares ids.
    """
    ids, _ = _read_interview_actors(experiment_dir)
    if len(ids) <= 1:
        return  # the gate does not exist under zero/one declared actor
    from hpc_agent.state.notebook_audit import read_draft_author

    signer = _session_actor(experiment_dir, ids)
    if signer is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): >1 actor is declared "
            f"but this session has no resolvable actor, so section {section!r} would "
            "be signed by nobody — an anonymous act in a declared-multi-actor "
            "experiment is the laundering channel. Configure HPC_ACTOR to a declared "
            "actor before signing."
        )
    author = read_draft_author(experiment_dir, audit_id, section, current_sha=section_sha)
    if author is None:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): section "
            f"{section!r} has NO current draft attribution at its recomputed sha "
            "(no `notebook-draft` record for this content), so an unattributed "
            "section could be self-reviewed by omission. Record the draft (the "
            "notebook-draft verb, part of the audit prelude) before signing."
        )
    if signer == author:
        raise errors.SpecInvalid(
            "notebook sign-off gate (MH6 reviewer≠author): the drafter's actor "
            f"({author!r}) cannot sign off their own section {section!r} — a sign-off "
            "by the drafting actor is self-review wearing a review's clothes. A "
            "DIFFERENT declared actor must review and sign."
        )


def _assert_signoff_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship + recompute gate for a NOTEBOOK section sign-off (T8).

    A sign-off is an ordinary ``append-decision`` whose ``scope_kind=="notebook"``,
    ``block=="notebook-sign-off"``, and ``resolved={audit_id, section, section_sha,
    view_sha}`` (D3). It ATTESTS that a human reviewed one section at a specific
    hash — a HUMAN attestation, so it faces both the un-fakeable recompute lock and
    the tiered authorship bar (``docs/design/notebook-audit.md`` D5 + D-attention).

    Block convention, enforced both directions (mirrors ``scope-unlock``): a
    ``notebook-sign-off`` block is refused for any ``scope_kind`` other than
    ``notebook``; every other record passes untouched.

    **Lock 1 (no affordance)** is organizational: there is no sign-off verb / chain
    / next_block — append-decision under this block is the only write path. Pinned
    by the contract test in ``tests/contracts/`` (no primitive is named sign-off).

    **Lock 2 (recompute, un-fakeable)** — the audited ``.py`` is resolved (from
    ``resolved['source']`` or the interview.json ``audited_source`` block), parsed
    (:func:`parse_percent_source`), the named section located, and its
    ``section_sha`` RECOMPUTED. The record binds through the ONE attestation kernel
    (``state.attestation.bind``, D5 lock 2 extracted once): the asserted
    ``section_sha`` must equal the recomputed one or the append is refused — a hash
    cannot be asserted into existence. An unresolvable source / missing section is
    REFUSED loudly, never skipped.

    **Lock 3 (authorship bar, D-attention tiered)** — bare acks are refused
    (:func:`_is_bare_ack`); the sign-off must NAME the section slug (token-exact,
    the #26 precedent). EVIDENCE IS TIERED like the unlock gate (run-#12
    finding 9, closing the run-#11 composed-response laundering hole): with a
    harness utterance log present the naming/engagement legs run over LOGGED
    HUMAN UTTERANCES — chat (capture hook) or the sign-off popup (the E4
    elicitation handler appends to the same log, which is what lets the MCP
    retry-once land) — and the agent-relayed ``response`` carries no authorship
    weight; absent a log the non-bare ``response`` is the friction tier
    (byte-identical v1). Log-tier candidates are TEMPORALLY BOUND (finding 10):
    only utterances logged after the signed view's render was written count —
    a prior prompt that happened to name the slug is not attestation
    (:func:`_signoff_fresh_human_texts`). The tier is RECOMPUTED here over the CANONICAL view
    (:func:`~hpc_agent.ops.notebook.canonical.build_canonical_view`) — with the
    REAL lint findings (recomputed server-side from the recorded roots), the
    journaled fresh receipts, and the recorded attention order. The v1 "statically
    recomputable legs only" boundary is RETIRED: a section made human-required
    *solely* by a lint flag IS now distinguished here (the lint is cheap local
    static analysis; the receipts are journaled; the roots are persisted on the
    ``audited_source`` block). For a **HUMAN_REQUIRED** section the bar RAISES: the
    response must additionally ENGAGE the change — contain at least one identifier
    drawn from the section's diff-changed lines (:func:`_section_specific_tokens`).
    This is the boundary-drift defense: soften the human-required tier only via a
    richer harness-captured utterance, never a bare ack.

    **AUTO_CLEARED + a human sign-off: ACCEPT, but mark ``resolved['redundant'] =
    True``.** The alternative (refuse) was rejected: refusing a human's VOLUNTARY
    review would delete information and create a verb-shaped affordance gap
    (a human who looked would have no way to record it). Marking keeps the
    attention ledger honest — the record shows a real human sign-off that the
    tiering deemed unnecessary. The recompute lock and the base authorship floor
    (non-bare, slug-named) still apply to a redundant sign-off; only the raised
    diff-token bar is waived (an auto-cleared section has no change to engage).

    **TEMPLATE required (full-view recompute).** The canonical view is a
    diff-from-template projection, so a template that cannot be resolved means the
    signed ``view_sha`` is not reproducible — REFUSED loudly
    (:func:`_resolve_signoff_audit_config`), never a conservative empty-template
    pass. Every sanctioned ``view_sha`` was produced against a real template
    (``notebook-audit-view`` requires one), so an absent template at append time is
    a broken setup.

    **view_sha is RECOMPUTED (the defect this gate fixes).** The full-view
    recompute rebuilds the canonical section view and REFUSES unless the section's
    recomputed ``view_sha`` equals the resolved one. Because the section body is
    already confirmed current (the ``section_sha`` bind) and the render is confirmed
    current, a ``view_sha``-only mismatch pinpoints a moved VIEW ingredient — a
    changed lint finding (e.g. a vanished data path under the recorded
    ``input_roots``), a changed journaled receipt, or a changed attention order —
    and the refusal says so.

    **Trusted-display lock (v1.5)** — the audit view an agent relays in chat is
    model-carried and unforceable, so a sign-off additionally requires the
    CONTENT-ADDRESSED render file code wrote (:func:`_assert_signoff_render_current`,
    over ``ops/notebook/render_store.py``): the render addressed by the resolved
    ``view_sha`` must exist, parse, agree on ``view_sha``/``section``, and carry a
    header ``section_sha`` equal to the FRESHLY RECOMPUTED ``sect.section_sha`` — so
    an edit-after-render (render stale vs the recomputed sha) is refused even though
    the record's own asserted sha is already covered by the bind lock. Because the
    gate can't recompute ``view_sha`` (the recorded boundary), the render's header is
    the cross-reference; applied BEFORE the tier branch so redundant/auto-cleared
    sign-offs need the artifact too.

    Raises :class:`errors.SpecInvalid` on any refusal.
    """
    is_signoff_block = spec.block == _SIGNOFF_BLOCK

    # Block convention: notebook-sign-off is a notebook-only action.
    if is_signoff_block and spec.scope_kind != "notebook":
        raise errors.SpecInvalid(
            f"block {_SIGNOFF_BLOCK!r} is only valid for scope_kind='notebook' "
            f"(a notebook section sign-off); got scope_kind={spec.scope_kind!r}."
        )
    if not (is_signoff_block and spec.scope_kind == "notebook"):
        return  # not a notebook sign-off — nothing to gate

    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "notebook sign-off gate: resolved must carry "
            "{audit_id, section, section_sha, view_sha}."
        )

    audit_id = resolved.get("audit_id")
    section = resolved.get("section")
    section_sha = resolved.get("section_sha")
    view_sha = resolved.get("view_sha")
    missing = [
        name
        for name, value in (
            ("audit_id", audit_id),
            ("section", section),
            ("section_sha", section_sha),
            ("view_sha", view_sha),  # binds what-the-human-saw (D5); required.
        )
        if not isinstance(value, str) or not value
    ]
    if missing:
        raise errors.SpecInvalid(
            "notebook sign-off gate: resolved must carry non-empty "
            f"{{audit_id, section, section_sha, view_sha}}; missing/empty: {missing}. "
            "view_sha binds what-the-human-saw into the record (D5) and is required."
        )
    assert isinstance(section, str) and isinstance(section_sha, str)
    assert isinstance(audit_id, str) and isinstance(view_sha, str)

    # Base authorship floor — TIERED exactly like the unlock gate (the shared
    # lock tier, extended to T8): with a harness utterance log present the legs
    # run over LOGGED HUMAN UTTERANCES — text a human verifiably typed, in chat
    # (the UserPromptSubmit capture hook) or in the sign-off POPUP (the E4
    # elicitation handler appends to the SAME log, which is what lets the
    # retry-once land on the human's words) — and the agent-relayed ``response``
    # carries no authorship weight (the run-#11 laundering finding: a composed
    # response passes the token checks mechanically but attests nothing).
    # Absent a log (older harness / no capture hook), the non-bare ``response``
    # is the human's typed sign-off — the v1 friction tier, byte-identical.
    # MH4: >1 declared actors scope the read to the session actor's log only;
    # an unattributed session falls to the friction tier, never the union.
    response = str(spec.response or "")
    _signoff_actor_ids, _signoff_policy = _read_interview_actors(experiment_dir)
    _signoff_harness_texts = _signoff_fresh_human_texts(
        experiment_dir,
        actor_ids=_signoff_actor_ids,
        audit_id=audit_id,
        section=section,
        view_sha=view_sha,
    )
    if _signoff_harness_texts is None:
        if _is_bare_ack(response):
            _refuse_missing_authorship(
                "notebook sign-off gate: signing off a section is a HUMAN act — a bare "
                f"{spec.response!r} (a 'y' / click) cannot sign off. Name the section "
                f"({section!r}) and state what you reviewed."
            )
        if not _names_slug(response, section):
            _refuse_missing_authorship(
                "notebook sign-off gate: the sign-off response must NAME the section "
                f"slug {section!r} (token-exact, the #26 precedent) — a generic ack "
                "cannot attest a specific section. Restate, naming the section."
            )
        signoff_candidates = [response]
    else:
        signoff_candidates = [
            text
            for text in _signoff_harness_texts
            if not _is_bare_ack(text) and _names_slug(text, section)
        ]
        if not signoff_candidates:
            _refuse_missing_authorship(
                "notebook sign-off gate: signing off a section is a HUMAN act — no "
                f"logged human utterance NAMES the section slug {section!r} "
                "(token-exact, the #26 precedent). The human types the sign-off in "
                "their own words (in chat, or in the sign-off popup when it opens); "
                "an agent-relayed response carries no authorship weight here."
            )

    # Lazy, subject-lint-safe imports (state.* is allowed substrate; the ops
    # notebook subject is reached through the top-level ``notebook_view`` facade).
    from hpc_agent.ops import notebook_view as _notebook_view
    from hpc_agent.state import attestation
    from hpc_agent.state.audit_source import parse_percent_source

    # Resolve the CANONICAL audit configuration: source/template relpaths + the
    # recorded lint roots + attention order (the ingredients of the view_sha).
    source_relpath, template_relpath, cfg = _resolve_signoff_audit_config(experiment_dir, resolved)

    # Lock 2 — recompute the section hash from the .py on disk and bind through
    # the ONE attestation kernel (D5 lock 2). Refuses an unresolvable source.
    source_text = _read_signoff_source_text(experiment_dir, source_relpath, required=True)
    assert source_text is not None  # required=True raises rather than returning None
    parsed = parse_percent_source(source_text)
    sect = next((s for s in parsed.sections if s.slug == section), None)
    if sect is None:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: section {section!r} not found in the audited "
            f"source (audit_id={audit_id!r}). A sign-off must name a CURRENT section "
            "— re-view the source and sign an existing section."
        )
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": _notebook_view.SUBJECT_KIND,
            "subject_id": f"{audit_id}:{section}",
            "content_sha": section_sha,
            "view_sha": view_sha,
        },
        recompute=sect.section_sha,
    )

    # Trusted-display lock (v1.5) — the CONTENT-ADDRESSED render for
    # what-the-human-saw must exist and be CURRENT. Reuses the freshly-recomputed
    # ``sect.section_sha`` (never a second recompute). Applied BEFORE the tier
    # branch so it covers redundant/auto-cleared sign-offs too (the human claims a
    # review; the trusted artifact must exist).
    _assert_signoff_render_current(
        experiment_dir,
        audit_id=audit_id,
        section=section,
        view_sha=view_sha,
        recomputed_section_sha=sect.section_sha,
    )

    # FULL-VIEW RECOMPUTE (the "statically-recomputable legs only" boundary is
    # RETIRED). Build the CANONICAL view SERVER-SIDE — real lint findings from the
    # recorded roots, journaled fresh receipts, recorded attention order (one
    # definition: ``build_canonical_view``, shared with the verbs + the plugin) —
    # and REFUSE unless the section's recomputed view_sha equals the resolved one.
    # The section body is already confirmed current (the bind lock) and the render
    # is confirmed current, so a view_sha-ONLY mismatch means a VIEW ingredient
    # moved: a lint finding changed (a data path under input_roots vanished /
    # appeared), a journaled receipt changed, or the attention order changed.
    view = _notebook_view.build_canonical_view(
        experiment_dir,
        audit_id=audit_id,
        source_relpath=source_relpath,
        template_relpath=template_relpath,
        cfg=cfg,
    )
    section_view = next((v for v in view.sections if v.slug == section), None)
    if section_view is None:
        raise errors.SpecInvalid(
            f"notebook sign-off gate: section {section!r} is not in the recomputed "
            f"canonical view (audit_id={audit_id!r}). Re-run notebook-audit-view and "
            "sign a current section."
        )
    if section_view.view_sha != view_sha:
        raise errors.SpecInvalid(
            "notebook sign-off gate (full-view recompute): the section body is "
            "unchanged (the section_sha bind passed) but the recomputed canonical "
            f"view_sha ({section_view.view_sha}) does not equal the signed view_sha "
            f"({view_sha}). An ingredient of the VIEW moved since it was rendered — a "
            "lint finding changed (e.g. a data path under the recorded input_roots "
            "vanished or appeared), a journaled render receipt changed, or the "
            "attention order changed. Re-run notebook-audit-view for section "
            f"{section!r}, re-inspect the fresh view, and sign THAT view_sha."
        )
    # MH6 (reviewer ≠ author): under >1 declared actors a sign-off may not be
    # authored by the SECTION'S DRAFTER — self-review wearing a review's clothes.
    # Applied BEFORE the tier branch so it covers the redundant (auto-cleared)
    # path too: a redundant self-review is still recorded self-review. Silent under
    # zero/one declared actor (the gate does not exist there — byte-identical).
    _assert_signoff_reviewer_not_author(
        experiment_dir, audit_id=audit_id, section=section, section_sha=sect.section_sha
    )

    # The tier is now REAL — recomputed with the REAL lint flags (the recorded
    # conservative-floor gap is closed). A section made human-required SOLELY by a
    # lint flag is now distinguished here.
    tier = section_view.tier

    if tier == _notebook_view.AUTO_CLEARED:
        # ACCEPT a voluntary human sign-off of an auto-cleared section, but mark it
        # redundant (decision recorded in the docstring). Mutating ``resolved`` in
        # place is visible to the append that follows (same dict object).
        resolved["redundant"] = True
        return

    # HUMAN_REQUIRED — raise the bar: the response must engage a section specific.
    # The slug's OWN tokens are subtracted from both sides: naming the section
    # (already required) must not double as "engaging the change", or a slug like
    # ``model-fit`` whose fragments appear in the diff line would satisfy the bar
    # by itself and the raise would be a no-op.
    slug_tokens = _signoff_token_names(section)
    raw_specifics = _section_specific_tokens(section_view) if section_view is not None else set()
    specifics = raw_specifics - slug_tokens
    # The engaging text must be one that ALSO names the slug (the tiered
    # candidates): a slug-naming utterance and a separate token-dropping one
    # cannot combine into an attestation neither made alone.
    engaged = any(
        (_signoff_token_names(text) - slug_tokens) & specifics for text in signoff_candidates
    )
    if specifics and not engaged:
        _refuse_missing_authorship(
            f"notebook sign-off gate: section {section!r} is HUMAN-REQUIRED "
            "(nonempty diff-from-template / lint flags / ungreen assertions), so the "
            "sign-off must ENGAGE the change — name at least one identifier from the "
            "section's diff, not offer a generic ack (soften only via a richer "
            "utterance, never a bare ack; the boundary-drift flag). Identifiers in "
            f"the change include: {sorted(specifics)[:8]}."
        )


# ── registration authorship gate (R6 three locks + the revoke floor, T7) ──────

# The seven ``resolved`` keys a registration record must carry as non-empty
# values (R6 lock 2). ``view_sha`` is required too (checked separately — it is
# the fourth recompute leg, R6). A registration is the maximal human ceremony:
# every leg is recomputed server-side and no waiver / auto-clear / redundant tier
# exists at this gate (the attestor is ALWAYS human, R6 lock 3).
_REGISTRATION_REQUIRED_KEYS: tuple[str, ...] = (
    "registration_id",
    "run_id",
    "dossier_sha",
    "template",
    "template_sha",
    "fields",
    "prerequisites",
)

# Identifier-shaped hex runs of length >= 8 — the sha-prefix pool for R6 lock 3.
# An 8-hex prefix exists NOWHERE in a human's prior vocabulary and can only derive
# from the presented evidence (the rendered verify-registration brief), so it is
# the diff-token pattern elevated to its strongest form.
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{8,}")


def _field_present(value: Any) -> bool:
    """True when a template field value counts as PRESENT — non-None, non-empty.

    Mirrors ``ops/registration/verify_op.py::_nonempty`` (completeness is COUNTING
    over opaque values, R5 — a value is never read for meaning, only for presence).
    """
    if value is None:
        return False
    return not (isinstance(value, (str, list, tuple, dict, set)) and len(value) == 0)


def _registration_authored_text(experiment_dir: Path, response: str) -> str:
    """The human-authored text the R6 lock-3 tokens must derive from.

    Tiered exactly like the notebook / unlock gates: with the harness utterance
    log present (:func:`_harness_human_texts` — the shared lock tier) the tokens
    (the ``registration_id`` and the prerequisite sha-prefix) must derive from a
    logged human utterance, text the harness recorded out-of-band. Absent the log,
    the agent-relayed ``response`` is the human's typed sign-off (the friction
    tier, honestly weaker). There is NO auto-clear / redundant tier here — the
    registration attestor is ALWAYS human (R6).

    Actor-scoped under >1 declared actors (MH4): the pool is the SESSION ACTOR'S
    log only (:func:`_actor_scoped_human_texts`); an unattributed >1-actor session
    falls to the friction tier (the ``response``), never the anonymous union.
    """
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    harness_texts = _actor_scoped_human_texts(experiment_dir, _actor_ids)
    if harness_texts is not None:
        return "\n".join(harness_texts)
    return response


def _names_sha_prefix(text: str, chain_entries: list[Any]) -> str | None:
    """The first chain entry ``content_sha`` a hex run in *text* prefixes, or None.

    R6 lock 3: the sign-off must NAME at least one prerequisite by an 8+ hex-char
    prefix of one chain entry's ``content_sha``, matched against the gate-verified
    chain. Case-insensitive prefix match.
    """
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        for entry in chain_entries:
            if str(entry.content_sha).lower().startswith(run):
                return str(entry.content_sha)
    return None


def _assert_revoke_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The R7 revoke floor for a ``registration-revoke`` record.

    A human overturn: non-bare, NAMES the ``registration_id``, and its free-text
    ``reason`` is MANDATORY ("validate or overturn WITH reason", the consumer-seat
    prior). It binds no new sha (it withdraws), so there is NO recompute leg — but
    it is journaled, attributed, and permanent like everything else. The bare-ack
    and id-naming refusals carry the E2 authorship marker (a freshly typed human
    rationale resolves them); the missing-reason refusal is structural (the agent
    must add ``resolved['reason']``), left UNMARKED.
    """
    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "registration-revoke gate: resolved must name a non-empty registration_id "
            "(the id being overturned)."
        )
    reason = resolved.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise errors.SpecInvalid(
            "registration-revoke gate: a revoke MUST carry a free-text resolved['reason'] "
            "(validate or overturn WITH reason — the consumer-seat prior, R7). It binds no new "
            "sha, but it is journaled, attributed, and permanent."
        )
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration-revoke gate: overturning a registration is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot revoke it. State why you are revoking "
            f"the registration ({registration_id!r})."
        )
    # B4 ts>=anchor: the naming leg reads only utterances logged at or after the
    # target registration's ts — the human named the id at CREATION, so an
    # unbounded read is permanently satisfied and an agent-composed revoke rides
    # through. Anchor = the registration filing record's ts (None → unfiltered,
    # the existence checks above / below own the never-registered case).
    anchor = _target_record_ts(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        filing_block=REGISTRATION_BLOCK,
        id_field="registration_id",
        target_id=registration_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration-revoke gate: the revoke must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor), in an utterance logged "
            "AT OR AFTER the registration it overturns (B4 ts>=anchor). Restate, naming "
            "the registration being overturned."
        )


def _assert_conformance_baseline_membership(resolved: dict[str, Any], sig: Any) -> None:
    """Refuse a ``conformance`` declaration whose baseline is NOT in the sealed dossier.

    The live-conformance C-declare append leg (moved here from registration T6 by
    pre-implementation verification — the state substrate imports no ``ops`` and
    ``compute_dossier_signature`` is an ``ops`` seam). When the registration's
    ``resolved`` carries an optional ``conformance`` block, it is validated
    STRUCTURE-only through the ONE declaration validator
    (:func:`~hpc_agent.state.registration.parse_conformance_declaration` →
    ``state/conformance.py::validate_declaration``; unknown keys refused there),
    and its declared baseline ``{path, sha256}`` must be a MEMBER of *sig*'s
    dry-gathered manifest entries (identity against the ``{path, sha256}`` pairs
    the dossier seals). So the control limits derive from evidence INSIDE the
    sealed dossier by construction — never from a file the caller can swap after
    sign-off. An absent block is a no-op (conformance is opt-in, byte-identical).

    Structural refusal (UNMARKED): a non-member baseline is a moved/absent
    artifact a re-elicited utterance cannot fix.
    """
    from hpc_agent.state.registration import parse_conformance_declaration

    declaration = parse_conformance_declaration(resolved)
    if declaration is None:
        return  # conformance is opt-in — no block, no machinery (byte-identical)
    path = declaration.baseline.path
    sha256 = declaration.baseline.sha256
    # Membership is by the sealed-bytes SHA — the integrity identity. The declared
    # ``path`` is the caller's EXPERIMENT-RELATIVE locator the read-side (T5/T8)
    # resolves; the dossier's manifest entries carry ARCHIVE paths (the ``_aggregated``
    # → ``aggregated`` rename means the two path spellings differ by construction), so
    # the sha is the ONE stable identity across both. A declared sha that is a sealed
    # entry's sha proves the control limits derive from evidence INSIDE the sealed
    # dossier — never a file swapped after sign-off (C-declare; recorded in the drift log).
    if any(entry.get("sha256") == sha256 for entry in sig.entries):
        return
    raise errors.SpecInvalid(
        "registration gate (lock 2, conformance): the declared conformance baseline "
        f"{{path={path!r}, sha256={sha256[:12]}...}} is NOT sealed in the dossier — no manifest "
        "entry carries that sha256. The control limits must derive from evidence INSIDE the "
        "sealed dossier by construction (C-declare), never a file swapped after sign-off. "
        "Declare a baseline artifact the dossier seals."
    )


def _assert_registration_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """R6's three locks for a ``registration`` record — every bar at its ceiling.

    Lock 1 (no affordance) is organizational: append-decision under this block is
    the ONLY write path (there is no registration verb / chain / next_block /
    skill; pinned by the contract test). Lock 2 recomputes ALL FOUR legs
    server-side and binds through :func:`state.attestation.bind`:

    * (a) ``dossier_sha`` vs a dry ``compute_dossier_signature`` re-gather from the
      LIVE stores (R2 — you may not register what has drifted since it was
      validated); bound through the ONE attestation kernel.
    * (b) ``template_sha`` vs the template file's raw bytes on disk (R5), plus
      template completeness by COUNTING (every declared field slug non-empty in
      ``resolved['fields']``; every declared prerequisite slot present in the
      chain).
    * (c) every chain entry's ``content_sha`` via ``check_chain`` — ALL slots must
      verdict CURRENT (partial registration REFUSED, naming every failing slot).
    * (d) ``view_sha`` RECOMPUTED via the deterministic verify-registration brief
      projection (:func:`build_view` over the append-time legs — a witness you can
      regenerate is regenerated).

    Lock 3 (authorship, the raised bar): bare acks refused; the response must NAME
    the ``registration_id`` token-exact AND name at least one prerequisite by an 8+
    hex prefix of one chain entry's ``content_sha`` (matched against the verified
    chain). Tiered on the harness utterance log (:func:`_registration_authored_text`).
    NO auto-clear tier, NO redundant-mark path — the attestor is ALWAYS human.

    The authorship-bar refusals (bare ack, missing id, missing sha-prefix) carry
    the E2 marker via :func:`_refuse_missing_authorship`; the Lock-2 sha /
    structural refusals raise plain :class:`errors.SpecInvalid` UNMARKED (a re-elicit
    cannot fix a moved hash — the E2 scoping).
    """
    from hpc_agent._wire.actions.verify_registration import (
        DossierLeg,
        FieldsBlock,
        LegStatus,
        PrerequisiteKind,
        PrerequisiteLeg,
        TemplateLeg,
    )
    from hpc_agent.ops import export_dossier, registration_view
    from hpc_agent.state import attestation
    from hpc_agent.state.registration import CURRENT as REG_CURRENT
    from hpc_agent.state.registration import parse_chain_entry, parse_template

    # ── Lock 2 shape: the seven required non-empty keys + view_sha ──
    missing = [k for k in _REGISTRATION_REQUIRED_KEYS if not resolved.get(k)]
    if missing:
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved must carry non-empty "
            f"{list(_REGISTRATION_REQUIRED_KEYS)}; missing/empty: {missing}."
        )
    registration_id = resolved["registration_id"]
    run_id = resolved["run_id"]
    dossier_sha = resolved["dossier_sha"]
    template_rel = resolved["template"]
    template_sha = resolved["template_sha"]
    fields = resolved["fields"]
    prerequisites = resolved["prerequisites"]
    view_sha = resolved.get("view_sha")
    if not isinstance(view_sha, str) or not view_sha:
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved must carry a non-empty view_sha — the "
            "code-rendered verify-registration brief the human saw (R6's fourth recompute leg)."
        )
    if not (
        isinstance(registration_id, str)
        and isinstance(run_id, str)
        and isinstance(dossier_sha, str)
        and isinstance(template_rel, str)
        and isinstance(template_sha, str)
    ):
        raise errors.SpecInvalid(
            "registration gate (lock 2): registration_id / run_id / dossier_sha / template / "
            "template_sha must all be strings."
        )
    if not isinstance(fields, dict):
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved['fields'] must be a mapping."
        )
    if not isinstance(prerequisites, list):
        raise errors.SpecInvalid(
            "registration gate (lock 2): resolved['prerequisites'] must be a list (the chain)."
        )

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration gate: registering a strategy is the maximal HUMAN ceremony — a bare "
            f"{spec.response!r} (a 'y' / click) cannot register. Name the registration "
            f"({registration_id!r}) and a prerequisite sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration gate: the sign-off must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── Lock 2a: dossier — dry re-gather + bind through the ONE kernel ──
    include_lineage = bool(resolved.get("include_lineage", False))
    sig = export_dossier.compute_dossier_signature(
        experiment_dir, run_id, include_lineage=include_lineage
    )
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": SUBJECT_KIND,
            "subject_id": registration_id,
            "content_sha": dossier_sha,
            "view_sha": view_sha,
        },
        recompute=sig.bundle_sha256,
    )

    # ── Lock 2a': conformance baseline membership (live-conformance C-declare) ──
    # When the registration opts into live conformance, the declared baseline
    # {path, sha256} must be a MEMBER of the dossier's dry-gathered manifest
    # entries — the control limits derive from evidence INSIDE the sealed dossier
    # by construction, never from a file swapped after sign-off.
    _assert_conformance_baseline_membership(resolved, sig)

    # ── Lock 2b: template raw-bytes sha on disk + completeness by counting ──
    try:
        tmpl_bytes = (Path(experiment_dir) / template_rel).read_bytes()
    except OSError as exc:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template file {template_rel!r} is unreadable ({exc}). "
            "A registration recomputes the template raw-bytes sha from disk; an unresolvable "
            "template is refused, never skipped."
        ) from exc
    recomputed_template_sha = hashlib.sha256(tmpl_bytes).hexdigest()
    if recomputed_template_sha != template_sha:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template sha mismatch — recorded {template_sha!r} vs "
            f"the on-disk {recomputed_template_sha!r}. A hash cannot be asserted into existence."
        )
    try:
        template = parse_template(
            json.loads(tmpl_bytes.decode("utf-8")), template_sha=recomputed_template_sha
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): template {template_rel!r} is not valid UTF-8 JSON "
            f"({exc})."
        ) from exc
    missing_fields = [s for s in template.fields if not _field_present(fields.get(s))]
    if missing_fields:
        raise errors.SpecInvalid(
            "registration gate (lock 2): template fields incomplete — every declared field slug "
            f"must carry a non-empty value in resolved['fields']; missing: {missing_fields}."
        )

    # ── Lock 2c: the prerequisite chain — every declared slot filled + all CURRENT ──
    entries = [parse_chain_entry(e) for e in prerequisites]
    declared_slots = {p.slot for p in template.prerequisites}
    chain_slots = {e.slot for e in entries}
    missing_slots = sorted(declared_slots - chain_slots)
    if missing_slots:
        raise errors.SpecInvalid(
            f"registration gate (lock 2): declared prerequisite slot(s) {missing_slots} are not "
            "present in the chain — every declared prerequisite must be filled (counting)."
        )
    verdicts = registration_view.check_chain(
        experiment_dir, entries, dossier_run_ids=set(sig.run_ids)
    )
    failing = [(v.slot, v.status) for v in verdicts if v.status != REG_CURRENT]
    if failing:
        names = ", ".join(f"{slot}={status}" for slot, status in failing)
        raise errors.SpecInvalid(
            "registration gate (lock 2): partial registration REFUSED — prerequisite slot(s) not "
            f"CURRENT: {names}. Every prerequisite must read current at append (R4); the remedy "
            "for partial readiness is not registering."
        )

    # ── Lock 2d: view_sha recomputed via the deterministic brief projection ──
    dossier_leg = DossierLeg(
        recorded_sha=dossier_sha, recomputed_sha=sig.bundle_sha256, drifted_stores=[]
    )
    template_leg = TemplateLeg(
        status="current", recorded_sha=template_sha, recomputed_sha=recomputed_template_sha
    )
    prereq_legs = [
        PrerequisiteLeg(
            slot=v.slot,
            kind=cast("PrerequisiteKind", v.kind),
            status=cast("LegStatus", v.status),
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
        for v in verdicts
    ]
    declared = list(template.fields)
    fields_report = FieldsBlock(
        declared=declared,
        present=[s for s in declared if _field_present(fields.get(s))],
        missing=[],
    )
    _, recomputed_view_sha = registration_view.build_view(
        status=REG_CURRENT,
        registration_id=registration_id,
        registered_at=None,
        dossier=dossier_leg,
        template=template_leg,
        prerequisites=prereq_legs,
        fields=fields_report,
    )
    if recomputed_view_sha != view_sha:
        raise errors.SpecInvalid(
            "registration gate (lock 2, fourth leg): the recomputed verify-registration view_sha "
            f"({recomputed_view_sha}) does not equal the signed view_sha ({view_sha}). The brief "
            "the human bound must be the deterministic projection over the CURRENT legs — re-run "
            "verify-registration and sign THAT view_sha."
        )

    # ── Lock 3, part 2: the raised bar — name a prerequisite by an 8+ hex sha prefix ──
    if _names_sha_prefix(authored, entries) is None:
        _refuse_missing_authorship(
            "registration gate (lock 3): the sign-off must NAME at least one prerequisite by an "
            "8+ hex-character prefix of one chain entry's content_sha (the diff-token pattern at "
            "its strongest — an 8-hex prefix exists nowhere in a human's prior vocabulary and can "
            "only derive from the presented evidence). Quote a prerequisite sha prefix from the "
            "verify-registration brief."
        )


def _assert_registration_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The R6 registration sign-off gate — the deployment-boundary attestation.

    Block convention, enforced BOTH directions (mirrors ``scope-unlock`` /
    ``notebook-sign-off``): a registration-family block
    (:data:`REGISTRATION_BLOCK_FAMILY`) is refused for any ``scope_kind`` other
    than ``"registration"``; and the ``"registration"`` scope accepts ONLY the
    block family (``registration`` / ``registration-revoke`` /
    ``registration-review`` / ``conformance-verdict``). Every other record passes
    untouched. Dispatches a ``registration-revoke`` to the revoke floor (R7), a
    ``registration-review`` to the C-horizon re-affirmation floor, a
    ``conformance-verdict`` to the C-verdict drift-verdict gate, and a
    ``registration`` to the full three locks (R6).

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals
    carry the E2 marker so the single append firing site covers registration
    sign-offs over MCP too; sha / structural refusals stay unmarked).
    """
    block = spec.block
    in_family = block in REGISTRATION_BLOCK_FAMILY
    # A registration-family block is registration-scope-only.
    if in_family and spec.scope_kind != "registration":
        raise errors.SpecInvalid(
            f"block {block!r} is a registration-family block, only valid for "
            f"scope_kind='registration'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "registration":
        return  # not a registration record — nothing to gate
    # The registration scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='registration' accepts only its block family "
            f"{sorted(REGISTRATION_BLOCK_FAMILY)}; got block={block!r} — a registration scope "
            "records ONLY registration / registration-revoke (R6)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "registration gate: resolved must be a mapping carrying the registration fields."
        )
    if block == REVOKE_BLOCK:
        _assert_revoke_floor(experiment_dir, spec, resolved)
        return
    if block == REGISTRATION_REVIEW_BLOCK:
        _assert_registration_review_floor(experiment_dir, spec, resolved)
        return
    if block == CONFORMANCE_VERDICT_BLOCK:
        _assert_conformance_verdict_authorship(experiment_dir, spec, resolved)
        return
    # block == REGISTRATION_BLOCK — the maximal human ceremony (R6 three locks).
    _assert_registration_full(experiment_dir, spec, resolved)


# ── registration-review floor + conformance-verdict gate (live-conformance T7) ─


def _names_any_sha_prefix(text: str, shas: Sequence[str]) -> bool:
    """True iff a hex run in *text* is an 8+ hex prefix of ANY sha in *shas*.

    The R6 sha-prefix bar (:data:`_HEX_RUN_RE`) applied to a bare list of shas
    (the conformance-verdict ``cites`` are raw ``content_sha`` strings, not
    ``.sha``-bearing citation objects like :func:`_names_citation_sha_prefix`).
    Case-insensitive prefix match — a token that can only derive from the
    presented evidence brief.
    """
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        for sha in shas:
            if str(sha).lower().startswith(run):
                return True
    return False


def _valid_review_horizon(review_horizon: str) -> None:
    """Refuse a ``registration-review`` horizon that is not ISO-8601 (T7 append check).

    C-horizon: core names no period and computes no cadence — it only compares a
    caller-computed timestamp. The reduction's :func:`state.registration._horizon_lapsed`
    is deliberately tolerant (an unparseable horizon yields "not lapsed"), so the
    *append* gate is where a malformed horizon is caught loudly (its docstring
    names this gate). A trailing ``Z`` normalizes to ``+00:00`` before
    :func:`datetime.fromisoformat`.
    """
    from datetime import datetime

    raw = review_horizon[:-1] + "+00:00" if review_horizon.endswith("Z") else review_horizon
    try:
        datetime.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise errors.SpecInvalid(
            "registration-review gate: resolved['review_horizon'] must be an ISO-8601 "
            f"timestamp (the caller computes the date; core compares timestamps); got "
            f"{review_horizon!r} ({exc})."
        ) from exc


def _assert_registration_review_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-horizon re-affirmation floor for a ``registration-review`` record.

    A review EXTENDS a current registration's ``review_horizon`` WITHOUT
    re-registration, when nothing has drifted — the cheaper tier that answers
    "does a human still stand behind this, today?" (recorded rationale: forcing a
    full re-registration for an unchanged dossier would train horizon inflation).

    ``resolved = {registration_id, dossier_sha, review_horizon}`` — all three
    non-empty; ``review_horizon`` a valid ISO timestamp (:func:`_valid_review_horizon`).

    The authorship floor (the R6 form, tiered on the harness utterance log via
    :func:`_registration_authored_text`): a bare ack cannot re-affirm; the response
    must NAME the ``registration_id`` token-exact AND the dossier sha by an 8+ hex
    prefix (:func:`_names_target_sha_prefix`).

    **The recompute leg — you cannot re-affirm a DRIFTED registration (C-horizon).**
    The gate reduces the id's registration journal to the WINNER, RECOMPUTES the
    live dossier signature via the ONE seam
    (:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`), and refuses
    when it no longer equals the winner's recorded ``dossier_sha`` — the remedy for
    a moved dossier is re-registration, not review. The review's own asserted
    ``dossier_sha`` must name that SAME sealed dossier.

    Authorship-bar refusals carry the E2 marker (a freshly typed re-affirmation
    resolves them); the shape / drift / missing-winner refusals raise plain
    :class:`errors.SpecInvalid` UNMARKED (a re-elicit cannot un-drift a dossier).
    """
    from hpc_agent.ops import export_dossier
    from hpc_agent.state.registration import ABSENT as REG_ABSENT
    from hpc_agent.state.registration import REVOKED as REG_REVOKED
    from hpc_agent.state.registration import reduce_registration

    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must name a non-empty registration_id "
            "(the registration being re-affirmed)."
        )
    dossier_sha = resolved.get("dossier_sha")
    if not isinstance(dossier_sha, str) or not dossier_sha:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must carry a non-empty dossier_sha (the sealed "
            "dossier being re-affirmed — the review recomputes the live signature against it)."
        )
    review_horizon = resolved.get("review_horizon")
    if not isinstance(review_horizon, str) or not review_horizon:
        raise errors.SpecInvalid(
            "registration-review gate: resolved must carry a non-empty review_horizon (the new "
            "ISO horizon the re-affirmation extends to)."
        )
    _valid_review_horizon(review_horizon)

    # ── authorship floor (part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "registration-review gate: re-affirming a registration is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot re-affirm it. Name the registration "
            f"({registration_id!r}) and the dossier sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration-review gate: the re-affirmation must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── recompute leg: reduce to the winner; the live dossier must NOT have drifted ──
    records = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)
    peek = reduce_registration(records, registration_id=registration_id, live_dossier_sha=None)
    winner = peek.winner
    if winner is None or peek.status in (REG_ABSENT, REG_REVOKED):
        raise errors.SpecInvalid(
            f"registration-review gate: no current registration named {registration_id!r} to "
            f"re-affirm (status {peek.status!r}). A review extends a LIVE registration's horizon; "
            "there is nothing to re-affirm — re-register the subject."
        )
    run_id = winner.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise errors.SpecInvalid(
            f"registration-review gate: the winning registration for {registration_id!r} carries "
            "no run_id, so the live dossier signature cannot be recomputed to confirm nothing "
            "drifted."
        )
    recorded_sha = winner.get("dossier_sha")
    sig = export_dossier.compute_dossier_signature(
        experiment_dir, run_id, include_lineage=bool(winner.get("include_lineage", False))
    )
    if sig.bundle_sha256 != recorded_sha:
        raise errors.SpecInvalid(
            "registration-review gate: the live dossier signature "
            f"({sig.bundle_sha256[:12]}...) does not match the registration's recorded "
            f"dossier_sha ({str(recorded_sha)[:12]}...). You CANNOT re-affirm a registration whose "
            "sealed stores have DRIFTED (C-horizon) — the remedy is re-registration, not review."
        )
    if dossier_sha != recorded_sha:
        raise errors.SpecInvalid(
            "registration-review gate: resolved['dossier_sha'] "
            f"({dossier_sha[:12]}...) does not name the registration's sealed dossier "
            f"({str(recorded_sha)[:12]}...). A review re-affirms the EXISTING dossier; name "
            "its sha."
        )

    # ── authorship floor (part 2): name the dossier sha by an 8+ hex prefix ──
    if not _names_target_sha_prefix(authored, str(recorded_sha)):
        _refuse_missing_authorship(
            "registration-review gate: the re-affirmation must NAME the dossier sha by an 8+ "
            "hex-character prefix (a token that can only derive from the presented "
            "verify-registration brief). Quote the dossier sha prefix you reviewed."
        )


def _assert_conformance_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The live-conformance drift-verdict gate (C-verdict) — a DATED CONCLUSION.

    A human's resolution of a ``needs_verdict`` / ``nonconforming`` FINDING is an
    ordinary ``append-decision`` (block ``conformance-verdict`` on scope kind
    ``registration`` — no verdict verb, the no-unlock-verb doctrine), citing the
    offending receipts by sha. ``resolved = {registration_id, cites: [<receipt
    content_sha>, ...], note}`` — ``cites`` NON-EMPTY, ``note`` a free-text opaque
    dated conclusion. The verdict binds NO dossier (it is dated evidence, never a
    re-registration) and never mutates the registration's status.

    **Lock 2 (recompute — the E-shape citation posture).** Every cited sha is
    resolved SERVER-SIDE against the registration's conformance ledger
    (:func:`~hpc_agent.state.conformance_store.read_observations`): a sha the ledger
    does NOT carry is refused (a caller-asserted sha is never trusted-then-recorded).

    **Lock 3 (authorship, the R6 bar reused, tiered on the harness log via
    :func:`_registration_authored_text`).** A bare ack cannot resolve a finding;
    the response must NAME the ``registration_id`` token-exact AND at least one
    cited receipt sha by an 8+ hex prefix (:func:`_names_any_sha_prefix`).

    Authorship-bar refusals carry the E2 marker (a freshly typed verdict resolves
    them); the shape / citation refusals raise plain :class:`errors.SpecInvalid`
    UNMARKED (a re-elicit cannot conjure a receipt the ledger never carried).
    """
    from hpc_agent.state.conformance_store import read_observations

    # ── Lock 2 shape: id + non-empty cites + a dated note ──
    registration_id = resolved.get("registration_id")
    if not isinstance(registration_id, str) or not registration_id:
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved must name a non-empty registration_id (the "
            "registration whose drift this verdict resolves)."
        )
    cites = resolved.get("cites")
    if not isinstance(cites, list) or not cites:
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved['cites'] must be a NON-EMPTY list of the "
            "offending receipt content_shas the verdict resolves (C-verdict — a drift verdict "
            "cites the receipts it judges)."
        )
    cite_shas: list[str] = []
    for c in cites:
        if not isinstance(c, str) or not c:
            raise errors.SpecInvalid(
                f"conformance-verdict gate: each cite must be a non-empty content_sha string; "
                f"got {c!r}."
            )
        cite_shas.append(c)
    note = resolved.get("note")
    if not isinstance(note, str) or not note.strip():
        raise errors.SpecInvalid(
            "conformance-verdict gate: resolved must carry a free-text 'note' — the human's "
            "dated conclusion over the cited drift (opaque to core, but a verdict is a dated "
            "CONCLUSION, never a bare citation)."
        )

    # ── Lock 3 (part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conformance-verdict gate: judging a drift FINDING is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot resolve it. Name the registration "
            f"({registration_id!r}) and a cited receipt sha you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "conformance-verdict gate: the verdict must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the registration."
        )

    # ── Lock 2 (recompute): every cited sha must be CARRIED by the ledger ──
    ledger_records, _skipped = read_observations(experiment_dir, registration_id)
    ledger_shas = {
        str(r.get("content_sha"))
        for r in ledger_records
        if isinstance(r.get("content_sha"), str) and r.get("content_sha")
    }
    unknown = [c for c in cite_shas if c not in ledger_shas]
    if unknown:
        raise errors.SpecInvalid(
            "conformance-verdict gate: cited content_sha(s) "
            f"{[c[:12] + '...' for c in unknown]} are NOT carried by registration "
            f"{registration_id!r}'s conformance ledger — a verdict may only cite receipts that "
            "EXIST on record (the E-shape citation posture; a caller-asserted sha is never "
            "trusted-then-recorded). Quote the offending receipts' shas from the "
            "conformance-status brief."
        )

    # ── Lock 3 (part 2): name at least one cited receipt sha by an 8+ hex prefix ──
    if not _names_any_sha_prefix(authored, cite_shas):
        _refuse_missing_authorship(
            "conformance-verdict gate: the verdict must NAME at least one cited receipt sha by an "
            "8+ hex-character prefix (a token that can only derive from the presented evidence "
            "brief). Quote an offending receipt's content_sha prefix."
        )


# ── reproduction-verdict authorship gate (D-consume admission, T12) ───────────


def _match_ledger_sha_prefix(
    authored: str, candidate_shas: set[str]
) -> tuple[str | None, str | None]:
    """Match the 8+ hex prefixes in *authored* against *candidate_shas*.

    Returns ``(full_sha, ambiguity)``:

    * a UNIQUE match → ``(full_sha, None)`` (the full bind-locked sha the store
      join keys on);
    * NO match → ``(None, None)`` — the caller distinguishes "no prefix named at
      all" from "a prefix matched nothing" by re-testing :data:`_HEX_RUN_RE`;
    * an AMBIGUOUS match → ``(None, reason)`` naming the COUNT, never the shas: the
      count is the disclosure a human needs to narrow the prefix; printing the
      colliding shas would hand back the very evidence the naming bar demands they
      quote.

    Reuses the R6 sha-prefix vocabulary (:data:`_HEX_RUN_RE`, 8+ hex) — an 8-hex
    prefix exists nowhere in a human's prior vocabulary and can only derive from
    the presented reproduction evidence.
    """
    runs = [m.group(0).lower() for m in _HEX_RUN_RE.finditer(authored or "")]
    lowered = {s.lower(): s for s in candidate_shas}
    matched: set[str] = set()
    for run in runs:
        hits = {orig for low, orig in lowered.items() if low.startswith(run)}
        if len(hits) > 1:
            return None, (
                f"the named 8-hex prefix {run!r} matches {len(hits)} distinct ledger "
                "samples (ambiguous) — quote a LONGER prefix that names exactly one sample"
            )
        matched |= hits
    if not matched:
        return None, None
    if len(matched) > 1:
        return None, (
            f"the response names {len(matched)} distinct ledger samples by prefix — a "
            "reproduction verdict resolves exactly ONE sample; name a single content_sha"
        )
    return matched.pop(), None


def _assert_reproduction_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """D-consume admission gate for a fingerprint-sample acceptance/rejection (T12).

    A ``needs_verdict`` / ``mismatch`` fingerprint sample joins the determinism
    envelope ONLY when the reproduction run's decision journal carries a
    ``reproduction-verdict`` record whose ``resolved`` names the sample's
    ``content_sha`` TOKEN-EXACT with ``accept: true`` (the store-layer admission
    join, ``state/fingerprint_store.py::_is_admitted``). Without a gate an AGENT
    could append that acceptance and launder a mismatch into the envelope — the
    accumulation attack the D-consume admission rule exists to close. This gate is
    that lock, beside :func:`_assert_signoff_authorship` /
    :func:`_assert_registration_authorship`, same three-lock structure.

    Block convention (one direction — the run scope legitimately carries MANY
    blocks, so it is not made exclusive like ``registration``): the
    ``reproduction-verdict`` block is refused for any ``scope_kind`` other than
    ``"run"`` (it rides the reproduction run's journal); nothing else claims the
    block, so every other record passes untouched.

    ``resolved['accept']`` must be a real bool — an acceptance (``true``, the join
    predicate the store reads) AND a rejection (``false``) both face the full bar
    (a reject is a human judgment too, no cheaper path).

    Authorship bar, tiered exactly like the registration sha-prefix leg
    (:func:`_registration_authored_text` — the harness utterance log LOCK when
    present, the agent-relayed ``response`` FRICTION tier otherwise; NO waiver /
    auto-clear tier):

    * a bare ack (:func:`_is_bare_ack`) cannot resolve a verdict, and
    * the authored text must NAME the accepted sample's ``content_sha`` by an 8+
      hex prefix (the R6 form).

    Recompute leg: the gate re-reads THIS run's fingerprint ledger
    (``state/fingerprint_store.py::read_samples`` for the ``cmd_sha`` resolved from
    the run sidecar — the journal ``scope_id`` IS the reproduction run id) and
    refuses a prefix that matches nothing (``acceptance naming no sample``) or that
    matches ambiguously (refused naming the COUNT). Candidate shas are the samples
    whose SECOND ``run_ids`` member is this run — exactly the samples this verdict
    can admit.

    Prefix canonicalization (the store-join enabler): on a unique match the gate
    REWRITES ``resolved['content_sha']`` to the FULL matched sha before append, so
    the store-layer join (``resolved.content_sha == sample.content_sha``, token-exact
    on the full sha) admits. A pre-filled ``resolved['content_sha']`` that does not
    extend the named sample is a structural inconsistency, refused.

    Marking (the E2 scoping): the authorship-bar refusals (bare ack, no/ambiguous/
    unmatched prefix) carry the elicitation marker via
    :func:`_refuse_missing_authorship` — a freshly typed human utterance naming the
    right prefix resolves them. The STRUCTURAL refusals (wrong scope kind,
    non-bool ``accept``, an unresolvable sidecar / cmd_sha, a contradicting
    pre-filled ``content_sha``) raise plain :class:`errors.SpecInvalid` UNMARKED —
    a re-elicit cannot fix them.
    """
    from hpc_agent.state.fingerprint_store import REPRODUCTION_VERDICT_BLOCK, read_samples
    from hpc_agent.state.runs import read_run_sidecar

    if spec.block != REPRODUCTION_VERDICT_BLOCK:
        return  # nothing else claims this block

    # Block↔scope convention: the verdict rides the reproduction RUN's journal.
    if spec.scope_kind != "run":
        raise errors.SpecInvalid(
            f"block {REPRODUCTION_VERDICT_BLOCK!r} is only valid for scope_kind='run' "
            f"(it rides the reproduction run's decision journal); got "
            f"scope_kind={spec.scope_kind!r}."
        )

    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved must be a mapping carrying "
            "{accept: bool, content_sha}."
        )

    # accept is the store's join predicate (`accept is True`) — it MUST be a real
    # bool. A rejection (false) faces the same authorship bar as an acceptance.
    accept = resolved.get("accept")
    if not isinstance(accept, bool):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved['accept'] must be a bool (true admits "
            "the sample into the determinism envelope, false records the rejection); "
            f"got {accept!r}."
        )

    # Authorship floor: a bare ack cannot resolve a needs_verdict / mismatch — the
    # admission is deliberately effortful (the D-attention rarity-plus-typing bet).
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "reproduction-verdict gate: admitting (or rejecting) a reproduction sample "
            f"is a HUMAN act — a bare {spec.response!r} (a 'y' / click) cannot resolve it. "
            "Name the sample's content_sha (an 8+ hex prefix) and state your verdict."
        )

    # Recompute leg: re-read THIS run's fingerprint ledger. cmd_sha comes from the
    # run sidecar (the scope_id IS the reproduction run id); an unresolvable sidecar
    # / cmd_sha is a STRUCTURAL refusal (a re-elicit cannot conjure the ledger).
    try:
        sidecar = read_run_sidecar(experiment_dir, spec.scope_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise errors.SpecInvalid(
            "reproduction-verdict gate: could not read the reproduction run's sidecar "
            f"for run {spec.scope_id!r} ({exc}) — the gate resolves cmd_sha from it to "
            "re-read the fingerprint ledger. An unresolvable run is refused, never skipped."
        ) from exc
    cmd_sha = str(sidecar.get("cmd_sha") or "")
    if not cmd_sha:
        raise errors.SpecInvalid(
            "reproduction-verdict gate: the run sidecar for "
            f"{spec.scope_id!r} carries no cmd_sha, so the fingerprint ledger cannot be "
            "located to verify the named sample."
        )

    samples, _skipped = read_samples(experiment_dir, cmd_sha)
    candidate_shas: set[str] = set()
    for sample in samples:
        run_ids = sample.get("run_ids")
        if not isinstance(run_ids, (list, tuple)) or len(run_ids) < 2:
            continue
        if run_ids[1] != spec.scope_id:  # only samples THIS verdict can admit
            continue
        content_sha = sample.get("content_sha")
        if isinstance(content_sha, str) and content_sha:
            candidate_shas.add(content_sha)

    # Tiered evidence source (utterance-log LOCK > journal-response FRICTION; NO
    # waiver tier) — the shared registration sha-prefix tiering.
    authored = _registration_authored_text(experiment_dir, response)
    full_sha, ambiguity = _match_ledger_sha_prefix(authored, candidate_shas)
    if ambiguity is not None:
        _refuse_missing_authorship("reproduction-verdict gate: " + ambiguity + ".")
    if full_sha is None:
        if _HEX_RUN_RE.search(authored):
            _refuse_missing_authorship(
                "reproduction-verdict gate: the response names an 8+ hex prefix that "
                f"matches NO sample in run {spec.scope_id!r}'s fingerprint ledger. A "
                "verdict must name the content_sha of a sample that EXISTS on record — "
                "quote the prefix from the reproduction receipt / evidence brief."
            )
        _refuse_missing_authorship(
            "reproduction-verdict gate: the response must NAME the accepted sample's "
            "content_sha by an 8+ hex-character prefix (the R6 form — a token that can "
            "only derive from the presented evidence). Quote the sample's content_sha "
            "prefix from the reproduction receipt."
        )

    # Prefix canonicalization: rewrite resolved['content_sha'] to the FULL matched
    # sha so the store-layer admission join (resolved.content_sha == sample.content_sha,
    # token-exact on the full bind-locked sha) admits. A pre-filled content_sha that
    # does not extend the named sample is a structural inconsistency, refused.
    existing = resolved.get("content_sha")
    if isinstance(existing, str) and existing and not full_sha.lower().startswith(existing.lower()):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved['content_sha'] "
            f"({existing!r}) does not extend the sample named by the response "
            f"(content_sha {full_sha!r}). Do not hand-commit a content_sha that "
            "disagrees with the named prefix; name the prefix and let the gate "
            "canonicalize it to the full sha."
        )
    resolved["content_sha"] = full_sha


# ── conclusion authorship gate (E-shape's three locks + the revoke floor, T8) ──


def _names_citation_sha_prefix(text: str, citations: Sequence[Any]) -> str | None:
    """The first citation ``sha`` a hex run in *text* prefixes, or None.

    E-shape lock 3 (the R6 sha-prefix bar reused VERBATIM): a finding's response
    must NAME at least one CITED sha by an 8+ hex-character prefix — a token that
    exists nowhere in a human's prior vocabulary and can only derive from the
    presented evidence (the rendered evidence-brief). Case-insensitive prefix
    match against the citations the gate just verified.
    """
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        for cit in citations:
            if str(cit.sha).lower().startswith(run):
                return str(cit.sha)
    return None


def _conclusion_dossier_resolver(experiment_dir: Path) -> Callable[[str], str | None]:
    """The INJECTED dossier resolver for citation dispatch (drift-log item 2).

    ``state/evidence.py`` never imports ``ops``; the dossier resolver
    (``compute_dossier_signature``) lives in ``ops/export_dossier.py`` and is
    passed IN here — reached through the top-level ``export_dossier`` facade via
    the package-alias form the subject-import lint permits from inside the
    ``decision`` subject (the ``_assert_registration_full`` precedent). A dossier
    ``ref`` is a ``run_id``; the resolved answer is that run's dossier
    ``bundle_sha256``. An unresolvable dossier (no such run, a store gone) returns
    ``None`` → :func:`state.evidence.resolve_citation` reports it unresolvable →
    the append refuses loudly (verification at append is load-bearing).
    """
    from hpc_agent.ops import export_dossier

    def _resolve(ref: str) -> str | None:
        try:
            sig = export_dossier.compute_dossier_signature(experiment_dir, ref)
        except Exception:  # noqa: BLE001 — any resolution failure is "unresolvable here"
            return None
        return sig.bundle_sha256

    return _resolve


def _assert_conclusion_revoke_floor(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The E-shape revoke floor (the R7 form) for a ``conclusion-revoke`` record.

    A human withdrawal: non-bare, NAMES the ``conclusion_id``, and its free-text
    ``reason`` is MANDATORY. It binds no new sha (it withdraws — a conclusion is
    dated evidence, never re-verified at withdrawal), so there is NO recompute
    leg; but it is journaled, attributed, and permanent like everything else. The
    bare-ack and id-naming refusals carry the E2 authorship marker (a freshly
    typed human rationale resolves them); the missing-reason refusal is structural
    (the agent must add ``resolved['reason']``), left UNMARKED.
    """
    conclusion_id = resolved.get("conclusion_id")
    if not isinstance(conclusion_id, str) or not conclusion_id:
        raise errors.SpecInvalid(
            "conclusion-revoke gate: resolved must name a non-empty conclusion_id "
            "(the finding being withdrawn)."
        )
    reason = resolved.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise errors.SpecInvalid(
            "conclusion-revoke gate: a revoke MUST carry a free-text resolved['reason'] "
            "(why the finding no longer holds). It binds no new sha, but it is journaled, "
            "attributed, and permanent."
        )
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conclusion-revoke gate: withdrawing a conclusion is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot revoke it. State why you are "
            f"withdrawing the conclusion ({conclusion_id!r})."
        )
    # B4 ts>=anchor: name the conclusion in an utterance logged AT OR AFTER the
    # conclusion it withdraws — the creation utterance (which named the id) no
    # longer self-satisfies the naming leg. Anchor = the conclusion filing ts.
    from hpc_agent.state.evidence import CONCLUSION_BLOCK

    anchor = _target_record_ts(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        filing_block=CONCLUSION_BLOCK,
        id_field="conclusion_id",
        target_id=conclusion_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, conclusion_id):
        _refuse_missing_authorship(
            "conclusion-revoke gate: the revoke must NAME the conclusion_id "
            f"{conclusion_id!r} token-exact (the #26 floor), in an utterance logged "
            "AT OR AFTER the conclusion it withdraws (B4 ts>=anchor). Restate, naming "
            "the conclusion being withdrawn."
        )


def _assert_conclusion_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """E-shape's three locks for a ``conclusion`` record — a human-authored finding.

    A conclusion is an ordinary ``append-decision`` whose ``scope_kind=="conclusion"``,
    ``block=="conclusion"``, and ``resolved={conclusion_id, tags, concludes?,
    citations, finding}`` (E-shape). It records a dated, sha-linked finding over
    sealed evidence — a HUMAN attestation, so it faces both the un-fakeable
    citation-verification lock and the tiered authorship bar.

    **Lock 1 (no affordance)** is organizational: there is no conclusion verb /
    chain / next_block / skill — append-decision under this block is the only write
    path (pinned by the T11 contract test; no primitive is named conclude).

    **Lock 2 (recompute, un-fakeable)** — the ``resolved`` shape is validated
    server-side (:func:`state.evidence.validate_conclusion_resolved` — slug-validated
    ``conclusion_id``, shape-validated ``tags``, a NON-EMPTY ``citations`` list). Then
    EVERY citation is resolved against the LIVE stores through its kind's ONE resolver
    (:func:`state.evidence.resolve_citation`, the ``dossier`` slot fed the injected
    :func:`_conclusion_dossier_resolver`): an unresolvable or mismatched citation
    REFUSES with the recorded-vs-live pair — you cannot conclude about evidence the
    machine cannot find on this namespace at write time (the receipt-laundering hole,
    closed at the memory boundary). The whole verified set is then hash-locked: its
    canonical ``content_sha`` (:func:`state.evidence.citations_content_sha`) binds
    through the ONE attestation kernel (:func:`state.attestation.bind`) and is persisted
    into ``resolved`` so the reduction's stored-sha fallback agrees.

    **Lock 3 (authorship, the R6 bar reused)** — bare acks refused
    (:func:`_is_bare_ack`); the response must NAME the ``conclusion_id`` token-exact
    AND name at least one CITED sha by an 8+ hex prefix
    (:func:`_names_citation_sha_prefix`) matched against the citations just verified.
    Tiered on the harness utterance log (:func:`_registration_authored_text` — the
    LOCK when present, the agent-relayed ``response`` FRICTION tier otherwise). There
    is NO auto-clear / redundant tier: a conclusion's attestor is ALWAYS human (a
    machine has no findings, only measurements, and the measurements are already
    attested elsewhere).

    The authorship-bar refusals (bare ack, missing id, missing sha-prefix) carry the
    E2 marker via :func:`_refuse_missing_authorship`; the Lock-2 shape / citation
    refusals raise plain :class:`errors.SpecInvalid` UNMARKED (a re-elicit cannot fix
    a moved or absent evidence sha — the E2 scoping).
    """
    from hpc_agent.state import attestation
    from hpc_agent.state.evidence import (
        SUBJECT_KIND as CONCLUSION_SUBJECT_KIND,
    )
    from hpc_agent.state.evidence import (
        citations_content_sha,
        resolve_citation,
        validate_conclusion_resolved,
    )

    # ── Lock 2 shape: slug-validated id + non-empty shape-validated citations ──
    parsed = validate_conclusion_resolved(resolved)

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "conclusion gate: recording a finding is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot conclude. Name the conclusion "
            f"({parsed.conclusion_id!r}) and a cited sha prefix you reviewed."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, parsed.conclusion_id):
        _refuse_missing_authorship(
            "conclusion gate: the finding must NAME the conclusion_id "
            f"{parsed.conclusion_id!r} token-exact (the #26 floor). Restate, naming the "
            "conclusion."
        )

    # ── Lock 2 (recompute — the load-bearing verification): every citation must
    # resolve AND match against the LIVE stores or the append is refused. ──
    dossier_resolver = _conclusion_dossier_resolver(experiment_dir)
    for cit in parsed.citations:
        res = resolve_citation(experiment_dir, cit, dossier_resolver=dossier_resolver)
        if not res.resolved:
            raise errors.SpecInvalid(
                f"conclusion gate (lock 2): citation {cit.kind}:{cit.ref!r} is UNRESOLVABLE "
                f"on this namespace ({res.detail}) — a conclusion may only cite evidence the "
                "machine can find at write time. Cite evidence that exists on this namespace."
            )
        if not res.matches:
            raise errors.SpecInvalid(
                f"conclusion gate (lock 2): citation {cit.kind}:{cit.ref!r} sha MISMATCH "
                f"({res.detail}) — the asserted sha {cit.sha!r} is not what the live store "
                "carries. A caller-asserted sha is never trusted-then-recorded (the "
                "receipt-laundering hole). Quote the live sha."
            )

    # ── content_sha bound via the ONE kernel against the re-canonicalized set ──
    content_sha = citations_content_sha(parsed.citations)
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": CONCLUSION_SUBJECT_KIND,
            "subject_id": parsed.conclusion_id,
            "content_sha": content_sha,
        },
        recompute=lambda: citations_content_sha(parsed.citations),
    )
    # Persist the hash-lock so reduce_conclusion's stored-sha fallback agrees.
    resolved["content_sha"] = content_sha

    # ── Lock 3, part 2: the raised bar — name a CITED sha by an 8+ hex prefix ──
    if _names_citation_sha_prefix(authored, parsed.citations) is None:
        _refuse_missing_authorship(
            "conclusion gate (lock 3): the finding must NAME at least one cited sha by an "
            "8+ hex-character prefix (the diff-token pattern at its strongest — a token that "
            "exists nowhere in a human's prior vocabulary and can only derive from the "
            "presented evidence). Quote a cited sha prefix from the evidence-brief."
        )


def _assert_conclusion_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The E-shape conclusion gate — evidence memory's one new attested record (T8).

    Block convention, enforced BOTH directions (mirrors ``registration`` /
    ``notebook-sign-off``): a conclusion-family block
    (:data:`state.evidence.CONCLUSION_BLOCK_FAMILY`) is refused for any
    ``scope_kind`` other than ``"conclusion"``; and the ``"conclusion"`` scope
    accepts ONLY the block family (``conclusion`` / ``conclusion-revoke``). Every
    other record passes untouched. Dispatches a ``conclusion-revoke`` to the revoke
    floor and a ``conclusion`` to the full three locks.

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals carry
    the E2 marker so the single append firing site covers conclusions over MCP too;
    shape / citation refusals stay unmarked).
    """
    from hpc_agent.state.evidence import (
        CONCLUSION_BLOCK_FAMILY,
        CONCLUSION_REVOKE_BLOCK,
    )

    block = spec.block
    in_family = block in CONCLUSION_BLOCK_FAMILY
    # A conclusion-family block is conclusion-scope-only.
    if in_family and spec.scope_kind != "conclusion":
        raise errors.SpecInvalid(
            f"block {block!r} is a conclusion-family block, only valid for "
            f"scope_kind='conclusion'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "conclusion":
        return  # not a conclusion record — nothing to gate
    # The conclusion scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='conclusion' accepts only its block family "
            f"{sorted(CONCLUSION_BLOCK_FAMILY)}; got block={block!r} — a conclusion scope "
            "records ONLY conclusion / conclusion-revoke (E-shape)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "conclusion gate: resolved must be a mapping carrying the conclusion fields "
            "{conclusion_id, tags, citations, finding}."
        )
    if block == CONCLUSION_REVOKE_BLOCK:
        _assert_conclusion_revoke_floor(experiment_dir, spec, resolved)
        return
    # block == CONCLUSION_BLOCK — the human finding (E-shape three locks).
    _assert_conclusion_full(experiment_dir, spec, resolved)


# ── challenge authorship gate (C-gate: three locks + the verdict/withdraw floors, T5) ──


def _names_target_sha_prefix(text: str, sha: str) -> bool:
    """True iff a hex run in *text* is an 8+ hex prefix of *sha* (the R6 form).

    C-gate lock 3, the "name what you attack" leg: the challenge response must name
    the TARGET's ``content_sha`` by an 8+ hex-character prefix — a token that exists
    nowhere in a human's prior vocabulary and can only derive from the presented
    record. Case-insensitive prefix match (:data:`_HEX_RUN_RE`).
    """
    lowered = str(sha).lower()
    for run in (m.group(0).lower() for m in _HEX_RUN_RE.finditer(text or "")):
        if lowered.startswith(run):
            return True
    return False


def _challenge_filing_citations(experiment_dir: Path, challenge_id: str) -> Sequence[Any]:
    """The verified citations of *challenge_id*'s FILING record, or ``()``.

    Reads the challenge's own journal (the C-shape per-id thread), finds the
    newest ``challenge`` filing record, and returns its validated
    :class:`~hpc_agent.state.evidence.Citation` list — the shas a DISMISSAL must
    engage (C-gate: dismissing evidence requires naming it). ``()`` when no
    parseable filing exists (the caller then refuses the resolution — you cannot
    resolve a challenge that was never filed).
    """
    from hpc_agent.state.challenges import CHALLENGE_BLOCK, validate_challenge_resolved

    citations: Sequence[Any] = ()
    for rec in _read_decisions(experiment_dir, "challenge", challenge_id):
        if rec.get("block") != CHALLENGE_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get("challenge_id") != challenge_id:
            continue
        try:
            citations = validate_challenge_resolved(resolved).citations
        except errors.SpecInvalid:
            continue
    return citations


def _challenge_filing_attestor(experiment_dir: Path, challenge_id: str) -> str | None:
    """The ``attestor_id`` (challenger) of *challenge_id*'s newest FILING, or ``None``.

    MH7: the resolver≠challenger / withdrawer==challenger comparisons read WHO
    filed the challenge from the filing record's own ``attestor_id`` — the opaque
    actor slug the ops append stamped at filing time (server-resolved, never
    caller-suppliable). ``None`` when no parseable filing exists OR the filing was
    unattributed (a zero/one-actor filing, or a >1-actor filing with no resolvable
    session actor) — the caller's >1-actor guard then decides the refusal.
    """
    from hpc_agent.state.challenges import CHALLENGE_BLOCK

    attestor: str | None = None
    for rec in _read_decisions(experiment_dir, "challenge", challenge_id):
        if rec.get("block") != CHALLENGE_BLOCK:
            continue
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict) or resolved.get("challenge_id") != challenge_id:
            continue
        raw = rec.get("attestor_id")
        attestor = raw if isinstance(raw, str) and raw else None
    return attestor


def _recompute_challenge_view_sha(
    experiment_dir: Path, challenge_id: str, carried_view_sha: str
) -> None:
    """RECOMPUTE a carried ``view_sha`` against the challenge-status render (C-verb).

    The ``challenge-status`` brief is a PURE FUNCTION of the projection (no
    wall-clock, no fleet accounting — ``ops/challenge_status_op.py``), so a
    ``view_sha`` a human bound after reading the thread is recomputable: the gate
    re-invokes the ONE render (never a second inlined projection — the v1.6
    recomputable-render precedent) and refuses a mismatch. Reached through the
    top-level ``challenge_status_op`` role-root module (the subject-import lint
    permits the ops-facade form from inside the ``decision`` subject — the
    ``export_dossier`` precedent).

    Structural refusal (UNMARKED): a stale ``view_sha`` names a view that no longer
    renders — a re-elicited utterance cannot fix it, so it carries no E2 marker.

    Runtime note (drift log): the render routes through
    ``state/challenges.py::standing_challenges`` — the ONE collector; the op⇄state
    (T1) and op⇄wire (T2) entry-shape reconciliation is the Wave-A/B integrator's
    step, so this recompute is exercised under the same collector monkeypatch the
    op's own tests use until that lands. The op is reached via
    :func:`importlib.import_module` (not a static ``from ... import``) precisely
    because the op module is PRE-INTEGRATION and carries the placeholder-vs-real
    T2/T1 type divergence: a followed import would drag those known-transient
    errors into this subject's type-check. ``view_sha`` is OPTIONAL, so a verdict
    that carries none never reaches here.
    """
    import importlib

    op = importlib.import_module("hpc_agent.ops.challenge_status_op")
    result = op.challenge_status(
        experiment_dir=experiment_dir,
        spec=op.ChallengeStatusSpec(challenge_id=challenge_id),
    )
    if result.view_sha != carried_view_sha:
        raise errors.SpecInvalid(
            "challenge resolution gate (view_sha recompute): the carried view_sha "
            f"{carried_view_sha!r} does not match the challenge-status render for "
            f"{challenge_id!r} (recomputed {result.view_sha!r}). The view moved after "
            "the human signed it — re-read `challenge-status` and bind the current view_sha."
        )


def _assert_challenge_filing_full(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-gate three locks for a ``challenge`` FILING — human-authored dissent.

    A challenge is an ordinary ``append-decision`` whose ``scope_kind=="challenge"``,
    ``block=="challenge"``, ``resolved={challenge_id, target, citations, grounds}``
    (C-shape). It records a dated, sha-targeted, evidence-bound attestation of
    DISSENT — a HUMAN act (C3: code never files dissent), so it faces both the
    un-fakeable target/citation-verification lock and the raised authorship bar.

    **Lock 1 (no affordance)** — append-decision under this block is the ONLY write
    path; no challenge/contest/dispute/refute verb, chain, or next_block (pinned by
    the T9 contract test).

    **Lock 2 (recompute, un-fakeable)** — ``resolved`` validated server-side
    (:func:`state.challenges.validate_challenge_resolved`: slug ``challenge_id``, a
    full-address ``target``, a NON-EMPTY ``citations`` list, non-empty ``grounds``).
    Then the TARGET is resolved server-side and confirmed committed at the asserted
    ``content_sha`` (:func:`state.challenges.resolve_target_existence` — the
    ``attestation`` kind SCANS the named journal so a non-newest record is findable,
    C2); every CITATION is resolved against the LIVE stores
    (:func:`state.evidence.resolve_citation`) and refused on unresolvable/mismatch —
    you cannot contest what the machine cannot find, nor rest on evidence it cannot
    resolve (the receipt-laundering hole at the dissent boundary). The verified
    ``{target, citations}`` set is then hash-locked: its canonical ``content_sha``
    (:func:`state.challenges.challenge_content_sha`) binds through the ONE kernel
    (:func:`state.attestation.bind`) and persists into ``resolved``.

    **Lock 3 (authorship, the R6 bar reused)** — bare acks refused
    (:func:`_is_bare_ack`); the response must NAME the ``challenge_id`` token-exact
    AND the TARGET's ``content_sha`` by an 8+ hex prefix (:func:`_names_sha_prefix` —
    you must name what you attack) AND at least one CITED sha by an 8+ hex prefix
    (:func:`_names_citation_sha_prefix` — you must name what you rest on). Tiered on
    the harness utterance log (:func:`_registration_authored_text`). NO auto-clear
    tier: a challenge's attestor is ALWAYS human (C3).

    Authorship-bar refusals carry the E2 marker (:func:`_refuse_missing_authorship`);
    Lock-2 shape/target/citation refusals raise plain :class:`errors.SpecInvalid`
    UNMARKED (a re-elicit cannot conjure a moved or absent sha — the E2 scoping).
    """
    from hpc_agent.state import attestation
    from hpc_agent.state.challenges import (
        SUBJECT_KIND as CHALLENGE_SUBJECT_KIND,
    )
    from hpc_agent.state.challenges import (
        challenge_content_sha,
        resolve_target_existence,
        validate_challenge_resolved,
    )
    from hpc_agent.state.evidence import resolve_citation

    # ── Lock 2 shape: slug id + full-address target + non-empty citations/grounds ──
    parsed = validate_challenge_resolved(resolved)

    # ── Base authorship floor (Lock 3, part 1): non-bare + names the id ──
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "challenge gate: filing structured dissent is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot challenge. Name the challenge "
            f"({parsed.challenge_id!r}), the target sha you attack, and a cited sha you rest on."
        )
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, parsed.challenge_id):
        _refuse_missing_authorship(
            "challenge gate: the filing must NAME the challenge_id "
            f"{parsed.challenge_id!r} token-exact (the #26 floor). Restate, naming the challenge."
        )

    # ── Lock 2 (recompute): the target must exist committed at the asserted sha ──
    dossier_resolver = _conclusion_dossier_resolver(experiment_dir)
    target_res = resolve_target_existence(
        experiment_dir, parsed.target, dossier_resolver=dossier_resolver
    )
    if not target_res.resolved:
        raise errors.SpecInvalid(
            f"challenge gate (lock 2): the target "
            f"{parsed.target.kind}:{parsed.target.subject_kind}/{parsed.target.subject_id} "
            f"is UNRESOLVABLE on this namespace ({target_res.detail}) — you cannot contest a "
            "record the machine cannot find. Address a committed record that exists here."
        )
    if not target_res.matches:
        raise errors.SpecInvalid(
            f"challenge gate (lock 2): the target subject exists but carries NO committed "
            f"record at the asserted content_sha {parsed.target.content_sha!r} "
            f"({target_res.detail}). A challenge binds to an exact committed sha (R3); quote "
            "the sha of a record that exists on record."
        )

    # ── Lock 2 (recompute): every citation must resolve AND match the live store ──
    for cit in parsed.citations:
        res = resolve_citation(experiment_dir, cit, dossier_resolver=dossier_resolver)
        if not res.resolved:
            raise errors.SpecInvalid(
                f"challenge gate (lock 2): citation {cit.kind}:{cit.ref!r} is UNRESOLVABLE on "
                f"this namespace ({res.detail}) — a challenge may only rest on evidence the "
                "machine can find at write time. Cite evidence that exists on this namespace."
            )
        if not res.matches:
            raise errors.SpecInvalid(
                f"challenge gate (lock 2): citation {cit.kind}:{cit.ref!r} sha MISMATCH "
                f"({res.detail}) — the asserted sha {cit.sha!r} is not what the live store "
                "carries. A caller-asserted sha is never trusted-then-recorded (the "
                "receipt-laundering hole). Quote the live sha."
            )

    # ── content_sha bound via the ONE kernel against the re-canonicalized set ──
    content_sha = challenge_content_sha(parsed.target, parsed.citations)
    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": CHALLENGE_SUBJECT_KIND,
            "subject_id": parsed.challenge_id,
            "content_sha": content_sha,
        },
        recompute=lambda: challenge_content_sha(parsed.target, parsed.citations),
    )
    resolved["content_sha"] = content_sha

    # ── Lock 3, part 2: name the TARGET sha AND a CITED sha by 8+ hex prefix ──
    if not _names_target_sha_prefix(authored, parsed.target.content_sha):
        _refuse_missing_authorship(
            "challenge gate (lock 3): the filing must NAME the TARGET's content_sha by an "
            "8+ hex-character prefix (you must name what you attack — a token that can only "
            "derive from the presented record). Quote the target sha prefix from the "
            "challenge-status / verify-registration brief."
        )
    if _names_citation_sha_prefix(authored, parsed.citations) is None:
        _refuse_missing_authorship(
            "challenge gate (lock 3): the filing must NAME at least one CITED sha by an "
            "8+ hex-character prefix (you must name what you rest on). Quote a cited sha "
            "prefix from the evidence you are standing on."
        )


def _assert_challenge_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any]
) -> None:
    """The C-gate verdict/withdraw FLOOR — resolving standing dissent (C4).

    A verdict (``challenge-verdict``) or withdrawal (``challenge-withdraw``) is a
    SEPARATE record from the filing (C-gate: so the resolver≠challenger constraint is
    expressible later without a record-shape change — see the resolver-identity note
    below). Both face the same floor: non-bare, ``challenge_id`` token-exact, and a
    mandatory free-text ``reasoning``/``reason`` (waving dissent away with a bare ack
    is exactly the asymmetry violation the nudge machinery exists to prevent, C4).

    A DISMISSAL additionally must NAME one of the CHALLENGE's cited shas by an 8+ hex
    prefix (:func:`_challenge_filing_citations` — dismissing evidence requires
    engaging it; dismissal is effortful by construction, C4). An UPHELD verdict needs
    no extra sha (upholding agrees with evidence already bound into the record it
    resolves). A carried ``view_sha`` is RECOMPUTED against the challenge-status
    render (:func:`_recompute_challenge_view_sha`, C-verb).

    **Resolver-identity extension (MH7 — LANDED HERE as the multi-human follow-up).**
    ``docs/design/multi-human.md`` MH7 owns attributed authorship and reserved this
    identity comparison to the challenge gate as "a follow-up task executed by
    whichever plan lands second". Multi-human landed second, so it lands here now:
    under >1 declared actors (MH1), the VERDICT gate refuses ``resolver ==
    challenger`` (you may not adjudicate your own objection — the challenger is the
    filing record's ``attestor_id``, :func:`_challenge_filing_attestor`; the resolver
    is the session actor) and refuses an UNATTRIBUTED resolution; the WITHDRAWAL gate
    refuses ``withdrawer != challenger`` (a second actor silencing another's standing
    dissent is the suppression channel). Pure identity over opaque slugs — Q1-clean.
    Zero/one actor declared → silent, byte-identical (a solo researcher legitimately
    resolves their own past challenge). These identity refusals are the loud/dangling
    posture (NOT the E2 marker — a re-elicited utterance cannot fix WHO the session
    is). The verdict/withdraw being a SEPARATE record from the filing is what kept the
    constraint expressible without a record-shape change.

    Authorship-bar refusals carry the E2 marker; the missing-reason / stale-view /
    MH7-identity structural refusals raise plain :class:`errors.SpecInvalid` UNMARKED.
    """
    from hpc_agent.state.challenges import (
        CHALLENGE_VERDICT_BLOCK,
        DISMISSED,
        validate_verdict_resolved,
        validate_withdraw_resolved,
    )

    is_verdict = spec.block == CHALLENGE_VERDICT_BLOCK
    if is_verdict:
        parsed_v = validate_verdict_resolved(resolved)
        challenge_id = parsed_v.challenge_id
    else:  # CHALLENGE_WITHDRAW_BLOCK
        parsed_w = validate_withdraw_resolved(resolved)
        challenge_id = parsed_w.challenge_id

    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            f"challenge-{'verdict' if is_verdict else 'withdraw'} gate: resolving a challenge "
            f"is a HUMAN act — a bare {spec.response!r} (a 'y' / click) cannot resolve it. "
            f"Name the challenge ({challenge_id!r}) and state your reasoning."
        )
    # B4 ts>=anchor: name the challenge in an utterance logged AT OR AFTER the
    # filing it resolves — the challenger named the id when FILING, so an
    # unbounded read lets an agent-composed verdict/withdraw ride the creation
    # utterance. Anchor = the challenge filing ts (the thread is keyed by
    # challenge_id; None → unfiltered, the filing-existence checks own that case).
    from hpc_agent.state.challenges import CHALLENGE_BLOCK

    anchor = _target_record_ts(
        experiment_dir,
        scope_kind="challenge",
        scope_id=challenge_id,
        filing_block=CHALLENGE_BLOCK,
        id_field="challenge_id",
        target_id=challenge_id,
    )
    authored = _fresh_authored_text(experiment_dir, response, anchor=anchor)
    if not _names_slug(authored, challenge_id):
        _refuse_missing_authorship(
            f"challenge-{'verdict' if is_verdict else 'withdraw'} gate: the resolution must "
            f"NAME the challenge_id {challenge_id!r} token-exact (the #26 floor), in an "
            "utterance logged AT OR AFTER the filing it resolves (B4 ts>=anchor). Restate, "
            "naming the challenge being resolved."
        )

    # A DISMISSAL must engage the challenge's evidence by naming a cited sha prefix.
    if is_verdict and parsed_v.verdict == DISMISSED:
        citations = _challenge_filing_citations(experiment_dir, challenge_id)
        if not citations:
            raise errors.SpecInvalid(
                "challenge-verdict gate: no parseable filing found for challenge "
                f"{challenge_id!r} — a verdict resolves a challenge that was filed. File the "
                "challenge before dismissing it."
            )
        if _names_citation_sha_prefix(authored, citations) is None:
            _refuse_missing_authorship(
                "challenge-verdict gate: a DISMISSAL must NAME one of the challenge's cited "
                "shas by an 8+ hex prefix — dismissing evidence requires engaging it (dismissal "
                "is effortful by construction, C4). Quote a cited sha prefix you are dismissing."
            )

    # A carried view_sha is recomputed against the challenge-status render (C-verb).
    view_sha = resolved.get("view_sha")
    if isinstance(view_sha, str) and view_sha:
        _recompute_challenge_view_sha(experiment_dir, challenge_id, view_sha)

    # ── MH7: resolver ≠ challenger (verdict) / withdrawer == challenger (withdraw) ──
    # Silent under zero/one declared actor (byte-identical — a solo researcher
    # legitimately resolves their own past challenge).
    ids, _policy = _read_interview_actors(experiment_dir)
    if len(ids) > 1:
        session_actor = _session_actor(experiment_dir, ids)
        challenger = _challenge_filing_attestor(experiment_dir, challenge_id)
        if is_verdict:
            if session_actor is None:
                raise errors.SpecInvalid(
                    "challenge-verdict gate (MH7): >1 actor is declared but this "
                    f"session has no resolvable actor, so challenge {challenge_id!r} "
                    "would be resolved anonymously — an unattributed adjudication is "
                    "the laundering channel. Configure HPC_ACTOR to a declared actor."
                )
            if challenger is not None and session_actor == challenger:
                raise errors.SpecInvalid(
                    "challenge-verdict gate (MH7): the resolver "
                    f"({session_actor!r}) is the CHALLENGER who filed "
                    f"{challenge_id!r} — you may not adjudicate your own objection. A "
                    "DIFFERENT declared actor must resolve it."
                )
        else:  # CHALLENGE_WITHDRAW_BLOCK
            if session_actor != challenger:
                raise errors.SpecInvalid(
                    "challenge-withdraw gate (MH7): only the CHALLENGER who filed "
                    f"{challenge_id!r} (actor {challenger!r}) may withdraw it — the "
                    f"session actor {session_actor!r} is someone else, and a second "
                    "actor silencing another's standing dissent is the suppression "
                    "channel. The challenger must withdraw their own challenge."
                )


def _assert_challenge_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """The challenge gate — structured dissent's family of attested records (T5).

    Block convention, enforced BOTH directions (mirrors ``conclusion`` /
    ``registration``): a challenge-family block
    (:data:`state.challenges.CHALLENGE_BLOCK_FAMILY`) is refused for any
    ``scope_kind`` other than ``"challenge"``; and the ``"challenge"`` scope accepts
    ONLY the block family (``challenge`` / ``challenge-verdict`` /
    ``challenge-withdraw``). Every other record passes untouched. Dispatches a
    ``challenge`` FILING to the three locks (:func:`_assert_challenge_filing_full`)
    and a ``challenge-verdict`` / ``challenge-withdraw`` to the resolution floor
    (:func:`_assert_challenge_verdict_authorship`).

    Raises :class:`errors.SpecInvalid` on any refusal (authorship-bar refusals carry
    the E2 marker so the single append firing site covers challenges over MCP too;
    shape / target / citation refusals stay unmarked).
    """
    from hpc_agent.state.challenges import (
        CHALLENGE_BLOCK,
        CHALLENGE_BLOCK_FAMILY,
    )

    block = spec.block
    in_family = block in CHALLENGE_BLOCK_FAMILY
    # A challenge-family block is challenge-scope-only.
    if in_family and spec.scope_kind != "challenge":
        raise errors.SpecInvalid(
            f"block {block!r} is a challenge-family block, only valid for "
            f"scope_kind='challenge'; got scope_kind={spec.scope_kind!r}."
        )
    if spec.scope_kind != "challenge":
        return  # not a challenge record — nothing to gate
    # The challenge scope accepts ONLY its block family.
    if not in_family:
        raise errors.SpecInvalid(
            f"scope_kind='challenge' accepts only its block family "
            f"{sorted(CHALLENGE_BLOCK_FAMILY)}; got block={block!r} — a challenge scope "
            "records ONLY challenge / challenge-verdict / challenge-withdraw (C-shape)."
        )
    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "challenge gate: resolved must be a mapping carrying the challenge fields "
            "{challenge_id, target, citations, grounds}."
        )
    if block == CHALLENGE_BLOCK:
        _assert_challenge_filing_full(experiment_dir, spec, resolved)
        return
    # block ∈ {challenge-verdict, challenge-withdraw} — the resolution floor.
    _assert_challenge_verdict_authorship(experiment_dir, spec, resolved)


# ── overnight standing-consent authorship gate (notebook-audit.md item 8) ─────


def _compose_overnight_consent(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Poka-yoke seat: compose the wake + cap defaults for a standing consent.

    The item-8 conversions (notebook-audit.md ruling, 2026-07-10) run HERE, before
    the gates, so a composed block satisfies the caps + wake assertions rather than
    tripping their refusals — the human is handed a complete, editable ``resolved``
    (with every composed field disclosed in ``composed_defaults``) instead of a
    NO-GO. A non-``overnight-consent`` record passes untouched; off a run/campaign
    scope nothing is composed (the authorship gate raises on the bad scope). Never
    composes ``cmd_sha`` — its absence still refuses at
    :func:`hpc_agent.ops.overnight.assert_consent_hard_caps` (the identity binding
    is not a default). Reached through the top-level ``hpc_agent.ops.overnight``
    role-root sibling, exactly as the authorship gate below imports it.
    """
    from hpc_agent.ops import overnight as _overnight

    if spec.block != _overnight.OVERNIGHT_CONSENT_BLOCK:
        return resolved
    if spec.scope_kind not in _overnight.CONSENT_SCOPE_KINDS:
        return resolved
    return _overnight.compose_overnight_consent(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        resolved=resolved if isinstance(resolved, dict) else {},
    )


def _assert_overnight_consent_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Overnight standing-consent gate — the human's typed acceptance of fallout.

    A STANDING CONSENT (``docs/design/notebook-audit.md`` item 8) lets named
    boundaries auto-advance while the human sleeps. It is journaled as an
    ``append-decision`` under the distinct block
    :data:`hpc_agent.ops.overnight.OVERNIGHT_CONSENT_BLOCK` (there is no consent
    verb — this gate is the only choke point), so it cannot be laundered around.
    Four legs, mirroring the ``scope-unlock`` gate's structure:

    * **block convention** — the ``overnight-consent`` block is valid only for a
      ``run`` / ``campaign`` scope (a boundary the human sleeps through), refused
      for any other ``scope_kind``.
    * **authorship** (item 8 pin a) — the ``response`` is the human's OWN typed
      utterance accepting the fallout; a bare ack (:func:`_is_bare_ack`) cannot
      grant it, and with the harness utterance log installed the utterance's word
      tokens must derive from a logged human prompt (the shared lock tier — the
      model never composes a consent). These carry the E2 authorship-missing
      marker (a fresh utterance resolves them, so an MCP retry-after-elicit fits).
    * **hard caps + spec identity** (pins b + c) —
      :func:`hpc_agent.ops.overnight.assert_consent_hard_caps`: an ``expires_at``
      morning boundary, a ``budget_cap`` / ``walltime_cap`` ceiling, and the
      ``cmd_sha`` spec-identity binding consumption dies on.
    * **the wake** (second amendment) —
      :func:`hpc_agent.ops.overnight.assert_wake_armed`: a harness-tracked
      ``status-watch`` armed for the same scope, else the consent is refused (a
      pre-y no watch can consume is theater). Caps / wake are STRUCTURAL refusals
      (a fresh utterance cannot fix a missing cap or an unarmed watch), so they
      are deliberately NOT marked with the authorship-missing marker.

    Every non-``overnight-consent`` record passes untouched. Reached through the
    top-level ``hpc_agent.ops.overnight`` module (a role-root sibling, allowed
    from inside the ``decision`` subject exactly like the ``field_ownership``
    facade import).
    """
    from hpc_agent.ops import overnight as _overnight

    if spec.block != _overnight.OVERNIGHT_CONSENT_BLOCK:
        return  # not a standing consent — nothing to gate
    if spec.scope_kind not in _overnight.CONSENT_SCOPE_KINDS:
        raise errors.SpecInvalid(
            f"block {_overnight.OVERNIGHT_CONSENT_BLOCK!r} is a standing consent, only "
            f"valid for scope_kind in {sorted(_overnight.CONSENT_SCOPE_KINDS)} (a run "
            f"or campaign boundary the human sleeps through); got "
            f"scope_kind={spec.scope_kind!r}."
        )

    # Leg 1 — authorship: the consent is the human's OWN typed utterance (pin a).
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "overnight-consent authorship gate: a standing consent accepts the "
            "fallout of unattended overnight advances and is the human's OWN typed "
            f"act — a bare {spec.response!r} (a 'y' / click) cannot grant it. The "
            "human must type the consent, naming the boundaries and the caps they "
            "accept."
        )
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    harness_texts = _actor_scoped_human_texts(experiment_dir, _actor_ids)
    if harness_texts is not None:
        human_words: set[str] = set()
        for text in harness_texts:
            human_words |= _ha_word_tokens(text)
        consent_words = _ha_word_tokens(response)
        if consent_words and not (consent_words & human_words):
            _refuse_missing_authorship(
                "overnight-consent authorship gate: with the harness utterance log "
                "installed, the consent must derive from a logged human utterance "
                f"(harness-captured), not the agent-relayed response {spec.response!r}. "
                "The model must never compose a standing consent — have the human "
                "type it in a prompt. (Under >1 declared actors the pool is the "
                "SESSION ACTOR'S log only — MH4.)"
            )

    # Legs 2 + 3 — structural (never the authorship marker): hard caps + spec
    # identity, then the armed wake.
    _overnight.assert_consent_hard_caps(resolved)
    _overnight.assert_wake_armed(
        experiment_dir,
        scope_kind=spec.scope_kind,
        scope_id=spec.scope_id,
        resolved=resolved,
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
