"""``verify-relay`` — deterministic audit of the agent's relay vs. the journal.

The machine counterpart to conduct rule 10 — "never relay numbers/state that
don't match the journal" (``docs/design/history/proving-run-2-hardening.md`` §6). The
doctrine already holds the LLM to relaying only code-digested briefs, but the
*relay itself* is unguarded: a rounded number, a swapped run-id, or a stale
state claim ("running" when the journal recorded "failed" — proving run #3)
still reaches the durable record. This verb closes that seam by *deterministic
code auditing the LLM against the durable record* — the inversion of an
LLM-audits-LLM reviewer, and the project's moat stated as a feature.

Home — ``ops/decision/`` (not ``ops/monitor/``): rules 9 (provenance gate) and
10 (this) are the paired trust-seam checks at the human decision / relay
boundary, and rule 9 already lives beside the decision journal
(``ops/decision/journal.py``'s ``append-decision``). The monitor domain
observes run *lifecycle*; this observes the agent's outgoing *message*, and its
primary authoritative source is the decision journal. So it belongs here.

It is a pure AUDIT: it reads durable records, never writes, and never blocks a
turn itself — it returns a verdict. Hook-level enforcement lives in the
``Stop`` hook (:mod:`hpc_agent._kernel.hooks.relay_audit_stop`), which runs
this audit over the final assistant text and blocks the stop once on a
contradiction.

Claim extraction & the heuristics (the bar is USEFUL-conservative, not perfect
— prefer flagging to missing):

* **The numeric-literal grammar (ONE positive definition, not a growing carve-out
  list).** A single grammar (``_NUM_GRAMMAR``) defines the full numeric
  vocabulary the audit recognizes — signed ints, grouping commas, decimals,
  percentages, and scientific notation — and BOTH sides consume it: the
  source-collection side extracts source numbers with it, and the relay-audit
  side runs it as a numeric-span PRE-PASS. The pre-pass consumes every maximal
  numeric-literal span and audits it as a number (or, for a bare job-id-length
  digit run, as a job-id claim), so no numeric FORMAT can reach the id
  classifiers. This *replaced* the accretion of one carve-out per false positive
  — ISO dates, registry verbs, decimal fraction / integer parts, and (run-12
  finding 29) scientific notation — that had grown at each classifier: the id
  passes now simply never see a numeric literal, because ``_is_run_id_like``
  rejects any token the grammar matches and the pre-pass has already consumed
  every literal's span. Adding a future numeric format is one edit to the
  grammar, not a new exception at every classifier.

* **Run-id / job-id tokens.** A token is "run-id-like" when it equals the run in
  scope, starts with ``run-``, is timestamp-shaped (``\\d{8}-\\d{6}…``), or
  carries a hyphen + a digit and is >= 8 chars — but NEVER when it is wholly a
  numeric literal (the grammar decides). Each is matched against the
  authoritative id set (scope run_id + sidecar/record run_id, job_ids,
  parent_run_ids) by exact match or shared prefix (a short-sha reference passes).
  A run-id-like token matching nothing → ``run_id`` mismatch — EXCEPT the
  registry's verb vocabulary ("Next: submit-s3" names a verb, not a run; proving
  run #3 false positive), derived live from the ``@primitive`` registry, and
  EXCEPT ISO 8601 date/timestamp tokens ("2026-07-03T00:00:00+00:00" — the
  journal's own timestamps; a faithful quote is neither an id nor a number
  claim, so the whole span is consumed up front and audited as neither). The
  run-id ident pass runs BEFORE the numeric pre-pass (a run-id legitimately
  embeds digits — ``run-1`` — so its span must be consumed first); a bare digit
  run (>= 5 digits) is a job-id claim ONLY when the run has recorded job_ids and
  the digits do not verify as a number (``1000000`` samples is a number, not a
  suspicious job id). The character spans of every id token are excluded from
  number extraction, so the digits inside a run-id never masquerade as a numeric
  claim.

* **Numbers.** ``(?<![\\w.])`` + ``_NUM_GRAMMAR`` — ints, floats,
  percentages, comma-grouped values, scientific notation, and an OPTIONAL leading
  minus so a verbatim relay of a negative source metric passes (the lookbehind
  keeps the ``-`` off identifier/range hyphens; commas normalized away, ``%``
  stripped). A claim passes
  on an exact normalized-string match, on float equality (so ``95`` == ``95.0``),
  or — for a DECIMAL claim only — when it is a string-prefix of a longer source
  value (pure truncation like ``3.14`` of ``3.1411``). A rounding that changes a
  digit (``3.15`` vs ``3.1411``) is NOT a prefix and IS flagged; the
  ``.``-required guard stops ``1`` from "truncating" ``128``. A number that
  matches no source number → ``number`` mismatch carrying the nearest source
  value. A number when the records carry NO comparable number at all →
  ``unverifiable`` (flagged, never silently passed).

  Conversational numbers are filtered BEFORE counting/flagging: an integer that
  is a line-start ``N.`` list marker, and any number whose nearest preceding
  non-space char is ``~`` (``check back in ~2 minutes``).

  Spelled-out number WORDS are audited too (F-R): a rejected numeric claim
  restated in words is the same distortion, and the digit-only ``_NUM_RE``
  never saw it (``nineteen`` relayed for a journal that records only ``10`` — a
  demonstrated live evasion). Only cardinals whose VALUE is >= 13 (``thirteen``
  and up, tens, hyphenated compounds, and ``hundred``/``thousand``/``million``)
  become claims; ``one``..``twelve`` are overwhelmingly ordinary prose and
  auditing them would flood false positives (see
  :func:`_extract_number_word_claims`). Each qualifying word converts to its
  value and runs the SAME :func:`_match_number` path as a digit claim.

* **State words.** ``running / in_flight / complete / failed / pending /
  timeout / abandoned`` (+ synonyms) plus the verification phrases ``canary
  green`` and ``verified``. Each is mapped to a canonical family and compared to
  the run's recorded state (``RunRecord.status``, falling back to a sidecar
  ``status`` field). A lifecycle claim whose family differs from the recorded
  family → ``state`` mismatch carrying the recorded state. A verification claim
  (``verified`` / ``canary green``) passes only when its needle is evidenced
  value-semantically — never by a serialized JSON KEY (bug-sweep #12: the
  key ``"verified"`` in a persisted S2 brief is present regardless of its
  boolean value, so a raw substring test over the serialized text passed a
  ``verified`` relay after a FAILED canary). The needle counts only when some
  string VALUE contains it (``evidence_digest={"canary": "green"}``) or a KEY
  containing it maps to ``True`` (``verified: true`` — ``verified: false`` must
  NOT evidence it); else it is flagged. A state claim with no recorded state to
  check against at all → ``unverifiable``. A state word preceded by a count
  quantifier (``0 failed``,
  ``no failed waves``) is a COUNT claim, not a state claim (proving run #3
  false positive): a numeric quantifier's digits are audited by the number
  pass, and a zero-word quantifier (``no``/``none``/``zero``) is audited
  against the family's KEYED counts (numeric values / list lengths under keys
  naming the family, e.g. ``failed`` / ``failed_waves`` — the generic number
  pool always carries a 0 somewhere, so it cannot falsify a zero claim). Any
  nonzero keyed count falsifies the zero claim; with no keyed counts at all it
  falls back to the recorded state (``no failed waves`` while the run itself
  is failed → flagged; no state either → ``unverifiable``).

``sources_consulted`` names only the durable records actually found and read
(decision journal, run sidecar, RunRecord, per-run briefs, and — F-Q — the
code-written ``reduce_artifacts`` and ``campaign_briefs``), so a run with no
records honestly reports the empty/short list rather than a fabricated one.

The per-run briefs log (``<experiment>/.hpc/runs/<run_id>.briefs.jsonl``) is
read TOLERANTLY — another agent owns its creation; this verb never creates or
writes it, and a missing/partial file is simply skipped.

Code-written reduce artifacts & campaign briefs (F-Q)
-----------------------------------------------------
A code-drafted completion brief relays reducer-computed metrics (``qlike_sum``,
``n_samples``, ...) and, for a campaign, the campaign-complete numbers — none of
which live in the per-run journal/sidecar/record. Relaying such a brief VERBATIM
was structurally un-passable (the exact opposite of the relay-verbatim
doctrine): every reducer number matched nothing and the integer part of each
decimal even tripped the job-id check. These artifacts are code-written and
journal-adjacent — inside the trust boundary — so this verb loads them too
(tolerantly, fail-open, non-creating, mirroring ``_load_briefs``) and feeds them
into the NUMBER pool, WIDENING the source corpus without lowering the bar:

* ``reduce_artifacts`` — ``_aggregated/<run_id>/metrics_aggregate.json`` (the
  reducer's persisted aggregate, ``ops/aggregate_flow._persist_local_aggregate``),
  the combiner's ``_aggregated/<run_id>/_combiner/wave_<N>.json`` grid
  partials, and any top-level ``_aggregated/<run_id>/*.csv`` table a registered
  ``aggregate_cmd`` / pack reducer persisted (run-12 finding 29 — the corpus
  previously knew only the two JSON names, so a truthful relay of the pack
  reducer's table drew hundreds of mismatches);
* ``campaign_briefs`` — the campaign decision journal
  (``.hpc/campaigns/<campaign_id>/decisions.jsonl``) when the run's sidecar
  carries a ``campaign_id``.

These contribute NUMBERS ONLY: a campaign's own lifecycle words are never fed to
the run-state check (a campaign's ``complete`` is not the run's recorded status).

Notebook-audit relay (v1.5, T11)
--------------------------------
:func:`verify_notebook_relay` is the sibling audit for prose relayed about a
NOTEBOOK audit (``docs/design/notebook-audit.md`` D6: "prose relayed about a
section goes through the rule-10 verify-relay machinery"). The audit VIEW
(markdown projection) states verifiable strings — a section's status
(``auto_cleared`` / ``signed_current`` / ``signed_stale`` / ``unsigned``), the
module ``passed`` verdict, and section/view sha hexes — and an LLM paraphrasing
one wrongly is the same conduct class as misrelaying a run's state. It reduces
each claim against the SAME sources of truth the T6 status reduction uses (the
``"notebook"`` decision journal + the ``.py`` source recomputed on disk) and
returns the same :class:`VerifyRelayResult` shape, so the Stop hook blocks a
contradiction identically. Contradiction KINDS are REUSED, never extended (no
new wire enum / schema regen): a wrong status or ``passed`` verdict is a
``state`` mismatch (a status IS a lifecycle-family claim); a sha-hex matching
neither the current nor any recorded sha is a ``number`` mismatch (the
task-sanctioned reuse — a value claim contradicting the recorded value). An
UNRESOLVABLE source (no interview.json ``audited_source``, unreadable/malformed
``.py``) makes every claim ``unverifiable`` (flagged, never a contradiction) —
the useful-conservative posture, dropped by the hook exactly like a run's
unverifiable claims.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.verify_relay import (
    RelayMismatch,
    VerifyRelayInput,
    VerifyRelayResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from collections.abc import Iterable

# ── the numeric-literal grammar (ONE definition, consumed by both sides) ───────

# THE positive grammar of "what is a number literal" — the full numeric
# vocabulary this audit recognizes: an OPTIONAL leading minus, a digit run with
# grouping commas, an OPTIONAL decimal fraction, an OPTIONAL scientific-notation
# exponent, and an OPTIONAL trailing ``%``. It is the single definition BOTH
# sides consume: the source-collection side (:func:`_collect_source_numbers`)
# extracts source numbers with it, and the relay-audit side runs it as a
# numeric-span PRE-PASS that consumes every maximal numeric-literal span before
# the run-id / job-id classifiers see the text (run-12 finding 29). Growing the
# vocabulary (another numeric format) is one edit HERE, not a new per-format
# carve-out at each classifier.
#
# The ``(?<![\w.])`` lookbehind keeps the ``-`` from being stolen from an
# identifier or a range: it fires only when the char before the ``-`` is neither
# a word char nor a ``.`` (so ``run-1`` / ``a-1`` / ``1-2`` / ``3.14`` never read
# a hyphen or a fractional tail as a signed number). The optional exponent tail
# makes a scientific-notation literal (a reducer table's ``4.585623e-11``) ONE
# maximal token, so its integer / fractional parts never split into stray
# digit runs.
_NUM_GRAMMAR = r"-?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?%?"
_NUM_RE = re.compile(r"(?<![\w.])" + _NUM_GRAMMAR)
# The anchored form of the SAME grammar: True iff a whole token IS one numeric
# literal. This is THE test both classifiers use to answer "is this token a
# number, not an id?" — replacing the ad-hoc ``float()`` probes and the
# decimal-part span heuristics that used to accrete one carve-out per new
# numeric format (dates, verbs, decimal fraction / integer parts, scientific
# notation). ``re.fullmatch`` because a partial numeric prefix of a larger token
# (``4.585623e`` with trailing junk, ``run-1``) is NOT a number literal.
_FULL_NUM_RE = re.compile(_NUM_GRAMMAR)


def _is_number_literal(s: str) -> bool:
    """True iff *s* is wholly one numeric literal under the grammar."""
    return bool(_FULL_NUM_RE.fullmatch(s))


def _normalize_num(raw: str) -> str:
    """Strip grouping commas and a trailing ``%`` — the compare-normal form."""
    return raw.replace(",", "").rstrip("%")


def _is_identifier_like(s: str) -> bool:
    """True for run-id / job-id / date-shaped strings (digit + hyphen).

    Such strings carry digits that are NOT numeric claims (``run-1``,
    ``20260703-141500-ab``), so their embedded numbers are excluded from the
    source-number pool to avoid a relay number spuriously "matching" them.

    A string that IS wholly a numeric literal (under the one grammar) is NOT an
    identifier, though (bug-sweep #39): a negative metric stored as a STRING
    (``"-3.5"``) has a ``-`` and a digit, so the naive test excluded it from the
    pool entirely and a verbatim relay of it was flagged as unverifiable. Such
    tokens belong in the number pool; only genuine ids (``run-1``,
    ``20260703-141500-ab``) stay out.
    """
    return "-" in s and bool(re.search(r"\d", s)) and not _is_number_literal(s)


def _collect_source_numbers(obj: Any, strings: set[str], floats: list[float]) -> None:
    """Recursively gather every comparable number from a durable-record object.

    Scalar ints/floats contribute their value directly; strings contribute
    their embedded number tokens UNLESS the string is identifier-shaped (a
    run-id/job-id/date, whose digits are not numeric facts). Bools are skipped
    (``True``/``False`` are not numbers even though ``isinstance(True, int)``).
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        strings.add(_normalize_num(str(obj)))
        floats.append(float(obj))
        return
    if isinstance(obj, str):
        if _is_identifier_like(obj):
            return
        for m in _NUM_RE.finditer(obj):
            _add_num_token(m.group(0), strings, floats)
        return
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_source_numbers(v, strings, floats)
        return
    if isinstance(obj, (list, tuple)):
        # A list's LENGTH is a derivable fact of the record (run-#12: "27
        # SLURM jobs" — len(job_ids) — was struck as an unsupported numeric
        # claim, forcing a relay to enumerate all 27 ids instead of counting
        # them). Contribute the count alongside the members.
        strings.add(_normalize_num(str(len(obj))))
        floats.append(float(len(obj)))
        for v in obj:
            _collect_source_numbers(v, strings, floats)


def _add_num_token(raw: str, strings: set[str], floats: list[float]) -> None:
    norm = _normalize_num(raw)
    strings.add(norm)
    with contextlib.suppress(ValueError):
        floats.append(float(norm))


def _match_number(raw: str, source_strings: set[str], source_floats: list[float]) -> bool:
    """True iff the relay number *raw* is supported by some source number."""
    norm = _normalize_num(raw)
    if norm in source_strings:
        return True
    try:
        val = float(norm)
    except ValueError:
        # Unparseable numeric token — do not flag (nothing to compare).
        return True
    if any(f == val for f in source_floats):
        return True
    # Truncation tolerance: a DECIMAL claim that is a string-prefix of a longer
    # source value (``3.14`` of ``3.1411``). Requiring the ``.`` stops ``1``
    # from "truncating" ``128``; requiring a strictly longer source stops the
    # exact case (already handled) from double-counting.
    if "." in norm:
        for s in source_strings:
            if len(s) > len(norm) and s.startswith(norm):
                return True
    return False


# ── spelled-out number words (F-R) ─────────────────────────────────────────────

# Cardinal words → value. A rejected numeric claim restated in words is the same
# distortion the digit pass catches; ``_NUM_RE`` is blind to words, so an agent
# under pressure launders ``10`` as ``nineteen`` and passes (a demonstrated live
# evasion). The lexicon covers single-token cardinals + tens; hyphenated
# compounds (``twenty-one``..``ninety-nine``) are composed at parse time.
_WORD_UNITS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_WORD_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip
_WORD_SCALES: dict[str, int] = {"hundred": 100, "thousand": 1000, "million": 1_000_000}

# The minimum spelled-cardinal VALUE audited as a claim. ``one``..``twelve`` are
# overwhelmingly ordinary prose ("one of the", "two ways", "a dozen") — auditing
# them would flood false positives on the word "one" and kill the hook's
# credibility (F-R's explicit warning). ``thirteen`` and up spelled out is rare
# in prose and almost always a deliberate restatement of a count — exactly the
# laundering channel this closes. The threshold is on the VALUE (so a compound
# like ``twenty-one`` and the scale words all qualify), not the surface token.
_NUMBER_WORD_MIN_VALUE = 13

# One number-word token: a tens word with an optional hyphenated unit
# (``ninety-nine``), a bare unit/teen (``nineteen``), a bare tens (``forty``), or
# a scale word (``thousand``). Alpha boundaries on both sides so ``oneiric`` /
# ``someone`` never match. Tens-with-unit is first so the compound wins over the
# bare tens; within the unit alternation, regex backtracking lets ``seventeen``
# win over a ``seven`` prefix (its trailing-boundary lookahead fails on "teen").
_NUMBER_WORD_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:-(?:one|two|three|four|five|six|seven|eight|nine))?"
    r"|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
    r"|hundred|thousand|million"
    r")(?![A-Za-z])",
    re.IGNORECASE,
)


