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

Two trust-seam gates run before an append is persisted: the rule-9
brief-provenance gate (:func:`_assert_brief_provenance`) and the
human-authorship gate (:func:`_assert_human_authorship`, proving run #4).
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

import json
import re
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
    _assert_human_authorship(experiment_dir, spec, resolved)
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


# ── human-authorship gate (conduct rule 9 extension, proving run #4) ──────────

# REQUIRED_CALLER fields whose value is free-text intent (no structured tokens
# to extract) — checked with the softer non-bare-response / word-overlap rule.
# Everything else in REQUIRED_CALLER_FIELDS (today: task_generator) is checked
# by deterministic token derivation.
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
      proving run #5's finding was the gate demanding the enumeration).
      A value with no numbers falls back to the free-text rule.
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
    from hpc_agent.state.utterances import read_utterances

    utterances = read_utterances(experiment_dir)
    harness_captured = bool(utterances)

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

    if harness_captured:
        # The lock: only text the HARNESS recorded counts as human. The
        # spec's ``response`` (and prior responses) are agent-relayed and
        # carry no authorship weight — exactly the laundering channel the
        # v1 gate could not close.
        human_texts = [str(u.get("text") or "") for u in utterances]
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
            if value_numbers:
                missing = sorted(
                    norm
                    for norm, val in value_numbers.items()
                    if not _human_derivable(val, norm, human_num_strings, human_num_floats)
                )
                if missing:
                    problems.append(
                        f"{field} is human-authored: {spec.response!r} cannot "
                        "commit a value that appears only in the agent's proposal — "
                        f"ask the human for the sweep (or {remedy}); value "
                        f"token(s) {missing} derive from no {source_desc}"
                    )
                continue
            # No number tokens — fall through to the free-text rule below.
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
        raise errors.SpecInvalid("human-authorship gate (conduct rule 9): " + "; ".join(problems))


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
