"""Shared substrate for the decision-journal authorship gates.

The ONE internal submodule the gate modules build on: the authorship-refusal
marker, the multi-human actor substrate, the B4 ``ts >= anchor`` freshness
filters (``_fresh_human_texts`` / ``_newest_lock_ts`` / ``_target_record_ts``),
the bare-ack + number/word token helpers, and the sha-prefix naming helpers
reused across more than one gate. Every gate submodule imports from here; this
module imports only ``infra`` / ``state`` substrate, so it never cycles back."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn

from hpc_agent import errors
from hpc_agent.infra.env_flags import env_actor
from hpc_agent.state.decision_journal import read_decisions as _read_decisions
from hpc_agent.state.interview_doc import iter_interview_docs

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
    for doc in iter_interview_docs(experiment_dir):
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
# by vocabulary impossibility). The overnight-consent gate RETIRED its unbounded
# reader entirely (USER RULING 3, 2026-07-12): it reads only BOUND consent records
# (:func:`_bound_consent_records`) captured at a surface that named exactly what
# they cover, so the chat pool it once word-overlapped is never consulted — the
# same vocabulary-impossibility class as the sha-prefix gates (a chat hook cannot
# forge a ``bound`` binding), documented in the route-through exemption.


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


# Identifier-shaped hex runs of length >= 8 — the sha-prefix pool for R6 lock 3.
# An 8-hex prefix exists NOWHERE in a human's prior vocabulary and can only derive
# from the presented evidence (the rendered verify-registration brief), so it is
# the diff-token pattern elevated to its strongest form.
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{8,}")


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


def _conclusion_recipe_resolver(
    experiment_dir: Path,
) -> Callable[[str], tuple[str, str] | None]:
    """The INJECTED recipe resolver for citation dispatch (the ``recipe`` kind, BR-5).

    The ``recipe`` counterpart to :func:`_conclusion_dossier_resolver`:
    ``state/evidence.py`` never imports ``ops``, so the recipe resolver
    (``ops/extract_recipe.py::resolve_recipe_citation`` — re-derives the recipe
    and returns ``(recipe_signature, summary)``) is passed IN here through the
    top-level ``extract_recipe`` facade (the package-alias form the subject-import
    lint permits from inside the ``decision`` subject). A recipe ``ref`` is
    ``"<seed_kind>:<seed_ref>"``; the resolved answer is the recipe's signature +
    a compact disclosure summary. A no-longer-derivable recipe returns ``None`` →
    :func:`state.evidence.resolve_citation` reports it unresolvable → the append
    refuses loudly (verification at append is load-bearing).
    """
    from hpc_agent.ops import extract_recipe

    def _resolve(ref: str) -> tuple[str, str] | None:
        return extract_recipe.resolve_recipe_citation(experiment_dir, ref)

    return _resolve


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