def _number_word_value(token: str) -> int | None:
    """The integer value of a spelled cardinal *token*, or None if unrecognized."""
    t = token.lower()
    if "-" in t:
        tens, _, unit = t.partition("-")
        if tens in _WORD_TENS and unit in _WORD_UNITS:
            return _WORD_TENS[tens] + _WORD_UNITS[unit]
        return None
    if t in _WORD_UNITS:
        return _WORD_UNITS[t]
    if t in _WORD_TENS:
        return _WORD_TENS[t]
    return _WORD_SCALES.get(t)


def _word_is_conversational(text: str, start: int) -> bool:
    """Port of :func:`_is_conversational_number`'s intent for a word claim.

    A number word whose nearest preceding non-space char is ``~`` is chatter
    (``~thirteen minutes``), not a fact — the same tilde-duration heuristic the
    digit pass applies. (The line-marker heuristic is digit-only — a list marker
    is ``13.`` not ``thirteen.`` — so it does not apply to words.)
    """
    j = start - 1
    while j >= 0 and text[j] == " ":
        j -= 1
    return j >= 0 and text[j] == "~"


def _extract_number_word_claims(text: str) -> list[tuple[int, int, str, int]]:
    """Spelled-cardinal claims in *text*: ``(start, end, surface, value)`` tuples.

    Only cardinals whose value is >= :data:`_NUMBER_WORD_MIN_VALUE` are returned
    (see that constant for the false-positive rationale), and a ``~``-prefixed
    conversational word is skipped. Each returned value flows through the SAME
    :func:`_match_number` path as a digit claim in the caller.
    """
    out: list[tuple[int, int, str, int]] = []
    for m in _NUMBER_WORD_RE.finditer(text):
        value = _number_word_value(m.group(0))
        if value is None or value < _NUMBER_WORD_MIN_VALUE:
            continue
        if _word_is_conversational(text, m.start()):
            continue
        out.append((m.start(), m.end(), m.group(0), value))
    return out


