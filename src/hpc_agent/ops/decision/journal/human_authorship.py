"""The human-authorship gate (proving run #4) — a REQUIRED_CALLER field's value
must derive from human-attributed text, not the agent's proposal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _FREE_TEXT_CALLER_FIELDS,
    _authorship_evidence_texts,
    _collect_value_numbers,
    _collect_value_string_tokens,
    _derivation_rule,
    _ha_word_tokens,
    _human_number_pool,
    _is_bare_ack,
    _read_decisions,
    _read_interview_actors,
    _refuse_missing_authorship,
)

# ── checker-path elicited inputs (harness-contract checker obligation 3) ──────
#
# The checker chain's HUMAN-AUTHORED inputs that never transit append-decision's
# ``resolved`` (so the REQUIRED_CALLER_FIELDS first-commit trigger cannot see
# them): the external-baseline CLAIM (``verify-reproduction``) and the directed
# terminal evidence (``settle-run``; ``adopt-run``'s already-terminal branch
# settles through the same mechanism). Same partition idiom as
# ``_FREE_TEXT_CALLER_FIELDS``: a free-text member is checked by the
# word-overlap rule, a structured member by deterministic token derivation
# (numbers AND categorical string claims, finding 25).
ELICITED_FREE_TEXT_FIELDS: frozenset[str] = frozenset({"terminal_evidence"})
ELICITED_STRUCTURED_FIELDS: frozenset[str] = frozenset({"claimed_values"})
ELICITED_CHECKER_FIELDS: frozenset[str] = ELICITED_FREE_TEXT_FIELDS | ELICITED_STRUCTURED_FIELDS

# ── adoption-fact provenance (harness-contract checker obligation 1) ──────────
#
# The ONLY origins an adoption fact may claim: the HUMAN's typed words, or
# OBSERVED scheduler/filesystem state the harness actually read. Anything else
# — agent / llm / inferred / missing — is the round-tripped-guess laundering
# channel obligation 1 exists to deny (proving run #4's class, at the front
# door).
FACT_PROVENANCE_KINDS: frozenset[str] = frozenset({"human", "observed"})


def _source_log(
    number_rules: dict[str, str],
    value_numbers: dict[str, float],
    matched_strings: Any,
    log_num_pools: dict[str, tuple[set[str], set[float]]],
    log_word_pools: dict[str, set[str]],
) -> str:
    """The per-field ``source_log`` stamp (dev-mode leg c): WHICH log(s)
    contributed at least one matched claim token for this field.

    ``verbatim`` tokens credit the log(s) stating them; ``off_by_one`` credits
    the log(s) stating the anchor count; ``zero``-rule tokens derive from no
    log and contribute no source. An empty contributing set stamps ``"own"`` —
    the own namespace is always consulted and the home log is never credited
    gratis. ``"own+home"`` when both logs contributed.
    """
    logs: set[str] = set()
    for norm, rule in number_rules.items():
        if rule == "zero":
            continue
        val = value_numbers[norm]
        for log, (strings, floats) in log_num_pools.items():
            if (rule == "verbatim" and (norm in strings or val in floats)) or (
                rule == "off_by_one" and ((val + 1) in floats or (val - 1) in floats)
            ):
                logs.add(log)
    for token in matched_strings:
        for log, words in log_word_pools.items():
            if token in words:
                logs.add(log)
    if "home" not in logs:
        return "own"
    return "home" if "own" not in logs else "own+home"


def _assert_human_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> dict[str, Any] | None:
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
      structured one. This is the lock the v1 gate staged. Dev-mode
      (``docs/design/dev-mode-authorship.md`` legs b–d): under a journaled,
      human-authored ``authorship-home`` GRANT the pool widens to the UNION
      of the own namespace and the named home repo's log — same derivation
      rules, same actor scoping; the home log widens the human's STATEMENTS,
      never the derivation grammar.
    * **No utterance log** (hook not installed / older sessions —
      back-compat fail-open): this record's ``response`` plus every prior
      record's ``response`` in the scope's journal (a prior nudge that
      stated the sweep authorizes a later bare ``y``). The friction tier is
      unchanged and own-repo only: an agent-authored journal ``response``
      can never pull in another repo's log.

    Per-field rules, against the chosen human texts:

    * Structured fields (``task_generator``): every number token the value
      asserts must be human-derivable (:func:`_human_derivable` — verbatim,
      magnitude-suffixed like ``1M``, zero, or a range endpoint of a stated
      count). A consecutive-int list asserts only its endpoints and length
      (:func:`_contiguous_int_run` — "20 seeds" derives ``seeds=[0..19]``;
      proving run #5's finding was the gate demanding the enumeration). The
      off-by-one leg is scoped to RANGE-SHAPED claims (a run's
      endpoint/length, a string range literal): a standalone scalar asserts
      exactly itself, so its adjacency to a stated number never derives
      (run-15 gate finding 2 — ``n_samples=10000004`` rode the prior drill's
      stated ``10000003`` through that leg). Its non-numeric CATEGORICAL
      string claims must ALSO overlap the human word pool
      (:func:`_collect_value_string_tokens`, finding 25) — schema
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
    token(s), the evidence source consulted, the repo namespace the evidence
    was sought in (so an operator session in the wrong cwd can see why their
    utterance was not found), and the remedy (the human states the sweep in a
    prompt / their reply).

    Returns the ACCEPT-SIDE disclosure (docket #1 part 2 — "which rule fired"
    must be answerable from the journal, not just the refuse side): ``None``
    when the gate did not evaluate a first-commit field, else a mapping with
    the ``evidence_source`` tier (``harness_captured`` / ``journal_response``),
    the ``evidence_logs`` list (every namespace CONSULTED — own always, the
    granted home when one exists; dev-mode leg c) and, per gated field, the
    matched tokens with the derivation rule that accepted each
    (:func:`_derivation_rule` — ``verbatim`` / ``zero`` / ``off_by_one``) or
    the free-text rule that committed it, plus the per-field ``source_log``
    (``own`` / ``home`` / ``own+home`` — WHICH log(s) contributed a matched
    claim token). A dangling grant is disclosed (``dangling_home``) on accepts
    that still pass, never silently. The caller journals it under a code-owned
    provenance key; gate SEMANTICS are unchanged (this is additive disclosure,
    never a tightening).
    """
    if not isinstance(resolved, dict) or not resolved:
        return None

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
        return None

    prior = _read_decisions(experiment_dir, spec.scope_kind, spec.scope_id)

    from hpc_agent.state.run_record import repo_hash as _repo_hash

    # Tiered evidence source: prefer the harness-captured utterance log (the
    # lock) over agent-authored journal responses (the friction fallback). Under
    # >1 declared actors the pool is the SESSION ACTOR'S log only (MH4 — actor A's
    # agent cannot commit a value only actor B ever typed); an unattributed
    # >1-actor session falls to the friction tier (never the anonymous union).
    # Dev-mode (legs b–d): the shared reader owns the own-namespace read AND the
    # cross-repo widening under a journaled authorship-home grant; ``None`` →
    # the friction tier, byte-identical to pre-ruling (cross-repo reading never
    # applies there).
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    evidence = _authorship_evidence_texts(experiment_dir, _actor_ids)
    harness_captured = evidence is not None

    first_commits = [
        f
        for f in candidates
        if not any(isinstance(rec.get("resolved"), dict) and f in rec["resolved"] for rec in prior)
    ]
    if not first_commits:
        return None

    if not harness_captured and prior and not any("response" in rec for rec in prior):
        # Fail-open (journal-response mode only): an old-schema journal with
        # no response text at all — there is no human record to derive from
        # OR to contradict. With an utterance log the stronger source exists,
        # so this escape hatch never applies. Disclosed as such: a silent
        # fail-open is exactly the unanswerable commit docket #1 part 2 names.
        return {
            "evidence_source": "journal_response",
            "evidence_logs": [_repo_hash(experiment_dir)],
            "fail_open": "old_schema_journal_no_response_text",
            "fields": {},
        }

    if evidence is not None:
        # The lock: only text the HARNESS recorded counts as human. The
        # spec's ``response`` (and prior responses) are agent-relayed and
        # carry no authorship weight — exactly the laundering channel the
        # v1 gate could not close. The pool is the UNION of the own namespace
        # and a validly-granted home namespace (leg b); per-log membership is
        # kept for the source_log stamp (leg c).
        own_texts = list(evidence["own"] or [])
        home_texts = list(evidence["home"])
        human_texts = own_texts + home_texts
        response_commits = False
        if home_texts:
            source_desc = (
                "logged human utterance for this repo or its granted home repo (harness-captured)"
            )
        else:
            source_desc = "logged human utterance for this repo (harness-captured)"
        remedy = "the human states it in a prompt (captured to the utterance log)"
    else:
        own_texts = []
        home_texts = []
        human_texts = [str(spec.response or "")]
        human_texts.extend(str(rec.get("response") or "") for rec in prior)
        response_commits = not _is_bare_ack(str(spec.response or ""))
        source_desc = "human response in this scope's journal"
        remedy = "the human restates it in their reply"

    human_num_strings, human_num_floats = _human_number_pool(human_texts)
    human_words: set[str] = set()
    for text in human_texts:
        human_words |= _ha_word_tokens(text)

    # Leg (c): per-log membership of the union pool, so each accepted field
    # stamps WHICH log(s) contributed a matched claim token. The friction tier
    # never consults the home log (own-repo only by design) — its pool is the
    # own repo's journal responses, stamped "own".
    if evidence is not None:
        log_num_pools = {
            "own": _human_number_pool(own_texts),
            "home": _human_number_pool(home_texts),
        }
        log_word_pools = {
            "own": {w for t in own_texts for w in _ha_word_tokens(t)},
            "home": {w for t in home_texts for w in _ha_word_tokens(t)},
        }
    else:
        log_num_pools = {"own": (human_num_strings, human_num_floats), "home": (set(), set())}
        log_word_pools = {"own": set(human_words), "home": set()}

    disclosure_fields: dict[str, Any] = {}
    problems: list[str] = []
    for field in first_commits:
        value = resolved[field]
        if field not in _FREE_TEXT_CALLER_FIELDS:
            value_numbers: dict[str, float] = {}
            range_eligible: set[str] = set()
            _collect_value_numbers(value, value_numbers, range_eligible)
            # Finding 25: the number check alone let a fabricated CATEGORICAL
            # param (a dataset name the human never uttered) pass whenever the
            # numbers derived. Hold the value's non-numeric claim tokens to the
            # same human-derivability bar — schema vocabulary (dict keys, the
            # ``kind`` discriminator value) is excluded by the collector.
            value_strings: set[str] = set()
            _collect_value_string_tokens(value, value_strings)
            if value_numbers or value_strings:
                number_rules = {
                    norm: _derivation_rule(
                        val,
                        norm,
                        human_num_strings,
                        human_num_floats,
                        # Run-15 (gate finding 2): the off-by-one leg matched a
                        # standalone ``n_samples=10000004`` against the adjacent
                        # stated ``10000003`` — a coincidence adjacency, not a
                        # range endpoint. Only range-shaped claims (a contiguous
                        # run's endpoint/length, a string range literal) may
                        # derive by adjacency; a bare scalar asserts itself.
                        off_by_one_eligible=norm in range_eligible,
                    )
                    for norm, val in value_numbers.items()
                }
                missing = sorted(norm for norm, rule in number_rules.items() if rule is None)
                missing += sorted(value_strings - human_words)
                if missing:
                    problems.append(
                        f"{field} is human-authored: {spec.response!r} cannot "
                        "commit a value that appears only in the agent's proposal — "
                        f"ask the human for the sweep (or {remedy}); value "
                        f"token(s) {missing} derive from no {source_desc}"
                    )
                else:
                    # Accept-side disclosure: which derivation rule accepted each
                    # token (the matched set is the whole claim set — anything
                    # missing refused above), and WHICH log(s) contributed (leg c).
                    matched_strings = value_strings & human_words
                    accepted_rules = {
                        norm: rule
                        for norm, rule in sorted(number_rules.items())
                        if rule is not None
                    }
                    disclosure_fields[field] = {
                        "numbers": accepted_rules,
                        "strings": sorted(matched_strings),
                        "source_log": _source_log(
                            accepted_rules,
                            value_numbers,
                            matched_strings,
                            log_num_pools,
                            log_word_pools,
                        ),
                    }
                continue
            # No number OR string claims — fall through to the free-text rule below.
        if response_commits:
            # journal-response mode: a substantive human reply commits it
            disclosure_fields[field] = {"rule": "response_commit", "source_log": "own"}
            continue
        overlap_text = value if isinstance(value, str) else json.dumps(value, default=str)
        overlap = _ha_word_tokens(overlap_text) & human_words
        if overlap:
            # the human's own words state it (per the evidence source)
            disclosure_fields[field] = {
                "rule": "word_overlap",
                "matched_words": sorted(overlap),
                "source_log": _source_log({}, {}, overlap, log_num_pools, log_word_pools),
            }
            continue
        problems.append(
            f"{field} is human-authored: {spec.response!r} cannot commit a "
            "value that appears only in the agent's proposal — ask the human to "
            f"state the {field} (or {remedy}); the value derives from no "
            f"{source_desc}"
        )

    if problems:
        # Name the repo namespace the gate consulted (docket #2): an operator
        # session in the wrong cwd otherwise cannot tell WHY their utterance was
        # not found — the refusal named the tokens but not the namespace.
        # Dev-mode: the consultation (granted home), or the reason it did NOT
        # happen (dangling grant / mid-session revocation), is disclosed the
        # same way — never a silent own-only fallback.
        tail = _refusal_namespace_tail(experiment_dir, evidence)
        _refuse_missing_authorship(
            "human-authorship gate (conduct rule 9): " + "; ".join(problems) + tail
        )
    disclosure: dict[str, Any] = {
        "evidence_source": "harness_captured" if harness_captured else "journal_response",
        # Leg (c): every namespace CONSULTED (own always; home when a valid
        # grant exists) — "which logs were searched" is answerable from the
        # journal alone.
        "evidence_logs": (
            list(evidence["evidence_logs"])
            if evidence is not None
            else [_repo_hash(experiment_dir)]
        ),
        "fields": disclosure_fields,
    }
    if evidence is not None and evidence["dangling_home"]:
        # A dangling grant is disclosed on accepts that still pass, too (never
        # a silent own-only fallback).
        disclosure["dangling_home"] = evidence["dangling_home"]
        disclosure["dangling_reason"] = evidence["dangling_reason"]
    return disclosure


