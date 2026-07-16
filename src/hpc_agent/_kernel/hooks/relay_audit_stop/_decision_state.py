"""Audit 5 — decision-state claims (an unjournaled decision EVENT, queue item 5).

verify-relay audits a run's numbers/status; this pass audits a claim about a
DECISION EVENT — "revoked", "superseded", "greenlit", "journaled" — against the
scope's decision journal. An unsupported claim joins the rule-10 findings. See
the package docstring's "Decision-state claims" section for the matching rules.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._shared import _Violation

# A small, conservative lexicon of PAST-TENSE assertions that a decision event
# happened. Word-boundary matched (so "unjournaled" does not read as a
# "journaled" claim). Positive verbs assert a decision was recorded/approved;
# the revocation verbs assert a prior decision no longer stands.
_DECISION_STATE_POSITIVE_RE = re.compile(r"\b(?:greenlit|greenlighted|journaled)\b", re.IGNORECASE)
_DECISION_STATE_NEGATIVE_RE = re.compile(r"\b(?:revoked|superseded)\b", re.IGNORECASE)
_MAX_STATE_CLAIM_FINDINGS = 5


def _quoted_line_indices(lines: list[str]) -> set[int]:
    """Indices of relay lines inside a blockquote (``> ...``) or code fence.

    Quote MARKUP alone does not excuse a line from decision-state attribution —
    a fenced line is excluded only when it is a GENUINE quote, i.e. its text
    verbatim-matches the scope's own persisted brief (finding 8e's live case).
    Otherwise fencing a fresh assertion would silently bypass the whole
    decision-event audit — the self-quote laundering class (the run-10
    "nineteen" evasion's shape). The genuine-quote check is
    :func:`_is_genuine_quote`; this pass only finds the candidate lines.
    """
    quoted: set[int] = set()
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            quoted.add(i)  # the fence marker line itself carries no claim
            continue
        if in_fence or stripped.startswith(">"):
            quoted.add(i)
    return quoted


def _normalize_quote(text: str) -> str:
    """Whitespace-collapsed, casefolded, markup-stripped form for quote matching."""
    return " ".join(text.replace("`", " ").lstrip(" >").split()).casefold()


def _brief_quote_blob(experiment_dir: Path, run_ids: list[str]) -> str:
    """Normalized concatenation of every string the scopes' persisted briefs carry.

    The brief store is the gate's own rendered proposal text — the only thing a
    relay can legitimately QUOTE. Fail-open to empty (no readable briefs → no
    line is excused, the audit runs as if unquoted — the safe direction).
    """
    try:
        from hpc_agent.state.decision_briefs import read_briefs
    except Exception:
        return ""
    parts: list[str] = []

    def _walk(value: object) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                _walk(v)

    for rid in run_ids:
        try:
            for record in read_briefs(experiment_dir, rid):
                _walk(record)
        except Exception:
            continue
    return _normalize_quote("\n".join(parts))


def _is_genuine_quote(line: str, brief_blob: str) -> bool:
    """True when the quoted *line*'s content really appears in the briefs' text.

    An empty normalized line (bare fence marker, lone ``>``) carries no claim
    and is trivially genuine; anything else must be a verbatim substring of the
    scopes' persisted brief text. A fenced FABRICATION therefore attributes
    normally — accounting by construction, no silent bypass.
    """
    norm = _normalize_quote(line)
    if not norm:
        return True
    return bool(brief_blob) and norm in brief_blob


def _decision_state_findings(
    experiment_dir: Path, relay_text: str, run_ids: list[str]
) -> list[_Violation]:
    """Flag decision-state claims no journal record supports (queue item 5).

    A decision-state verb is only attributable to a scope the relay NAMES (the
    rule-10 discipline): candidate scopes are the mentioned runs plus any
    mentioned campaign. The verb and the scope id must share a LINE, and the
    scope's decision journal must support the claim — a positive verb needs a
    committed ``y`` greenlight; a revocation/supersession verb needs the journal
    to show that greenlight no longer standing (or nothing to revoke at all).
    Fail-open per scope; capped; a scope-less claim is a deliberate miss.

    Returns :class:`_Violation`s: the ``claim`` carries the matched verb category
    (so the completer's poisoned-decision intersection can test it against a
    pending brief), ``text`` the verbatim rejector line.
    """
    has_pos = _DECISION_STATE_POSITIVE_RE.search(relay_text)
    has_neg = _DECISION_STATE_NEGATIVE_RE.search(relay_text)
    if not has_pos and not has_neg:
        return []  # fast path: no decision-state vocabulary anywhere

    scopes: list[tuple[str, str]] = [("run", rid) for rid in run_ids]
    try:
        campaign_ids = sorted(
            p.parent.name
            for p in (Path(experiment_dir) / ".hpc" / "campaigns").glob("*/decisions.jsonl")
        )
        scopes += [("campaign", c) for c in campaign_ids if c and c in relay_text]
    except OSError:
        pass
    if not scopes:
        return []  # attributable to no journaled scope — conservative miss

    from hpc_agent.state.decision_journal import is_latest_committed_greenlight, read_decisions

    relay_lines = relay_text.splitlines()
    quoted = _quoted_line_indices(relay_lines)
    brief_blob = _brief_quote_blob(Path(experiment_dir), run_ids) if quoted else ""
    findings: list[_Violation] = []
    for scope_kind, scope_id in scopes:
        if len(findings) >= _MAX_STATE_CLAIM_FINDINGS:
            break
        pos_here = False
        neg_here = False
        for line_no, line in enumerate(relay_lines):
            if line_no in quoted and _is_genuine_quote(line, brief_blob):
                continue  # a GENUINE quote of the scope's own brief is not a fresh claim (8e)
            if scope_id not in line:
                continue  # proximity: the verb must share the scope id's line
            if _DECISION_STATE_POSITIVE_RE.search(line):
                pos_here = True
            if _DECISION_STATE_NEGATIVE_RE.search(line):
                neg_here = True
        if not pos_here and not neg_here:
            continue
        try:
            records = read_decisions(experiment_dir, scope_kind, scope_id)
            standing = is_latest_committed_greenlight(experiment_dir, scope_kind, scope_id)
        except Exception:
            continue  # a scope we cannot read is a silent pass
        has_greenlight = any(r.get("response") == "y" for r in records)
        # A genuine supersession is journaled on the RUN RECORD (ops/supersession
        # stamps ``superseded_by`` — the durable evidence), NOT as a decision
        # record, so a truthful "run X was superseded" must read as supported:
        # the decision journal's standing greenlight is the launch approval, not
        # a contradiction of the later closure.
        superseded_evidence = False
        if scope_kind == "run":
            try:
                from hpc_agent.state.journal import load_run

                rec = load_run(experiment_dir, scope_id)
                superseded_evidence = bool(rec is not None and rec.superseded_by)
            except Exception:
                superseded_evidence = False
        if pos_here and not has_greenlight and len(findings) < _MAX_STATE_CLAIM_FINDINGS:
            findings.append(
                _Violation(
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    claim="greenlit journaled",
                    journal_value=None,
                    text=(
                        f"[{scope_id}] decision-state claim (greenlit/journaled) has no "
                        "committed greenlight in the decision journal"
                    ),
                    kind="state",
                )
            )
        if (
            neg_here
            and not superseded_evidence
            and (not records or standing)
            and len(findings) < _MAX_STATE_CLAIM_FINDINGS
        ):
            detail = (
                "the latest decision is a standing greenlight, not a revocation"
                if standing
                else "there is no decision record at all"
            )
            findings.append(
                _Violation(
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    claim="revoked superseded",
                    journal_value=None,
                    text=(
                        f"[{scope_id}] decision-state claim (revoked/superseded) has no "
                        f"supporting journal record — {detail}"
                    ),
                    kind="state",
                )
            )
    return findings
