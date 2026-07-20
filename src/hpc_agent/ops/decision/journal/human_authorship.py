"""The human-authorship gate (proving run #4) — a REQUIRED_CALLER field's value
must derive from human-attributed text, not the agent's proposal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        own_hash = _repo_hash(experiment_dir)
        tail = (
            f" — evidence was sought in repo namespace {own_hash} (experiment_dir {experiment_dir})"
        )
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