def _nearest_number(raw: str, source_floats: list[float]) -> str | None:
    """The source number closest to *raw*, as a string, or None if no numbers."""
    if not source_floats:
        return None
    try:
        val = float(_normalize_num(raw))
    except ValueError:
        return None
    nearest = min(source_floats, key=lambda f: abs(f - val))
    # Render an integral float without the ``.0`` tail so it reads like the
    # source (``128`` not ``128.0``).
    return str(int(nearest)) if nearest == int(nearest) else str(nearest)


# ── run-id / job-id extraction ─────────────────────────────────────────────────

# Tokens with internal ``._-`` separators — the run-id-shaped candidates.
_IDENT_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+")
_TS_PREFIX_RE = re.compile(r"\d{8}-\d{6}")
# A bare digit run of job-id length — the ONLY numeric-literal shape the numeric
# pre-pass may hand to the job-id arm (a decimal / comma-grouped / signed / sci
# span is unambiguously a number, never a job id).
_BARE_JOB_DIGITS_RE = re.compile(r"\d{5,}")

# ISO 8601 date / datetime spans ("2026-07-03", "2026-07-03T00:00:00+00:00") —
# the journal's own timestamp dialect (``infra.time.utcnow_iso``). A faithful
# relay quoting one is NOT an id or number claim, but the shape trips both
# passes (same false-positive class as verbs and decimal fractions, proving
# run #3): the date is hyphen+digit and >= 8 chars so it reads run-id-like,
# and the ``:``-split time components leak bare digit runs into the number
# pass that no source number can verify (identifier-shaped source strings are
# excluded from the number pool). The whole span is therefore consumed up
# front and audited as NEITHER. Distinct from the run-id timestamp shape
# ``\d{8}-\d{6}`` (``_TS_PREFIX_RE``), which stays a run-id claim.
_ISO_DATETIME_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"(?![0-9])"
)
# The fragments ``_IDENT_RE`` can carve out of an ISO date/datetime (its token
# class stops at ``:`` / ``+``): the date, optionally with the hour attached.
_ISO_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2})?")


def _is_run_id_like(token: str, scope_run_id: str) -> bool:
    if token == scope_run_id:
        return True
    if token.lower().startswith("run-"):
        return True
    if _TS_PREFIX_RE.match(token):
        return True
    if _is_number_literal(token):
        # A token that IS wholly a numeric literal is a NUMBER claim, never a
        # run-id one — decimals, comma-grouped values, percentages, and
        # scientific notation (``4.585623e-11``) all read hyphen+digit and long
        # enough to look run-id-shaped (run-12 finding 29). THE grammar decides;
        # the numeric pre-pass audits the whole literal against the source pool.
        return False
    if _ISO_DATE_TOKEN_RE.fullmatch(token):
        # A faithful ISO date/timestamp quote, not a run-id claim (see
        # ``_ISO_DATETIME_RE``); its span is consumed by the ISO pre-pass.
        return False
    return "-" in token and bool(re.search(r"\d", token)) and len(token) >= 8


def _id_matches(token: str, auth_ids: set[str]) -> bool:
    """True iff *token* names an authoritative id (exact or shared prefix)."""
    if token in auth_ids:
        return True
    for aid in auth_ids:
        # Short-sha / prefix reference either direction (>= 4 chars to be a
        # meaningful prefix, not a 1-char coincidence).
        if len(token) >= 4 and (aid.startswith(token) or token.startswith(aid)):
            return True
    return False