def _refusal_namespace_tail(experiment_dir: Path, evidence: dict[str, Any] | None) -> str:
    """The refusal tail naming the repo namespace(s) the evidence was sought in.

    Extracted VERBATIM from :func:`_assert_human_authorship`'s refuse side
    (docket #2 — an operator session in the wrong cwd must see WHY their
    utterance was not found) so the checker-path gates below refuse with the
    same namespace disclosure: the own namespace always; the granted home
    namespace when consulted; a dangling grant or a mid-session revocation
    disclosed the same way — never a silent own-only fallback.
    """
    from hpc_agent.state.run_record import repo_hash as _repo_hash

    own_hash = _repo_hash(experiment_dir)
    tail = f" — evidence was sought in repo namespace {own_hash} (experiment_dir {experiment_dir})"
    if evidence is not None:
        consulted = evidence["evidence_logs"]
        if len(consulted) > 1:
            tail += f" and granted home namespace {consulted[1]}"
        elif evidence["dangling_home"]:
            tail = (
                f" — home-log trust for home namespace {evidence['dangling_home']} "
                f"is dangling ({evidence['dangling_reason']}); evidence was sought "
                f"in repo namespace {own_hash} (experiment_dir {experiment_dir}) only"
            )
        elif evidence["revoked"]:
            tail = (
                f" — home-log trust revoked at {evidence['revoked']['ts']} "
                f"(home namespace {evidence['revoked']['home_repo_hash']}); evidence "
                f"was sought in repo namespace {own_hash} "
                f"(experiment_dir {experiment_dir}) only"
            )
    return tail


