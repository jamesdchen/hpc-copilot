"""Audit 6 — experiment-scope COMPLETENESS claims (the incident this closes).

verify-relay and the decision-state pass both audit a claim ONLY where the relay
NAMES a run/audit/campaign scope — a claim binds no journal record otherwise. But
a session's final message can assert whole-EXPERIMENT completeness ("both fleets
are drained, all runs journaled, monitors retired") while naming NO run id, so no
per-scope detector could ever fire — and one run (``causal_tune_tree_xgb-0b5ef197``)
was journaled ``in_flight`` under exactly that claim.

This pass closes that gap. It matches a small, conservative lexicon of
experiment-scope completeness assertions in the final text, and — on a hit —
routes THROUGH the journal's own non-terminal scans (``state/index.py``) to build
the witness set: the runs that are still live/pre-dispatch RIGHT NOW. A non-empty
witness set contradicts the claim. Deliberately conservative (a false completeness
correction is worse than a miss): the lexicon REQUIRES an ``all`` / ``both`` /
``everything`` / ``fleet`` quantifier, so a single-run phrasing ("run X is
complete") never fires. Fail-open: any exception → no finding.

Two refinements keep the posture honest (a false correction is worse than a miss,
so we suppress on ANY doubt):

* **Sentence-level suppression.** The bare regex fires on shapes that only look
  like a claim — negations ("*not* all runs are complete"), questions ("are all
  runs complete?"), futures/conditionals ("*once* all runs are complete, we
  will…"), the duration sense ("all runs complete *in under* 5 minutes"), and
  quoted intent ("the *goal* is all runs journaled"). After a lexical hit we
  extract the sentence around the match and SUPPRESS on any of those governors,
  scanning for the FIRST match the sentence context does NOT excuse.

* **Fleet-phrase witness widening.** A ``fleet`` phrase ("both fleets drained")
  asserts completeness across the WHOLE machine, so its witness is every
  journaled experiment (``discover_fleet_experiments``), each witness stamped
  with its experiment dir. A plain ``all runs`` phrase is a cwd-scoped claim, so
  its witness stays cwd-only — a sibling repo's unrelated live run must never
  block a claim that never reached beyond this experiment.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._shared import _Violation

# A small, conservative lexicon of EXPERIMENT-SCOPE completeness assertions. Each
# alternative REQUIRES a whole-scope quantifier (``all`` / ``both`` / ``fleet`` /
# ``everything``) so a single-run claim ("run X is complete") is NOT matched — the
# detector prefers a miss to a false correction. Whitespace is ``\s+`` so a claim
# wrapped across lines still matches; the whole match (``group(0)``) is quoted
# back verbatim (whitespace-normalized) in the finding. Case-insensitive.
_COMPLETENESS_RE = re.compile(
    r"\b(?:"
    r"all\s+runs?\s+(?:are\s+)?"
    r"(?:journaled|complete|completed|drained|terminal|harvested|settled|finished|done)"
    r"|both\s+fleets?\s+(?:are\s+)?drained"
    r"|fleets?\s+(?:are\s+)?drained"
    r"|everything\s+(?:is\s+)?(?:settled|terminal|done|journaled)"
    r")\b",
    re.IGNORECASE,
)

# Sentence-level suppression governors. Each is checked against the sentence
# containing a lexical hit; ANY match excuses the hit (suppress on doubt).
_SENTENCE_BOUNDARY = ".!?\n"

# (a) negation somewhere BEFORE the match in the sentence ("not all runs …",
# "haven't confirmed all runs …"). ``n't`` catches every contraction; the bare
# words are word-bounded so "another"/"cannot" do not trip it.
_NEGATION_RE = re.compile(r"\bnot\b|n't|\bnever\b|\bwithout\b|\byet\s+to\b", re.IGNORECASE)

# (b) interrogative — the sentence terminates in ``?`` OR opens with an
# interrogative auxiliary ("are all runs complete?").
_INTERROGATIVE_OPENER_RE = re.compile(
    r"(?:are|is|have|has|had|did|do|does|will|would|can|could|should)\b",
    re.IGNORECASE,
)

# (c) conditional / temporal opener BEFORE the match ("once all runs are complete,
# we will …", "until all runs drain …") — the claim is about a future state.
_CONDITIONAL_RE = re.compile(
    r"\b(?:once|when|until|unless|if|after|before|as\s+soon\s+as)\b",
    re.IGNORECASE,
)

# (d) duration idiom immediately AFTER the matched phrase ("all runs complete in
# under 5 minutes") — "complete" is the verb sense, not a state assertion.
_DURATION_RE = re.compile(r"\s*(?:in\s+under|in\s+less\s+than|within|in\s+\d)", re.IGNORECASE)

# (e) intent / plan governor BEFORE the match ("the goal is all runs journaled",
# "waiting for all runs to drain") — a stated aim, not a claim of fact.
_INTENT_RE = re.compile(
    r"\b(?:goal|plan|aim|want|wanted|expect|hope|hoping|need|"
    r"waiting\s+for|working\s+toward)\b",
    re.IGNORECASE,
)

# Cap how many witness run ids the finding names inline (the rest are summarized
# as ``+N more``) so one pathological fleet cannot flood the block reason.
_MAX_WITNESS_RUN_IDS = 8


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int, str]:
    """Return ``(left, right, trailing)`` for the sentence enclosing ``[start, end)``.

    ``left``/``right`` bracket the sentence (split conservatively on ``.!?\\n``);
    ``trailing`` is the boundary character that TERMINATED the sentence (``""`` at
    end of text) so an interrogative ``?`` terminator is detectable — the split
    itself excludes the boundary char from the slice.
    """
    left = start
    while left > 0 and text[left - 1] not in _SENTENCE_BOUNDARY:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _SENTENCE_BOUNDARY:
        right += 1
    trailing = text[right] if right < len(text) else ""
    return left, right, trailing


def _match_is_suppressed(text: str, match: re.Match[str]) -> bool:
    """True when the sentence context excuses a lexical hit (suppress on doubt).

    Applies the five governors — negation / interrogative / conditional-temporal /
    duration / intent — scoped to the sentence around the match. A false
    completeness correction is worse than a miss, so ANY governor suppresses.
    """
    left, right, trailing = _sentence_bounds(text, match.start(), match.end())
    sentence = text[left:right]
    pre = text[left : match.start()]
    post = text[match.end() : right]

    # (a) negation before the match
    if _NEGATION_RE.search(pre):
        return True
    # (b) interrogative sentence
    if trailing == "?" or _INTERROGATIVE_OPENER_RE.match(sentence.strip()):
        return True
    # (c) conditional / temporal opener before the match
    if _CONDITIONAL_RE.search(pre):
        return True
    # (d) duration idiom immediately after the matched phrase
    if _DURATION_RE.match(post):
        return True
    # (e) intent / plan governor before the match
    return bool(_INTENT_RE.search(pre))


def _live_match(relay_text: str) -> re.Match[str] | None:
    """The first lexical hit whose sentence context does NOT excuse it (or None).

    Iterating (not ``search``) lets a genuinely live claim later in the text still
    fire even when an earlier hit is a suppressed negation/question/etc.
    """
    for match in _COMPLETENESS_RE.finditer(relay_text):
        if not _match_is_suppressed(relay_text, match):
            return match
    return None


def _cwd_witnesses(experiment_dir: Path) -> list[tuple[str, None]]:
    """Non-terminal runs of THIS experiment only (the plain ``all runs`` scope)."""
    from hpc_agent.state.index import find_in_flight_runs, find_submitting_runs

    return [
        (r.run_id, None)
        for r in (*find_in_flight_runs(experiment_dir), *find_submitting_runs(experiment_dir))
    ]


def _fleet_witnesses(experiment_dir: Path) -> list[tuple[str, str]]:
    """Non-terminal runs across EVERY journaled experiment (the ``fleet`` scope).

    A ``fleet`` phrase asserts machine-wide completeness, so the witness widens to
    every namespace ``discover_fleet_experiments`` finds — each witness stamped
    with its experiment dir so a foreign live run is legible in the finding.
    Fail-open: if fleet discovery is unavailable, fall back to the cwd witness.
    """
    from hpc_agent.state.index import (
        discover_journaled_experiments,
        find_in_flight_runs,
        find_submitting_runs,
    )

    experiments, _skipped = discover_journaled_experiments()
    if not experiments:
        # No discovered namespace (e.g. no journal home) — degrade to cwd so a
        # fleet phrase is never LESS strict than a plain one on the same journal.
        return [(rid, str(experiment_dir)) for rid, _ in _cwd_witnesses(experiment_dir)]
    out: list[tuple[str, str]] = []
    for exp in experiments:
        for r in (*find_in_flight_runs(exp), *find_submitting_runs(exp)):
            out.append((r.run_id, str(exp)))
    return out


def _completeness_findings(experiment_dir: Path, relay_text: str) -> list[_Violation]:
    """Flag an experiment-scope completeness claim the journal contradicts.

    On a NON-SUPPRESSED lexicon hit, the witness set is the non-terminal runs the
    claim spans — routed through ``state/index.py``'s own scans
    (``find_in_flight_runs`` + ``find_submitting_runs``), never re-derived (the
    attention-queue D5 route-through discipline). A ``fleet`` phrase widens the
    witness to every journaled experiment (``discover_fleet_experiments``); a
    plain ``all runs`` phrase stays cwd-scoped. A non-empty witness contradicts
    the claim → one :class:`_Violation` at ``scope_kind="experiment"`` /
    ``scope_id=""`` (the paraphrase-precedent empty scope id), ``kind="state"``
    (REUSING the existing contradiction kind — no wire/enum change). Because the
    claim binds no run id, this runs even when the relay names none — the exact
    incident shape. Fail-open: any exception → no finding.
    """
    match = _live_match(relay_text)
    if match is None:
        return []  # no live completeness vocabulary (or every hit was suppressed)

    phrase = " ".join(match.group(0).split())
    is_fleet = "fleet" in phrase.lower()

    try:
        witnesses = _fleet_witnesses(experiment_dir) if is_fleet else _cwd_witnesses(experiment_dir)
    except Exception:
        return []  # any scan error → no finding (fail-open)

    if not witnesses:
        return []  # the claim is TRUE — every run really is terminal — no finding

    # Stamp fleet witnesses with their experiment dir; plain witnesses stay bare.
    def _label(run_id: str, exp: str | None) -> str:
        return f"{run_id} ({exp})" if exp is not None else run_id

    entries = sorted(_label(rid, exp) for rid, exp in witnesses)
    shown = entries[:_MAX_WITNESS_RUN_IDS]
    more = len(entries) - len(shown)
    listing = ", ".join(shown) + (f" +{more} more" if more else "")
    label = Path(experiment_dir).name or "experiment"
    return [
        _Violation(
            scope_kind="experiment",
            scope_id="",
            claim="all runs terminal",
            journal_value=None,
            text=(
                f'[{label}] completeness claim ("{phrase}") contradicts the journal — '
                f"{len(entries)} run(s) non-terminal: {listing}"
            ),
            kind="state",
        )
    ]