def _registry_verb_names() -> frozenset[str]:
    """The registry's verb vocabulary (``submit-s3``, ``verify-relay``, ...).

    Block-verb names are hyphen+digit shaped, so they satisfy
    :func:`_is_run_id_like` and a faithful "Next: submit-s3" relay would flag
    them as unmatched run-ids (proving run #3 false positive). Derived from
    the live ``@primitive`` registry — the canonical verb list — rather than
    a hardcoded copy that would drift as verbs land.
    :func:`register_primitives` is idempotent (a no-op after the first call
    in a process), so this stays cheap and deterministic.
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    return frozenset(get_registry())


# ── state extraction ───────────────────────────────────────────────────────────

# Relay state phrase → canonical family. Multi-word phrases first in the regex.
_STATE_WORD_TO_FAMILY: dict[str, str] = {
    "canary green": "canary_green",
    "in_flight": "running",
    "in-flight": "running",
    "in flight": "running",
    "inflight": "running",
    "timed_out": "timeout",
    "timed out": "timeout",
    "running": "running",
    "complete": "complete",
    "completed": "complete",
    "finished": "complete",
    "succeeded": "complete",
    "success": "complete",
    "failed": "failed",
    "failure": "failed",
    "errored": "failed",
    "pending": "pending",
    "queued": "pending",
    "waiting": "pending",
    "timeout": "timeout",
    "abandoned": "abandoned",
    "verified": "verified",
}

# Recorded ``RunRecord.status`` (or sidecar status) → canonical family.
_STATUS_TO_FAMILY: dict[str, str] = {
    "in_flight": "running",
    "running": "running",
    "complete": "complete",
    "failed": "failed",
    "abandoned": "abandoned",
    "timeout": "timeout",
    "pending": "pending",
}

_LIFECYCLE_FAMILIES = frozenset(
    {"running", "complete", "failed", "pending", "timeout", "abandoned"}
)

# A count quantifier immediately preceding a state word: a number ("0 failed",
# "3 failed waves") or a zero-word ("no failed waves", "none failed"). The
# lookbehind keeps the quantifier a whole token (and lets "3.0 failed" match
# the full decimal, not its fractional tail).
_COUNT_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(\d[\d,]*(?:\.\d+)?|no|none|zero)\s+$",
    re.IGNORECASE,
)


def _count_quantifier(text: str, start: int) -> str | None:
    """The count quantifier directly before the state word at *start*, if any.

    ``"0 failed"`` / ``"no failed waves"`` phrase a COUNT, not a lifecycle
    state (proving run #3 false positive: "0 failed" tripped the state
    matcher as claiming state ``failed``). Returns the quantifier token
    lowercased, or None when the state word stands alone.
    """
    m = _COUNT_QUANT_RE.search(text, 0, start)
    return m.group(1).lower() if m else None


def _collect_keyed_counts(obj: Any, family: str, out: list[float]) -> None:
    """Gather every count keyed to *family* from a durable-record object.

    A dict value under a key naming the family (``failed``, ``failed_waves``,
    ``n_failed``) contributes: its numeric value directly, or its length when
    it is a list (an empty ``failed_waves`` IS a count of 0). The generic
    number pool is useless for verifying a zero-count claim — a RunRecord
    always carries zero-valued counters somewhere — so the zero-word check
    compares against these keyed counts only.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and family in k.lower():
                if isinstance(v, bool):
                    pass
                elif isinstance(v, (int, float)):
                    out.append(float(v))
                elif isinstance(v, (list, tuple)):
                    out.append(float(len(v)))
            _collect_keyed_counts(v, family, out)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_keyed_counts(v, family, out)


