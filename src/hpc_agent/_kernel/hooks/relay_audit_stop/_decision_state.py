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
    findings: list[_Violation] = []
    for scope_kind, scope_id in scopes:
        if len(findings) >= _MAX_STATE_CLAIM_FINDINGS:
            break
        pos_here = False
        neg_here = False
        for line in relay_lines:
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
                )
            )
    return findings
