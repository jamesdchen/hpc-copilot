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
    _assert_no_code_derived_fields(resolved)
    _assert_brief_provenance(experiment_dir, spec, resolved)
    _assert_human_authorship(experiment_dir, spec, resolved)
    _assert_unlock_authorship(experiment_dir, spec, resolved)
    _assert_signoff_authorship(experiment_dir, spec, resolved)
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
        raise errors.SpecInvalid("human-authorship gate (conduct rule 9): " + "; ".join(problems))


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
        raise errors.SpecInvalid(
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
            raise errors.SpecInvalid(
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
    unified-diff ``+++``/``---`` file headers and ``@@`` hunk markers) — the human
    must engage a SPECIFIC of the change, not offer generic praise. Falls back to
    the section's declared ASSERTION identifiers when the diff is empty (a
    human-required-but-inherited section whose assertions are ungreen has no diff
    tokens); when BOTH are empty the bar reduces to the slug-naming floor already
    enforced (a token that does not exist cannot be demanded).
    """
    tokens: set[str] = set()
    for line in section_view.diff:
        if not line or line.startswith(("+++", "---", "@@")):
            continue
        if line[0] in "+-":
            tokens |= {m.group(0).lower() for m in _SIGNOFF_IDENT_RE.finditer(line[1:])}
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


def _resolve_signoff_sources(
    experiment_dir: Path, resolved: dict[str, Any]
) -> tuple[str, str | None]:
    """Resolve ``(source_text, template_text_or_None)`` for a sign-off record.

    Precedence: an explicit ``resolved["source"]`` / ``resolved["template"]``
    campaign-dir-relative path wins; otherwise the interview.json
    ``audited_source`` block (matched by ``audit_id``) supplies them. The SOURCE
    must resolve — an unresolvable source is REFUSED loudly (this differs from
    D7's absent-opt-in silence: an explicit notebook sign-off record is already
    inside the opted-in surface, so it is recomputed or refused, never passed).
    The TEMPLATE is optional here; ``None`` drives the conservative
    human-required tier in the caller.
    """
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
    source_text = _read_signoff_source_text(experiment_dir, src_rel, required=True)
    assert source_text is not None  # required=True raises rather than returning None
    template_text = (
        _read_signoff_source_text(experiment_dir, tmpl_rel, required=False)
        if isinstance(tmpl_rel, str) and tmpl_rel
        else None
    )
    return source_text, template_text


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
    the #26 precedent). The tier is RECOMPUTED here (``build_audit_view`` over
    source + template, ``lint_findings=()``). What the gate can honestly check at
    append time is the STATICALLY-recomputable tier legs — the diff classification
    (inherited/added/modified) and assertions-without-a-receipt; it does NOT have
    the T4 lint findings (a concurrent surface), so a section made human-required
    *solely* by a lint flag is not distinguished here. For a **HUMAN_REQUIRED**
    section the bar RAISES: the response must additionally ENGAGE the change —
    contain at least one identifier drawn from the section's diff-changed lines
    (:func:`_section_specific_tokens`). This is the boundary-drift defense: soften
    the human-required tier only via a richer harness-captured utterance, never a
    bare ack.

    **AUTO_CLEARED + a human sign-off: ACCEPT, but mark ``resolved['redundant'] =
    True``.** The alternative (refuse) was rejected: refusing a human's VOLUNTARY
    review would delete information and create a verb-shaped affordance gap
    (a human who looked would have no way to record it). Marking keeps the
    attention ledger honest — the record shows a real human sign-off that the
    tiering deemed unnecessary. The recompute lock and the base authorship floor
    (non-bare, slug-named) still apply to a redundant sign-off; only the raised
    diff-token bar is waived (an auto-cleared section has no change to engage).

    **TEMPLATE absent → HUMAN_REQUIRED (conservative, never auto-soften).** When no
    template resolves the tier cannot be honestly recomputed as auto-cleared, so an
    empty template is used: every section then classifies as ``added`` →
    human-required, and the diff-token pool is the whole section (the safe floor).

    **view_sha is provenance, not recomputed here** — it is validated present and
    non-empty (it binds what-the-human-saw into the record, D5), but the gate does
    NOT recompute it: the view depends on lint findings the gate does not have. The
    recompute lock is ``section_sha``; ``view_sha`` is a provenance witness.

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
        raise errors.SpecInvalid(
            "notebook sign-off gate: signing off a section is a HUMAN act — a bare "
            f"{spec.response!r} (a 'y' / click) cannot sign off. Name the section "
            f"({section!r}) and state what you reviewed."
        )
    if not _names_slug(response, section):
        raise errors.SpecInvalid(
            "notebook sign-off gate: the sign-off response must NAME the section "
            f"slug {section!r} (token-exact, the #26 precedent) — a generic ack "
            "cannot attest a specific section. Restate, naming the section."
        )

    # Lazy, subject-lint-safe imports (state.* is allowed substrate; the ops
    # notebook subject is reached through the top-level ``notebook_view`` facade).
    from hpc_agent.ops import notebook_view as _notebook_view
    from hpc_agent.state import attestation
    from hpc_agent.state.audit_source import parse_percent_source

    # Lock 2 — recompute the section hash from the .py on disk and bind through
    # the ONE attestation kernel (D5 lock 2). Refuses an unresolvable source.
    source_text, template_text = _resolve_signoff_sources(experiment_dir, resolved)
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

    # Lock 3 (tiered) — recompute the tier over the STATICALLY-available legs
    # (diff classification + assertions-without-receipt). No template resolves →
    # empty template → every section reads ``added`` → HUMAN_REQUIRED (never
    # auto-softened). lint_findings=() because the T4 lint is a concurrent surface
    # the gate does not have; a section human-required SOLELY by a lint flag is
    # therefore not distinguished at append time — the honest boundary.
    template_parsed = parse_percent_source(template_text if template_text is not None else "")
    view = _notebook_view.build_audit_view(parsed, template_parsed, ())
    section_view = next((v for v in view.sections if v.slug == section), None)
    tier = section_view.tier if section_view is not None else _notebook_view.HUMAN_REQUIRED

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
        raise errors.SpecInvalid(
            f"notebook sign-off gate: section {section!r} is HUMAN-REQUIRED "
            "(nonempty diff-from-template / lint flags / ungreen assertions), so the "
            "sign-off must ENGAGE the change — name at least one identifier from the "
            "section's diff, not offer a generic ack (soften only via a richer "
            "utterance, never a bare ack; the boundary-drift flag). Identifiers in "
            f"the change include: {sorted(specifics)[:8]}."
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