def _assert_value_derivable_from_evidence(
    experiment_dir: Path,
    *,
    field: str,
    value: Any,
    free_text: bool,
    evidence: dict[str, Any],
    obligation: str,
) -> dict[str, Any]:
    """The harness-captured LOCK leg the checker-path gates share.

    The per-field derivation rules of :func:`_assert_human_authorship`, applied
    to a value that arrives at a verb's INTAKE rather than through
    append-decision's ``resolved``: a structured value's number tokens must be
    human-derivable (:func:`_derivation_rule`) and its categorical string
    claims must overlap the human word pool (finding 25); a free-text value's
    word tokens must overlap some human text. The evidence pool is the one
    *evidence* mapping :func:`_authorship_evidence_texts` returned (own
    namespace + granted home — the dev-mode legs ride along unchanged). There
    is NO journal-response commit leg here: at intake there is no human reply
    on the spec, so the only lock evidence is the harness-captured log — the
    caller handles the log-absent tier.

    Returns the accept-side disclosure; refuses via
    :func:`_refuse_missing_authorship` (the authorship-missing marker — a
    freshly typed human statement resolves it), naming the field, the
    underivable token(s), and the namespace(s) consulted.
    """
    own_texts = list(evidence["own"] or [])
    home_texts = list(evidence["home"])
    human_texts = own_texts + home_texts
    if home_texts:
        source_desc = (
            "logged human utterance for this repo or its granted home repo (harness-captured)"
        )
    else:
        source_desc = "logged human utterance for this repo (harness-captured)"
    remedy = "the human states it in a prompt (captured to the utterance log)"

    human_num_strings, human_num_floats = _human_number_pool(human_texts)
    human_words: set[str] = set()
    for text in human_texts:
        human_words |= _ha_word_tokens(text)
    log_num_pools = {
        "own": _human_number_pool(own_texts),
        "home": _human_number_pool(home_texts),
    }
    log_word_pools = {
        "own": {w for t in own_texts for w in _ha_word_tokens(t)},
        "home": {w for t in home_texts for w in _ha_word_tokens(t)},
    }

    disclosure: dict[str, Any] = {
        "field": field,
        "evidence_source": "harness_captured",
        "evidence_logs": list(evidence["evidence_logs"]),
    }
    if evidence["dangling_home"]:
        disclosure["dangling_home"] = evidence["dangling_home"]
        disclosure["dangling_reason"] = evidence["dangling_reason"]

    if not free_text:
        value_numbers: dict[str, float] = {}
        range_eligible: set[str] = set()
        _collect_value_numbers(value, value_numbers, range_eligible)
        value_strings: set[str] = set()
        _collect_value_string_tokens(value, value_strings)
        if value_numbers or value_strings:
            number_rules = {
                norm: _derivation_rule(
                    val,
                    norm,
                    human_num_strings,
                    human_num_floats,
                    off_by_one_eligible=norm in range_eligible,
                )
                for norm, val in value_numbers.items()
            }
            missing = sorted(norm for norm, rule in number_rules.items() if rule is None)
            missing += sorted(value_strings - human_words)
            if missing:
                _refuse_missing_authorship(
                    f"human-authorship gate ({obligation}): {field} is "
                    "human-authored — the harness cannot commit its own "
                    "characterization as the human's input; ask the human to "
                    f"state it (or {remedy}); value token(s) {missing} derive "
                    f"from no {source_desc}" + _refusal_namespace_tail(experiment_dir, evidence)
                )
            matched_strings = value_strings & human_words
            accepted_rules = {
                norm: rule for norm, rule in sorted(number_rules.items()) if rule is not None
            }
            disclosure["numbers"] = accepted_rules
            disclosure["strings"] = sorted(matched_strings)
            disclosure["source_log"] = _source_log(
                accepted_rules,
                value_numbers,
                matched_strings,
                log_num_pools,
                log_word_pools,
            )
            return disclosure
        # No number OR string claims — fall through to the free-text rule.
    overlap_text = value if isinstance(value, str) else json.dumps(value, default=str)
    overlap = _ha_word_tokens(overlap_text) & human_words
    if not overlap:
        _refuse_missing_authorship(
            f"human-authorship gate ({obligation}): {field} is human-authored — "
            "the harness cannot commit its own characterization as the human's "
            f"input; ask the human to state the {field} (or {remedy}); the "
            f"value derives from no {source_desc}"
            + _refusal_namespace_tail(experiment_dir, evidence)
        )
    disclosure["rule"] = "word_overlap"
    disclosure["matched_words"] = sorted(overlap)
    disclosure["source_log"] = _source_log({}, {}, overlap, log_num_pools, log_word_pools)
    return disclosure