# Longest phrases first so ``in flight`` wins over a bare ``flight`` fragment,
# and ``canary green`` over ``green`` alone (which we deliberately don't match).
_STATE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(w) for w in sorted(_STATE_WORD_TO_FAMILY, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


# A verification family → the needle a source must EVIDENCE (value-semantically,
# not as a serialized JSON key — bug-sweep #12).
_VERIFICATION_NEEDLE: dict[str, str] = {"verified": "verified", "canary_green": "green"}


def _collect_verification_evidence(obj: Any, out: set[str]) -> None:
    """Gather the verification needles a durable record actually EVIDENCES.

    The raw substring test this replaces (``needle in json.dumps(source)``) was
    dead once any S2 brief existed: a persisted brief serializes the KEY
    ``"verified"`` regardless of its boolean value, so ``'run-1 is verified'``
    audited clean even after a FAILED canary (bug-sweep #12). A needle counts
    only value-semantically:

    * some STRING value contains it (``evidence_digest={"canary": "green"}``
      evidences ``green``); or
    * a KEY containing it maps to boolean ``True`` (``verified: true`` evidences
      ``verified``; ``verified: false`` must NOT).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and v is True:
                kl = k.lower()
                for needle in _VERIFICATION_NEEDLE.values():
                    if needle in kl:
                        out.add(needle)
            _collect_verification_evidence(v, out)
        return
    if isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_verification_evidence(v, out)
        return
    if isinstance(obj, str):
        s = obj.lower()
        for needle in _VERIFICATION_NEEDLE.values():
            if needle in s:
                out.add(needle)


def _classify_state(
    family: str,
    run_status_raw: str | None,
    run_status_family: str | None,
    verification_evidence: set[str],
    has_sources: bool,
) -> tuple[str, str | None] | None:
    """Return ``(kind, nearest)`` for a state claim, or None when it passes."""
    if family in ("verified", "canary_green"):
        needle = _VERIFICATION_NEEDLE[family]
        if needle in verification_evidence:
            return None
        if run_status_family is None and not has_sources:
            return ("unverifiable", None)
        return ("state", run_status_raw)
    # Lifecycle claim.
    if run_status_family is None:
        return ("unverifiable", None)
    if family == run_status_family:
        return None
    return ("state", run_status_raw)


# How many chars before a state word we scan for the word "canary".
_CANARY_WINDOW = 40


def _is_canary_adjacent(text: str, start: int) -> bool:
    """True when the state word at *start* is within a few tokens of "canary".

    A relayed ``canary failed`` is a claim about the CANARY sibling's outcome,
    not the MAIN run's recorded lifecycle state — flagging it against the main
    run's status (``abandoned``) is a misattribution (F-Q: "canary failed"
    tripped against a main run recorded ``abandoned``). Clean canary-outcome
    attribution is not reliably recoverable at this seam (the canary is a
    separate ``<run_id>-canary`` record this audit does not load), so a
    canary-adjacent lifecycle word is skipped-with-accounting (counted,
    span-consumed, never flagged) — the conservative choice the task sanctions:
    a missed wrong-canary claim beats a false mismatch that would train agents
    to ignore the hook. Conservative window: the ``_CANARY_WINDOW`` chars before
    the state word.
    """
    lo = max(0, start - _CANARY_WINDOW)
    return "canary" in text[lo:start].lower()


# ── source loading ─────────────────────────────────────────────────────────────


def _load_briefs(experiment_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Read ``<exp>/.hpc/runs/<run_id>.briefs.jsonl`` tolerantly (may be absent).

    Never creates or writes the file — another agent owns brief persistence. A
    missing file, unreadable bytes, or an individually-corrupt line yields no
    records for that line rather than raising.
    """
    path = experiment_dir / ".hpc" / "runs" / f"{run_id}.briefs.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_json_tolerant(path: Path) -> Any:
    """Parse a JSON file, or None on any absence/read/parse error (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None


# A combiner partial's file name (``wave_<N>.json``) — anchored so a sibling
# ``wave_<N>.runtime.json`` (runtime samples, not grid points) does not slip in.
# Mirrors ``ops/aggregate_flow._WAVE_PARTIAL_NAME_RE``.
_WAVE_PARTIAL_NAME_RE = re.compile(r"^wave_\d+\.json$")


# A CSV reduce artifact larger than this is skipped rather than read — the
# corpus widens with the run-level table (tens of KB), never with a mirrored
# per-task tree's worth of data.
_CSV_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024


def _load_reduce_artifacts(experiment_dir: Path, run_id: str) -> list[Any]:
    """Read the code-written reduce artifacts for *run_id* tolerantly (F-Q).

    Three shapes under the experiment's aggregated area, all written by the
    DETERMINISTIC reducer/combiner (inside the trust boundary, journal-adjacent):

    * ``_aggregated/<run_id>/metrics_aggregate.json`` — the reducer's persisted
      aggregate (``ops/aggregate_flow._persist_local_aggregate``);
    * ``_aggregated/<run_id>/_combiner/wave_<N>.json`` — the combiner's per-wave
      grid partials (``wave_<N>.runtime.json`` siblings excluded);
    * ``_aggregated/<run_id>/*.csv`` (top level ONLY, bounded size) — the table a
      registered ``aggregate_cmd`` / pack reducer persists (run-12 finding 29:
      ``metrics_table.csv``'s truthful relay drew 337 mismatches because the
      corpus only knew the two JSON names). Cells are contributed as strings;
      the pulled per-task mirror (``_per_task_results/``) is deliberately NOT
      walked — the corpus carries the reducer's OUTPUT, not its input tree.

    Never creates or writes anything (mirrors :func:`_load_briefs`): a missing
    dir/file or unreadable/corrupt bytes yields no records for that artifact. The
    caller feeds the result into the NUMBER pool only.
    """
    out: list[Any] = []
    agg_dir = experiment_dir / "_aggregated" / run_id
    aggregate = _read_json_tolerant(agg_dir / "metrics_aggregate.json")
    if aggregate is not None:
        out.append(aggregate)
    combiner = agg_dir / "_combiner"
    try:
        wave_files = sorted(
            p for p in combiner.glob("wave_*.json") if _WAVE_PARTIAL_NAME_RE.match(p.name)
        )
    except OSError:
        wave_files = []
    for wf in wave_files:
        wave = _read_json_tolerant(wf)
        if wave is not None:
            out.append(wave)
    try:
        csv_files = sorted(p for p in agg_dir.glob("*.csv") if p.is_file())
    except OSError:
        csv_files = []
    for cf in csv_files:
        try:
            if cf.stat().st_size > _CSV_ARTIFACT_MAX_BYTES:
                continue
            text = cf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        cells = [c.strip() for line in text.splitlines() for c in line.split(",") if c.strip()]
        if cells:
            out.append(cells)
    return out


def _load_campaign_briefs(experiment_dir: Path, sidecar: Any) -> list[dict[str, Any]]:
    """Read the campaign decision journal when the run's sidecar names one (F-Q).

    A campaign-complete brief is code-drafted from reducer output; the run
    sidecar carries ``campaign_id`` (``state/runs`` field set), and when present
    the campaign's decision journal
    (``.hpc/campaigns/<campaign_id>/decisions.jsonl``) is read via
    ``read_decisions``. Tolerant / non-creating: no ``campaign_id`` (or a
    non-dict sidecar, or an unreadable journal) yields no records. The caller
    feeds the result into the NUMBER pool only — a campaign's own lifecycle words
    must NOT be checked against the run's recorded status.
    """
    if not isinstance(sidecar, dict):
        return []
    cid = sidecar.get("campaign_id")
    if not isinstance(cid, str) or not cid:
        return []
    from hpc_agent.state.decision_journal import read_decisions

    try:
        return read_decisions(experiment_dir, "campaign", cid)
    except Exception:
        return []


def _dedupe_mismatches(items: Iterable[RelayMismatch]) -> list[RelayMismatch]:
    seen: set[tuple[str, str, str, str | None]] = set()
    out: list[RelayMismatch] = []
    for m in items:
        key = (m.claim, m.kind, m.detail, m.nearest_source_value)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


@primitive(
    name="verify-relay",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # A pure read-only audit: re-running it is always safe (it writes nothing).
    idempotent=True,
    cli=CliShape(
        help=(
            "Audit an agent's draft relay text against a run's durable records "
            "(decision journal, sidecar, RunRecord, briefs). Deterministic "
            "claim-extraction — numbers, run/job ids, state words — diffed "
            "against the record. Returns a verdict; never blocks (conduct "
            "rule 10)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=VerifyRelayInput,
        schema_ref=SchemaRef(input="verify_relay"),
    ),
    agent_facing=True,
)
def verify_relay(*, experiment_dir: Path, spec: VerifyRelayInput) -> VerifyRelayResult:
    """Audit *spec.relay_text* against the durable records for *spec.run_id*.

    Deterministically extracts the factual claims (numbers, run/job ids, state
    words) from the relay and diffs each against the decision journal, run
    sidecar, RunRecord, and per-run briefs. Returns a verdict — ``clean`` plus
    the itemized mismatches; it never blocks the turn itself.

    Raises
    ------
    :class:`errors.SpecInvalid`
        Never raised for a well-formed spec; the run_id shape is enforced at
        the wire boundary. Declared for registry honesty.
    """
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    experiment_dir = Path(experiment_dir)
    run_id = spec.run_id
    relay = spec.relay_text or ""

    # ── load the authoritative sources (honest sources_consulted) ──────────────
    sources_consulted: list[str] = []
    source_objs: list[Any] = []

    journal_records = read_decisions(experiment_dir, "run", run_id)
    if journal_records:
        sources_consulted.append("decision_journal")
        source_objs.extend(journal_records)

    sidecar: dict[str, Any] | None
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, ValueError):
        sidecar = None
    if sidecar is not None:
        sources_consulted.append("run_sidecar")
        source_objs.append(sidecar)

    record = load_run(experiment_dir, run_id)
    record_dict: dict[str, Any] | None = None
    if record is not None:
        record_dict = dataclasses.asdict(record)
        sources_consulted.append("run_record")
        source_objs.append(record_dict)

    briefs = _load_briefs(experiment_dir, run_id)
    if briefs:
        sources_consulted.append("briefs")
        source_objs.extend(briefs)

    # ── number-only sources (F-Q) ──────────────────────────────────────────────
    # Code-written, journal-adjacent artifacts whose numbers a verbatim
    # completion relay legitimately carries. Fed to the NUMBER pool but NOT
    # ``source_text`` / keyed counts / the run-state check — a campaign's own
    # lifecycle words are not the run's recorded status.
    number_only_objs: list[Any] = []
    reduce_artifacts = _load_reduce_artifacts(experiment_dir, run_id)
    if reduce_artifacts:
        sources_consulted.append("reduce_artifacts")
        number_only_objs.extend(reduce_artifacts)
    campaign_briefs = _load_campaign_briefs(experiment_dir, sidecar)
    if campaign_briefs:
        sources_consulted.append("campaign_briefs")
        number_only_objs.extend(campaign_briefs)

    # ── build the compare pools ────────────────────────────────────────────────
    source_num_strings: set[str] = set()
    source_num_floats: list[float] = []
    for obj in source_objs:
        _collect_source_numbers(obj, source_num_strings, source_num_floats)
    for obj in number_only_objs:
        _collect_source_numbers(obj, source_num_strings, source_num_floats)
    has_source_numbers = bool(source_num_strings)

    # Verification evidence (``verified`` / ``canary green``) is value-semantic,
    # NOT a substring of the serialized record — a persisted brief's KEY
    # "verified" must not vouch for a failed canary (bug-sweep #12).
    verification_evidence: set[str] = set()
    for obj in source_objs:
        _collect_verification_evidence(obj, verification_evidence)

    # Status lives on the journal RunRecord only — the run sidecar never
    # carries a "status" key (write_run_sidecar's field set), so there is
    # no sidecar fallback here.
    run_status_raw: str | None = record.status if record is not None else None
    run_status_family = _STATUS_TO_FAMILY.get(run_status_raw or "")

    # Authoritative id set + recorded job ids.
    auth_ids: set[str] = {run_id}
    job_ids: set[str] = set()
    for src in (sidecar, record_dict):
        if not isinstance(src, dict):
            continue
        rid = src.get("run_id")
        if isinstance(rid, str) and rid:
            auth_ids.add(rid)
        # The run's own campaign id is an authoritative identifier: a verbatim
        # campaign-complete brief names it (F-Q), and it is run-id-shaped
        # (``run10-proving``) so it would otherwise flag as an unknown run-id.
        cid = src.get("campaign_id")
        if isinstance(cid, str) and cid:
            auth_ids.add(cid)
        # The supersession audit links (ops/supersession stamps both directions
        # on the run record) are authoritative identifiers: a truthful relay of
        # a supersession names the OTHER run in the pair ("X was superseded by
        # Y"), and Y would otherwise flag as an unknown run-id for X's audit.
        for key in ("supersedes", "superseded_by"):
            sup = src.get(key)
            if isinstance(sup, str) and sup:
                auth_ids.add(sup)
        for key in ("job_ids", "parent_run_ids"):
            vals = src.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, (str, int)) and str(v):
                        auth_ids.add(str(v))
                        if key == "job_ids":
                            job_ids.add(str(v))

    mismatches: list[RelayMismatch] = []
    claims_checked = 0
    consumed_spans: list[tuple[int, int]] = []

    # ── (0) ISO date/timestamp spans (consumed; neither id nor number claims) ──
    for m in _ISO_DATETIME_RE.finditer(relay):
        # A faithful quote of the journal's own timestamps ("submitted at
        # 2026-07-03T00:00:00+00:00"). Consume the WHOLE span so the time
        # components' digit runs never masquerade as numeric claims; the
        # date-shaped token itself is exempted in _is_run_id_like.
        consumed_spans.append((m.start(), m.end()))

    # ── (1) run-id / job-id tokens (first; their spans block number reads) ─────
    verb_names = _registry_verb_names()
    for m in _IDENT_RE.finditer(relay):
        token = m.group(0)
        if not _is_run_id_like(token, run_id):
            continue
        if token.lower() in verb_names:
            # Registry verb vocabulary ("Next: submit-s3"), not a run-id
            # claim. Consume the span so the digit inside the verb name is
            # not read as a numeric claim, but audit nothing.
            consumed_spans.append((m.start(), m.end()))
            continue
        consumed_spans.append((m.start(), m.end()))
        claims_checked += 1
        if not _id_matches(token, auth_ids):
            mismatches.append(
                RelayMismatch(
                    claim=token,
                    kind="run_id",
                    detail=(
                        f"run-id-shaped token {token!r} matches no authoritative "
                        f"identifier for run {run_id!r}"
                    ),
                    nearest_source_value=run_id,
                )
            )

    # ── (2) numeric-literal pre-pass (THE grammar; consumes every maximal span) ─
    # Every maximal numeric-literal span (``_NUM_RE`` — the one grammar) is
    # audited here and its span consumed, so no numeric FORMAT can reach the
    # run-id / job-id classifiers (run-12 finding 29 retired the per-format
    # carve-outs for ISO dates, decimal fraction / integer parts, and scientific
    # notation). Each span resolves to exactly one verdict:
    #
    #   * JOB-ID claim — a bare digit run of job-id length (``\\d{5,}``), and
    #     ONLY when the run has recorded ``job_ids`` and the digits do not verify
    #     as a number (a recorded ``1000000`` samples count is a number, not a
    #     suspicious job id). A recorded job id passes; any other such run flags
    #     ``run_id``.
    #   * NUMBER claim — everything else the grammar matches (decimals,
    #     comma-grouped, percentages, scientific notation, signed, short ints, or
    #     any int when the run has no recorded job_ids): audited against the
    #     source number pool exactly as before.
    for m in _NUM_RE.finditer(relay):
        if _overlaps(m.start(), m.end(), consumed_spans):
            continue
        raw = m.group(0)
        if _is_conversational_number(relay, m.start(), m.end(), raw):
            # Chatter (list marker / ``~2 minutes``), not a fact. Not consumed:
            # a bare short int is invisible to the id classifiers anyway.
            continue
        is_job_candidate = (
            bool(job_ids)
            and bool(_BARE_JOB_DIGITS_RE.fullmatch(raw))
            and not _match_number(raw, source_num_strings, source_num_floats)
        )
        consumed_spans.append((m.start(), m.end()))
        claims_checked += 1
        if is_job_candidate:
            # A bare job-id-length digit run that verifies as no number: a
            # job-id claim. A recorded id passes; anything else is unknown.
            if raw not in job_ids:
                mismatches.append(
                    RelayMismatch(
                        claim=raw,
                        kind="run_id",
                        detail=(
                            f"job-id-shaped token {raw!r} is not among the run's "
                            f"recorded job ids {sorted(job_ids)}"
                        ),
                        nearest_source_value=", ".join(sorted(job_ids)),
                    )
                )
            continue
        if not has_source_numbers:
            mismatches.append(
                RelayMismatch(
                    claim=raw,
                    kind="unverifiable",
                    detail=(
                        f"numeric claim {raw!r} has no comparable value in any "
                        "durable record for the run"
                    ),
                    nearest_source_value=None,
                )
            )
            continue
        if not _match_number(raw, source_num_strings, source_num_floats):
            mismatches.append(
                RelayMismatch(
                    claim=raw,
                    kind="number",
                    detail=(
                        f"numeric claim {raw!r} matches no source number (nor a truncation of one)"
                    ),
                    nearest_source_value=_nearest_number(raw, source_num_floats),
                )
            )

    # ── (2b) spelled-out number words (F-R) ────────────────────────────────────
    # A rejected numeric claim restated in words is the same distortion; the
    # value runs the SAME source-number checks as a digit claim. Only cardinals
    # >= _NUMBER_WORD_MIN_VALUE qualify (see _extract_number_word_claims).
    for start, end, surface, value in _extract_number_word_claims(relay):
        if _overlaps(start, end, consumed_spans):
            continue
        norm = str(value)
        claims_checked += 1
        if not has_source_numbers:
            mismatches.append(
                RelayMismatch(
                    claim=surface,
                    kind="unverifiable",
                    detail=(
                        f"spelled-out numeric claim {surface!r} (= {value}) has no "
                        "comparable value in any durable record for the run"
                    ),
                    nearest_source_value=None,
                )
            )
            continue
        if not _match_number(norm, source_num_strings, source_num_floats):
            mismatches.append(
                RelayMismatch(
                    claim=surface,
                    kind="number",
                    detail=(
                        f"spelled-out numeric claim {surface!r} (= {value}) matches "
                        "no source number"
                    ),
                    nearest_source_value=_nearest_number(norm, source_num_floats),
                )
            )

    # ── (3) state words (deduped by phrase) ────────────────────────────────────
    seen_state: set[str] = set()
    for m in _STATE_RE.finditer(relay):
        phrase = re.sub(r"\s+", " ", m.group(0).lower())
        family = _STATE_WORD_TO_FAMILY.get(phrase)
        if family is None:
            continue
        quant = _count_quantifier(relay, m.start()) if family in _LIFECYCLE_FAMILIES else None
        if quant is not None:
            # "0 failed" / "no failed waves" is a COUNT claim, not a state
            # claim (proving run #3 false positive). A numeric quantifier's
            # digits are audited by the number pass above; a zero-word
            # quantifier ("no"/"none"/"zero") asserts a count of 0, audited
            # here against the family's KEYED counts — the generic number
            # pool always carries a 0 somewhere (RunRecord zero-valued
            # counters), so it cannot falsify a zero claim. NOT added to
            # seen_state — a later unquantified use of the same word is
            # still a state claim.
            if quant[0].isdigit():
                continue
            claims_checked += 1
            claim = f"{quant} {m.group(0)}"
            keyed_counts: list[float] = []
            for obj in source_objs:
                _collect_keyed_counts(obj, family, keyed_counts)
            nonzero = [c for c in keyed_counts if c != 0]
            if nonzero:
                # Conservative: ANY nonzero recorded count for the family
                # falsifies a zero-count claim, even when another source
                # also recorded a 0 (contradictory sources → prefer flagging).
                nearest_count = min(nonzero)
                mismatches.append(
                    RelayMismatch(
                        claim=claim,
                        kind="number",
                        detail=(
                            f"count claim {claim!r} asserts a zero count but the "
                            f"records carry a nonzero {family!r} count"
                        ),
                        nearest_source_value=(
                            str(int(nearest_count))
                            if nearest_count == int(nearest_count)
                            else str(nearest_count)
                        ),
                    )
                )
                continue
            if keyed_counts:
                # Counts exist and every one is zero — the claim is verified.
                continue
            # No keyed counts at all — fall back to the recorded state: a
            # zero-count claim for the run's OWN recorded state contradicts
            # it ("no failed waves" while the run itself is failed).
            if run_status_family == family:
                mismatches.append(
                    RelayMismatch(
                        claim=claim,
                        kind="state",
                        detail=(
                            f"count claim {claim!r} asserts zero but the run's "
                            f"recorded state is {run_status_raw!r}"
                        ),
                        nearest_source_value=run_status_raw,
                    )
                )
            elif run_status_family is None:
                mismatches.append(
                    RelayMismatch(
                        claim=claim,
                        kind="unverifiable",
                        detail=(
                            f"count claim {claim!r} has no comparable count or "
                            "state in any durable record for the run"
                        ),
                        nearest_source_value=None,
                    )
                )
            continue
        if family in _LIFECYCLE_FAMILIES and _is_canary_adjacent(relay, m.start()):
            # Canary-adjacent lifecycle word ("canary failed"): a claim about
            # the CANARY sibling, not the main run — skip-with-accounting rather
            # than misattribute it to the main run's status (F-Q). Counted, not
            # flagged; NOT added to seen_state so a later non-canary use of the
            # same word is still checked.
            claims_checked += 1
            continue
        if phrase in seen_state:
            continue
        seen_state.add(phrase)
        claims_checked += 1
        verdict = _classify_state(
            family, run_status_raw, run_status_family, verification_evidence, bool(source_objs)
        )
        if verdict is None:
            continue
        kind, nearest = verdict
        detail = (
            f"state claim {m.group(0)!r} has no comparable state in the record"
            if kind == "unverifiable"
            else (f"state claim {m.group(0)!r} contradicts the recorded state {run_status_raw!r}")
        )
        mismatches.append(
            RelayMismatch(
                claim=m.group(0),
                kind=kind,  # type: ignore[arg-type]
                detail=detail,
                nearest_source_value=nearest,
            )
        )

    mismatches = _dedupe_mismatches(mismatches)
    return VerifyRelayResult(
        clean=not mismatches,
        claims_checked=claims_checked,
        mismatches=mismatches,
        sources_consulted=sources_consulted,
    )


def _overlaps(start: int, end: int, spans: Iterable[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


def _is_conversational_number(text: str, start: int, end: int, raw: str) -> bool:
    """True for a number that is chatter, not a fact.

    Two pragmatic heuristics (documented in the module docstring):

    * a line-start ``N.`` list marker with an integer value 0-10;
    * any number whose nearest preceding non-space char is ``~`` (``~2 minutes``).
    """
    # ~-prefixed duration: nearest preceding non-space char is a tilde.
    j = start - 1
    while j >= 0 and text[j] == " ":
        j -= 1
    if j >= 0 and text[j] == "~":
        return True
    # Line-start ``N.`` list marker (integer 0-10 only).
    line_start = text.rfind("\n", 0, start) + 1
    before = text[line_start:start]
    if before.strip() == "" and end < len(text) and text[end] == "." and "." not in raw:
        try:
            if 0 <= int(raw) <= 10:
                return True
        except ValueError:
            return False
    return False


# ── notebook-audit relay (v1.5 / T11) ──────────────────────────────────────────

# The per-section status vocabulary a relay can claim (``state`` metaphor: a
# status IS a lifecycle-family word). Surface forms with ``_`` / ``-`` / space /
# no separator all normalize to the canonical status the T6 reduction yields.
_NB_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:auto[ _-]?cleared|signed[ _-]?current|signed[ _-]?stale|unsigned)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_NB_STATUS_CANON: dict[str, str] = {
    # sep-stripped surface → the canonical status string (== state.notebook_audit
    # constants; these are wire-visible vocabulary and do not drift).
    "autocleared": "auto_cleared",
    "signedcurrent": "signed_current",
    "signedstale": "signed_stale",
    "unsigned": "unsigned",
}

# A sha-hex token: 12–64 hex chars, boundary-guarded. Requiring >= one ``a-f``
# letter (checked at use) excludes pure-decimal counts (``1000000``) that are
# hex-valid but are NUMBER claims, not shas.
_NB_HEX_RE = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{12,64}(?![A-Za-z0-9])")

# How near (chars) a status word / sha-hex must sit to a section slug to be a
# claim ABOUT that section. A status word with no slug in range is module-level
# noise, not a section claim — skipped (conservative: prefer precision).
_NB_PROXIMITY = 80

# Verdict tokens for the module ``passed`` claim (checked only within a window of
# the audit-id mention). Negators flip a positive token (``cannot graduate``).
_NB_PASS_TOKENS = frozenset({"pass", "passed", "passes", "graduate", "graduated", "graduates"})
_NB_FAIL_TOKENS = frozenset({"fail", "failed", "fails", "block", "blocked", "blocks"})
_NB_NEGATORS = frozenset(
    {"not", "no", "never", "cannot", "cant", "wont", "isnt", "arent", "doesnt", "didnt", "hasnt"}
)


def _nb_canon_status(raw: str) -> str | None:
    """Canonical status for a matched surface form (sep-stripped), or None."""
    return _NB_STATUS_CANON.get(re.sub(r"[\s_-]+", "", raw.lower()))


def _nb_slug_spans(text: str, slug: str) -> list[tuple[int, int]]:
    """Every whole-token occurrence span of *slug* in *text*.

    Slugs carry ``.`` / ``-`` (the ``_RUN_ID_RE`` class); the boundary guard
    treats those as slug chars so ``fit-model`` is not fragmented (mirrors
    ``ops/decision/journal.py::_names_slug``).
    """
    if not slug:
        return []
    pat = re.compile(r"(?<![A-Za-z0-9._-])" + re.escape(slug) + r"(?![A-Za-z0-9._-])")
    return [(m.start(), m.end()) for m in pat.finditer(text)]


def _nb_nearest_slug(
    start: int, end: int, slug_spans: dict[str, list[tuple[int, int]]]
) -> str | None:
    """The mentioned slug whose nearest occurrence is within :data:`_NB_PROXIMITY`."""
    best: str | None = None
    best_dist: int | None = None
    for slug, spans in slug_spans.items():
        for s, e in spans:
            if e <= start:
                dist = start - e
            elif s >= end:
                dist = s - end
            else:
                dist = 0
            if dist <= _NB_PROXIMITY and (best_dist is None or dist < best_dist):
                best_dist, best = dist, slug
    return best


def _nb_hex_matches(token: str, candidates: Iterable[str]) -> bool:
    """True iff *token* equals or is a shared prefix of some candidate sha."""
    t = token.lower()
    for cand in candidates:
        c = cand.lower()
        if t == c or (len(t) >= 7 and (c.startswith(t) or t.startswith(c))):
            return True
    return False


def _nb_verdict_near(text: str, start: int, end: int) -> bool | None:
    """The module-``passed`` polarity claimed within a window of an audit mention.

    Returns True (a pass/graduate claim), False (a fail/block or negated-pass
    claim), or None (no verdict word in range). The first verdict token found
    wins; a pass token preceded within 3 tokens by a negator flips to False
    (``cannot graduate`` / ``did not pass``).
    """
    lo = max(0, start - _NB_PROXIMITY)
    hi = min(len(text), end + _NB_PROXIMITY)
    tokens = re.findall(r"[a-z']+", text[lo:hi].lower())
    tokens = [t.replace("'", "") for t in tokens]
    for i, tok in enumerate(tokens):
        if tok in _NB_PASS_TOKENS:
            return not any(prev in _NB_NEGATORS for prev in tokens[max(0, i - 3) : i])
        if tok in _NB_FAIL_TOKENS:
            return False
    return None


def _nb_journal_slugs(records: Iterable[dict[str, Any]]) -> set[str]:
    """Section slugs named by the notebook-attestation records in *records*."""
    from hpc_agent.state import notebook_audit as nb

    attestation_blocks = {nb.SIGN_OFF_BLOCK, nb.AUTO_CLEAR_BLOCK}
    out: set[str] = set()
    for rec in records:
        if rec.get("block") not in attestation_blocks:
            continue
        resolved = rec.get("resolved")
        if isinstance(resolved, dict):
            sec = resolved.get("section")
            if isinstance(sec, str) and sec:
                out.add(sec)
    return out


def _nb_read_py(experiment_dir: Path, rel: Any) -> str | None:
    """Read a campaign-dir-relative ``.py`` tolerantly, or None (never raises)."""
    if not isinstance(rel, str) or not rel:
        return None
    try:
        return (experiment_dir / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _nb_resolved_from_journal(experiment_dir: Path, audit_id: str) -> tuple[Any, Any]:
    """The newest notebook-journal record's ``resolved.source`` / ``.template``.

    The interview-less (plugin-driven) fallback (coverage finding F5): an audit
    signed via ``notebook-ingest-signoffs`` never writes interview.json, but every
    sign-off it lands rides ``resolved.source`` / ``resolved.template`` (the CURRENT
    source the section_sha/view_sha were recomputed from). Reading the newest such
    record recovers the same paths the gate recomputed against, so an
    interview-less audit is no longer invisible to the attention-queue collector or
    permanently unverifiable to the Stop hook. Non-creating, fail-soft: records are
    read append-order (newest last wins); a record with no ``resolved.source`` is
    skipped. Returns ``(source_rel, template_rel)`` (either may be ``None``).
    """
    from hpc_agent.state.decision_journal import read_decisions

    source_rel: Any = None
    template_rel: Any = None
    for rec in read_decisions(experiment_dir, "notebook", audit_id):
        resolved = rec.get("resolved")
        if not isinstance(resolved, dict):
            continue
        src = resolved.get("source")
        if isinstance(src, str) and src:
            source_rel = src
            tmpl = resolved.get("template")
            template_rel = tmpl if isinstance(tmpl, str) and tmpl else None
    return source_rel, template_rel


def _nb_resolve_sources(experiment_dir: Path, audit_id: str) -> tuple[Any | None, list[str] | None]:
    """Resolve ``(parsed_source | None, required_slugs | None)`` for *audit_id*.

    The source ``.py`` and (optional) template are resolved from interview.json's
    ``audited_source`` block matching *audit_id* — the same lookup the T8 sign-off
    gate uses (``ops/decision/journal.py::_read_interview_audited_source``, reused
    rather than re-implemented; same subject, lint-clean). When no interview.json
    block resolves (a plugin-driven, interview-less audit), it FALLS BACK to the
    newest notebook-journal record's ``resolved.source`` / ``.template`` (F5), so a
    signable-but-interview-less audit is still visible to the attention-queue audit
    collector and verifiable to the Stop hook. An unresolvable / unreadable /
    malformed source returns ``(None, ...)`` so the caller flags claims
    UNVERIFIABLE rather than fabricating a bogus ``unsigned`` reduction.
    ``required_slugs`` are the TEMPLATE's slugs (the T9 gate's required set); a
    missing template returns ``None`` there — the module ``passed`` claim is then
    not checkable and is skipped.
    """
    from hpc_agent.ops.decision.journal import _read_interview_audited_source
    from hpc_agent.state.audit_source import parse_percent_source

    block = _read_interview_audited_source(experiment_dir, audit_id)
    if block is not None:
        source_rel: Any = block.get("source")
        template_rel: Any = block.get("template")
    else:
        source_rel, template_rel = _nb_resolved_from_journal(experiment_dir, audit_id)

    source_text = _nb_read_py(experiment_dir, source_rel)
    if source_text is None:
        return None, None
    try:
        parsed = parse_percent_source(source_text)
    except errors.SpecInvalid:
        return None, None

    required_slugs: list[str] | None = None
    template_text = _nb_read_py(experiment_dir, template_rel)
    if template_text is not None:
        try:
            required_slugs = list(parse_percent_source(template_text).slugs)
        except errors.SpecInvalid:
            required_slugs = None
    return parsed, required_slugs


def verify_notebook_relay(
    experiment_dir: Path, audit_id: str, relay_text: str
) -> VerifyRelayResult:
    """Audit *relay_text*'s claims about notebook audit *audit_id* (T11).

    The notebook sibling of :func:`verify_relay`: it extracts the VERIFIABLE
    claims the audit view states — a section's status, the module ``passed``
    verdict, and section/view sha hexes — and diffs each against the ``"notebook"``
    decision journal (the T6 reduction) plus the ``.py`` source recomputed on
    disk. Returns the same :class:`VerifyRelayResult`; the Stop hook blocks a
    ``state`` / ``number`` contradiction identically to a run's.

    Claim grammar (all co-occurrence, useful-conservative — prefer precision):

    * **status** — a status word (``auto_cleared`` / ``signed_current`` /
      ``signed_stale`` / ``unsigned``, any separator) within :data:`_NB_PROXIMITY`
      chars of a mentioned section slug → a claim that section HAS that status.
      Verified via ``state.notebook_audit.audit_section``; a mismatch is a
      ``state`` contradiction carrying the actual status.
    * **passed** — a pass/graduate (or negated / fail) verdict word within range
      of the audit-id mention → a claim about ``ModuleAudit.passed`` (rolled up
      over the TEMPLATE's required slugs). A wrong verdict is a ``state``
      contradiction. Skipped when no template resolves.
    * **sha** — a 12–64 hex token (with >= one ``a-f`` letter, so decimals are
      excluded) attributed to a nearby slug must equal/prefix that section's
      current ``section_sha`` OR a recorded sign-off ``section_sha`` / ``view_sha``;
      otherwise a ``number`` contradiction.

    An UNRESOLVABLE source makes every status/sha claim ``unverifiable`` (flagged,
    never a contradiction — the hook drops it). Slugs are drawn from the current
    source AND the journal records (a signed-then-deleted section is still named).
    Read-only and fail-safe: a corrupt journal line is skipped by the reader.
    """
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.decision_journal import read_decisions

    experiment_dir = Path(experiment_dir)
    relay = relay_text or ""

    sources_consulted: list[str] = []
    records = read_decisions(experiment_dir, "notebook", audit_id)
    if records:
        sources_consulted.append("notebook_journal")

    parsed, required_slugs = _nb_resolve_sources(experiment_dir, audit_id)
    if parsed is not None:
        sources_consulted.append("audited_source")

    source_by_slug = {s.slug: s for s in parsed.sections} if parsed is not None else {}
    slug_universe = set(source_by_slug) | _nb_journal_slugs(records)
    # Only slugs the relay actually mentions can carry a claim.
    slug_spans = {slug: spans for slug in slug_universe if (spans := _nb_slug_spans(relay, slug))}

    mismatches: list[RelayMismatch] = []
    claims_checked = 0

    def _section_status(slug: str) -> nb.SectionAudit:
        current_sha = source_by_slug[slug].section_sha if slug in source_by_slug else None
        return nb.audit_section(records, slug, current_sha)

    # ── (a) status claims ──────────────────────────────────────────────────────
    for m in _NB_STATUS_RE.finditer(relay):
        claimed = _nb_canon_status(m.group(0))
        if claimed is None:
            continue
        slug = _nb_nearest_slug(m.start(), m.end(), slug_spans)
        if slug is None:
            continue  # not attributable to a section — module-level / noise
        claims_checked += 1
        if parsed is None:
            mismatches.append(
                RelayMismatch(
                    claim=f"{slug} {claimed}",
                    kind="unverifiable",
                    detail=(
                        f"status claim {claimed!r} about section {slug!r} cannot be "
                        f"verified — the audited .py source for audit_id={audit_id!r} "
                        "did not resolve"
                    ),
                    nearest_source_value=None,
                )
            )
            continue
        actual = _section_status(slug).status
        if claimed != actual:
            mismatches.append(
                RelayMismatch(
                    claim=f"{slug} {claimed}",
                    kind="state",
                    detail=(
                        f"section {slug!r} is relayed as {claimed!r} but the journal + "
                        f"current source reduce it to {actual!r}"
                    ),
                    nearest_source_value=actual,
                )
            )

    # ── (b) module passed / gate verdict ───────────────────────────────────────
    if parsed is not None and required_slugs is not None and _nb_slug_spans(relay, audit_id):
        rollup = nb.audit_module(
            experiment_dir, audit_id, source=parsed, required_slugs=required_slugs
        )
        for start, end in _nb_slug_spans(relay, audit_id):
            claimed_pass = _nb_verdict_near(relay, start, end)
            if claimed_pass is None:
                continue
            claims_checked += 1
            if claimed_pass != rollup.passed:
                mismatches.append(
                    RelayMismatch(
                        claim=f"audit {audit_id} {'passed' if claimed_pass else 'not passed'}",
                        kind="state",
                        detail=(
                            f"audit {audit_id!r} is relayed as "
                            f"{'passed' if claimed_pass else 'not passed'} but the "
                            f"graduation rollup computes passed={rollup.passed}"
                        ),
                        nearest_source_value="passed" if rollup.passed else "not passed",
                    )
                )

    # ── (c) sha-hex claims ─────────────────────────────────────────────────────
    for m in _NB_HEX_RE.finditer(relay):
        token = m.group(0)
        if not any(c in "abcdefABCDEF" for c in token):
            continue  # pure-decimal token — a number claim, not a sha
        slug = _nb_nearest_slug(m.start(), m.end(), slug_spans)
        if slug is None:
            continue  # a hex not attributed to a section — unverifiable, not surfaced
        claims_checked += 1
        if parsed is None:
            mismatches.append(
                RelayMismatch(
                    claim=token,
                    kind="unverifiable",
                    detail=(
                        f"sha claim {token!r} about section {slug!r} cannot be verified "
                        f"— the audited .py source for audit_id={audit_id!r} did not "
                        "resolve"
                    ),
                    nearest_source_value=None,
                )
            )
            continue
        audit = _section_status(slug)
        candidates = [
            c
            for c in (audit.current_section_sha, audit.signed_section_sha, audit.view_sha)
            if isinstance(c, str) and c
        ]
        if not _nb_hex_matches(token, candidates):
            mismatches.append(
                RelayMismatch(
                    claim=token,
                    kind="number",
                    detail=(
                        f"hex {token!r} attributed to section {slug!r} matches neither "
                        "its current section_sha nor a recorded sign-off sha/view_sha"
                    ),
                    nearest_source_value=audit.current_section_sha or audit.signed_section_sha,
                )
            )

    mismatches = _dedupe_mismatches(mismatches)
    return VerifyRelayResult(
        clean=not mismatches,
        claims_checked=claims_checked,
        mismatches=mismatches,
        sources_consulted=sources_consulted,
    )
