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
from hpc_agent.state.decision_journal import append_decision as _append_decision
from hpc_agent.state.decision_journal import decisions_path as _decisions_path
from hpc_agent.state.decision_journal import read_decisions as _read_decisions
from hpc_agent.state.registration import (
    REGISTRATION_BLOCK_FAMILY,
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
    _assert_no_code_derived_fields(resolved)
    _assert_brief_provenance(experiment_dir, spec, resolved)
    _assert_human_authorship(experiment_dir, spec, resolved)
    _assert_unlock_authorship(experiment_dir, spec, resolved)
    _assert_signoff_authorship(experiment_dir, spec, resolved)
    _assert_registration_authorship(experiment_dir, spec, resolved)
    _assert_reproduction_verdict_authorship(experiment_dir, spec, resolved)
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


def _harness_human_texts(experiment_dir: Path) -> list[str] | None:
    """The logged human utterances' texts, or ``None`` when none were captured.

    The harness-captured evidence tier BOTH authorship gates share
    (:func:`_assert_human_authorship`, :func:`_assert_unlock_authorship`): the
    ``UserPromptSubmit`` capture hook (:mod:`hpc_agent._kernel.hooks.utterance_capture`)
    writes each human prompt to :func:`hpc_agent.state.utterances.read_utterances`
    out-of-band, so this is text a human verifiably typed — not the
    agent-authored journal ``response``. ``None`` (no log / older session)
    signals the caller to fall back to the journal-response friction tier;
    a present-but-empty case reads the same.
    """
    from hpc_agent.state.utterances import read_utterances

    utterances = read_utterances(experiment_dir)
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
    # lock) over agent-authored journal responses (the friction fallback).
    harness_texts = _harness_human_texts(experiment_dir)
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

    harness_texts = _harness_human_texts(experiment_dir)
    if harness_texts is not None:
        human_words: set[str] = set()
        for text in harness_texts:
            human_words |= _ha_word_tokens(text)
        rationale_words = _ha_word_tokens(response)
        if rationale_words and not (rationale_words & human_words):
            _refuse_missing_authorship(
                "scope-unlock authorship gate: with the harness utterance log "
                "installed, the unlock rationale must derive from a logged human "
                "utterance (harness-captured), not the agent-relayed response "
                f"{spec.response!r}. Have the human state why the scope is being "
                "re-opened in a prompt."
            )


# ── notebook sign-off authorship gate (D5 three locks + D-attention, T8) ──────

# The block-terminator convention for a notebook section SIGN-OFF. A sign-off
# ATTESTS that a human reviewed a section AT A SPECIFIC HASH; it is a HUMAN
# attestation over the ``notebook`` scope, journaled under this distinct block so
# the gate can recognise — and lock — it (mirrors the ``scope-unlock`` block
# convention). Lock 1 (no affordance) is organizational: there is NO sign-off
# verb, chain, or next_block — append-decision under this block is the ONLY write
# path (pinned by the contract test in tests/contract/).
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
    if not isinstance(src_rel, str) or not src_rel:
        raise errors.SpecInvalid(
            "notebook sign-off gate: could not resolve the audited .py source for "
            f"audit_id={audit_id!r} — no resolved['source'] and no matching "
            "interview.json audited_source block. A sign-off must recompute the "
            "section hash from the source on disk; an unresolvable source is refused."
        )
    if not isinstance(tmpl_rel, str) or not tmpl_rel:
        raise errors.SpecInvalid(
            "notebook sign-off gate: could not resolve the audited .py TEMPLATE for "
            f"audit_id={audit_id!r} — no resolved['template'] and no matching "
            "interview.json audited_source block. The full-view recompute rebuilds "
            "the canonical view_sha (a diff-from-template projection), so the "
            "template is a required ingredient; an unresolvable template is refused."
        )
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
    by the contract test in ``tests/contract/`` (no primitive is named sign-off).

    **Lock 2 (recompute, un-fakeable)** — the audited ``.py`` is resolved (from
    ``resolved['source']`` or the interview.json ``audited_source`` block), parsed
    (:func:`parse_percent_source`), the named section located, and its
    ``section_sha`` RECOMPUTED. The record binds through the ONE attestation kernel
    (``state.attestation.bind``, D5 lock 2 extracted once): the asserted
    ``section_sha`` must equal the recomputed one or the append is refused — a hash
    cannot be asserted into existence. An unresolvable source / missing section is
    REFUSED loudly, never skipped.

    **Lock 3 (authorship bar, D-attention tiered)** — bare acks are refused
    (:func:`_is_bare_ack`); the response must NAME the section slug (token-exact,
    the #26 precedent). The tier is RECOMPUTED here over the CANONICAL view
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

    # Base authorship floor (applies to every tier).
    response = str(spec.response or "")
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
    engaged = (_signoff_token_names(response) - slug_tokens) & specifics
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
    """
    harness_texts = _harness_human_texts(experiment_dir)
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
    authored = _registration_authored_text(experiment_dir, response)
    if not _names_slug(authored, registration_id):
        _refuse_missing_authorship(
            "registration-revoke gate: the revoke must NAME the registration_id "
            f"{registration_id!r} token-exact (the #26 floor). Restate, naming the "
            "registration being overturned."
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
    block family (``registration`` / ``registration-revoke``). Every other record
    passes untouched. Dispatches a ``registration-revoke`` to the revoke floor (R7)
    and a ``registration`` to the full three locks (R6).

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
    # block == REGISTRATION_BLOCK — the maximal human ceremony (R6 three locks).
    _assert_registration_full(experiment_dir, spec, resolved)


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