def assert_elicited_value_human_authored(
    experiment_dir: Path, *, field: str, value: Any
) -> dict[str, Any]:
    """Checker obligation 3 gate: an ELICITED checker input is human-authored.

    The field-set extension the harness-contract's checker-path audit owed
    (obligation 3, ``docs/internals/harness-contract.md``): ``claimed_values``
    (verify-reproduction external-baseline intake) and ``terminal_evidence``
    (settle-run intake — and adopt-run's already-terminal branch, which
    settles through the same mechanism) are HUMAN-AUTHORED inputs; a harness
    relaying its own characterization as the claim — or as the terminal
    evidence — voids the check (the LLM-audits-LLM inversion). The mechanism
    is the SAME one :func:`_assert_human_authorship` applies to
    ``REQUIRED_CALLER_FIELDS``, reached per-verb at intake because these
    inputs never transit append-decision's ``resolved``:

    * **Harness-captured tier (the lock)** — the utterance log exists
      (capability 1; :func:`_authorship_evidence_texts`, dev-mode home
      widening and MH4 actor scoping included): the value must derive from
      LOGGED human text under the same rules — every number token
      human-derivable, every categorical/free-text token in the human word
      pool. Refuses via :func:`_refuse_missing_authorship` otherwise.
    * **No utterance log** — DISCLOSED ``unverified_fallback``, never a
      refusal (the ``settle-aggregate`` posture: refusing every pre-hook
      install would break back-compat, and at intake there is no
      journal-response channel — the honest tier note of
      :func:`_assert_human_authorship` applies verbatim).

    Returns the disclosure (tier + matched rules) for the caller to journal or
    drop; an EMPTY value is not a commit and is never gated (the callers'
    own required-field guards refuse emptiness first).
    """
    if field not in ELICITED_CHECKER_FIELDS:
        raise ValueError(
            f"assert_elicited_value_human_authored: {field!r} is not an "
            f"ELICITED_CHECKER_FIELDS member ({sorted(ELICITED_CHECKER_FIELDS)}) — "
            "extend the field set deliberately, never ad hoc."
        )
    if value in (None, "", {}, []):
        return {"field": field, "evidence_source": "empty_value_not_gated"}
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    evidence = _authorship_evidence_texts(experiment_dir, _actor_ids)
    if evidence is None:
        from hpc_agent.state.run_record import repo_hash as _repo_hash

        return {
            "field": field,
            "evidence_source": "unverified_fallback",
            "evidence_logs": [_repo_hash(experiment_dir)],
        }
    return _assert_value_derivable_from_evidence(
        experiment_dir,
        field=field,
        value=value,
        free_text=field in ELICITED_FREE_TEXT_FIELDS,
        evidence=evidence,
        obligation="checker obligation 3",
    )


def assert_adoption_fact_provenance(
    experiment_dir: Path, *, facts: dict[str, Any], provenance: dict[str, Any]
) -> dict[str, Any]:
    """Checker obligation 1 gate: every adoption fact carries typed provenance.

    The provenance check at adoption intake the harness-contract audit owed
    (obligation 1): adoption facts — run_id, the command, the cluster
    placement facts, ``job_ids``, the result layout — MUST come from the HUMAN
    or from OBSERVED scheduler/filesystem state; the harness must never invent
    one or round-trip its own prior guess. ``adopt-run``'s intake calls this
    with its facts and a caller-supplied per-fact annotation mapping::

        {fact_name: {"kind": "human" | "observed", "via": <source>}}

    Per gated fact (empty values are not commits and are never gated):

    * **missing / malformed annotation** — refused (an unattributed adoption
      fact IS the laundering channel; the authorship-missing marker rides the
      refusal because a typed human statement resolves it).
    * **kind outside** :data:`FACT_PROVENANCE_KINDS` (``agent`` / ``llm`` /
      ``inferred`` / anything else) — refused by vocabulary: only the human's
      words or observed state are accepted origins.
    * **``observed``** — ``via`` must name WHAT was read (a scheduler query, a
      directory listing, a receipt path); an observation with no record of the
      observation is a characterization, and is refused. Core never verifies
      the read happened (the harness-asserted trust tier, same limit as the
      utterance log's actor attribution) — it requires the assertion to be
      TYPED and on the record.
    * **``human``** — the fact's value faces the SAME tiered derivation bar as
      the elicited checker inputs: the harness-captured lock when the
      utterance log exists, a DISCLOSED ``unverified_fallback`` when it does
      not.

    Returns the per-fact disclosure mapping for the caller to journal.
    Structural misuse (a non-dict ``provenance``) raises a plain
    :class:`errors.SpecInvalid` — no typed utterance can fix a malformed call.
    """
    if not isinstance(provenance, dict):
        raise errors.SpecInvalid(
            "fact-provenance gate (checker obligation 1): provenance must be a "
            "mapping of {fact_name: {kind, via}} — got "
            f"{type(provenance).__name__}."
        )
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    evidence = _authorship_evidence_texts(experiment_dir, _actor_ids)

    disclosure_facts: dict[str, Any] = {}
    for name in sorted(facts):
        value = facts[name]
        if value in (None, "", {}, []):
            continue  # an empty value is not a commit and is never gated
        entry = provenance.get(name)
        if not isinstance(entry, dict):
            _refuse_missing_authorship(
                f"fact-provenance gate (checker obligation 1): adoption fact "
                f"{name!r} is unattributed — every adoption fact must carry a "
                "typed provenance annotation {kind: human|observed, via: <source>}. "
                "The facts come from the HUMAN or from OBSERVED "
                "scheduler/filesystem state; the harness must never invent one "
                "or round-trip its own prior guess as a fact."
            )
        kind = str(entry.get("kind") or "")
        if kind not in FACT_PROVENANCE_KINDS:
            _refuse_missing_authorship(
                f"fact-provenance gate (checker obligation 1): adoption fact "
                f"{name!r} claims provenance kind {kind!r} — only "
                f"{sorted(FACT_PROVENANCE_KINDS)} are accepted origins. An "
                "agent-attributed or invented fact is the laundering channel "
                "this gate exists to deny; ask the human to state the fact, or "
                "read it from the scheduler/filesystem and attribute it "
                "'observed' with the source named in 'via'."
            )
        via = str(entry.get("via") or "").strip()
        if kind == "observed":
            if not via:
                _refuse_missing_authorship(
                    f"fact-provenance gate (checker obligation 1): adoption fact "
                    f"{name!r} is attributed 'observed' with no 'via' — an "
                    "observation must name WHAT was read (a scheduler query, a "
                    "directory listing, a receipt path). An observation with no "
                    "record of the observation is a characterization, not "
                    "observed state."
                )
            disclosure_facts[name] = {"kind": "observed", "via": via}
            continue
        # kind == "human": the same tiered derivation bar as the elicited inputs.
        if evidence is None:
            disclosure_facts[name] = {"kind": "human", "evidence_source": "unverified_fallback"}
            continue
        # Structured treatment even for strings: a human-stated command / path
        # asserts its embedded number tokens AND its word tokens (the
        # "every number token human-derivable" bar); a value with no claims
        # falls back to the word-overlap rule inside the shared leg.
        checked = _assert_value_derivable_from_evidence(
            experiment_dir,
            field=name,
            value=value,
            free_text=False,
            evidence=evidence,
            obligation="checker obligation 1",
        )
        disclosure_facts[name] = {"kind": "human", **checked}

    from hpc_agent.state.run_record import repo_hash as _repo_hash

    return {
        "evidence_source": ("harness_captured" if evidence is not None else "unverified_fallback"),
        "evidence_logs": (
            list(evidence["evidence_logs"])
            if evidence is not None
            else [_repo_hash(experiment_dir)]
        ),
        "facts": disclosure_facts,
    }
