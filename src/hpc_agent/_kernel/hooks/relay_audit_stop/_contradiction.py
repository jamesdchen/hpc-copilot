"""Audit 1 — the rule-10 contradiction pass, and the violation gather.

``verify-relay`` mechanized rule 10 ("never relay numbers/state that don't match
the journal") as a pure audit verb; this drives it in-process for each mentioned
run and audit. :func:`_gather_violations` assembles the whole violation class in
its historical order: run rule-10, notebook rule-10, the paraphrase pass
(audit 3), then decision-state (audit 5).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from ._decision_state import _decision_state_findings
from ._paraphrase import _paraphrase_findings
from ._shared import _Violation

# Cap how many mentioned runs / audits one stop audits — the hook must stay cheap.
_MAX_RUNS_AUDITED = 5
_MAX_AUDITS_AUDITED = 5

# When a numeric claim genuinely matches NO source, cite the nearest source
# number only when it sits within this RELATIVE distance — beyond it a
# "(journal: X)" citation is a misleading unrelated neighbor, not evidence
# (run-14 finding: a fabricated relay drew "journal: 15.9" / "journal: 34" from
# records it had nothing to do with). Past the bound the correction stays
# "matches no source number" and cites none.
_NEAREST_CITE_REL_TOL = 0.1

# Mismatch kinds that contradict the durable record (surfaced); the
# ``unverifiable`` kind is deliberately excluded (see package docstring). The
# notebook-audit relay (T11) deliberately REUSES these kinds — a wrong section
# status / module ``passed`` verdict is a ``state`` contradiction, a mismatched
# sha-hex a ``number`` one — so no new kind is added to the blocking set (and no
# wire-enum / schema change): the semantics stay coherent (a status IS a
# lifecycle-family claim; a sha is a value claim).
_CONTRADICTION_KINDS = frozenset({"number", "state", "run_id"})


def _union_number_pool(experiment_dir: Path, run_ids: list[str]) -> tuple[set[str], list[float]]:
    """The UNION numeric corpus across every mentioned run (hook/verb parity).

    Routes through the verb's OWN ``collect_run_number_pool`` — the single corpus
    definition — so the hook can never disagree with ``verify-relay`` about which
    numbers a run legitimately sources. Fail-open per run: a run whose corpus
    cannot load simply contributes nothing.
    """
    from hpc_agent.ops.decision.journal.verify_relay import collect_run_number_pool

    strings: set[str] = set()
    floats: list[float] = []
    for run_id in run_ids:
        try:
            run_strings, run_floats = collect_run_number_pool(experiment_dir, run_id)
        except Exception:
            continue
        strings |= run_strings
        floats.extend(run_floats)
    return strings, floats


def _is_numeric_literal_claim(claim: str) -> bool:
    """True iff *claim* parses as a bare numeric value (not a word / phrase claim).

    The union suppression and near-citation gate apply only to literal numeric
    claims — a spelled-out number word (``nineteen``) or a zero-count phrase
    (``no failed``) keeps the verb's own verdict/citation untouched.
    """
    from hpc_agent.ops.decision.journal.verify_relay import normalize_num

    try:
        float(normalize_num(claim))
    except ValueError:
        return False
    return True


def _number_matches_union(claim: str, union_strings: set[str], union_floats: list[float]) -> bool:
    """True iff *claim* is supported by SOME mentioned run's number corpus."""
    from hpc_agent.ops.decision.journal.verify_relay import match_number

    return match_number(claim, union_strings, union_floats)


def _cite_nearest(claim: str, union_floats: list[float]) -> str | None:
    """The nearest union source number to cite for a surviving *claim*, or None.

    Cites only a value within :data:`_NEAREST_CITE_REL_TOL` relative distance —
    a far 'nearest' is a misleading unrelated neighbour, not evidence (defect 3).
    """
    from hpc_agent.ops.decision.journal.verify_relay import normalize_num

    if not union_floats:
        return None
    try:
        val = float(normalize_num(claim))
    except ValueError:
        return None
    nearest = min(union_floats, key=lambda f: abs(f - val))
    denom = max(abs(val), abs(nearest), 1e-9)
    if abs(nearest - val) / denom > _NEAREST_CITE_REL_TOL:
        return None
    return str(int(nearest)) if nearest == int(nearest) else str(nearest)


def _gather_violations(
    experiment_dir: Path, relay_text: str, run_ids: list[str], audit_ids: list[str]
) -> list[_Violation]:
    """Every violation-class finding (rule-10 + paraphrase + decision-state).

    The order is preserved from the pre-completer rejector: run rule-10, then
    notebook rule-10, then the paraphrase pass, then decision-state. Each helper
    is fail-open; the whole gather is additionally wrapped by the caller.
    """
    violations: list[_Violation] = []

    if run_ids:
        from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
        from hpc_agent.ops.decision.journal.verify_relay import verify_relay

        audited_runs = run_ids[:_MAX_RUNS_AUDITED]
        # The UNION numeric corpus across EVERY mentioned run (run-14 hook/verb
        # parity): ``verify_relay`` audits each run against ITS OWN pool, so a
        # reduce-table number a SIBLING run legitimately sources (its pulled
        # reduce artifacts) flags as a contradiction under a run whose scope never
        # loaded it — the exact divergence the verb (one, correctly-scoped run)
        # never hits. Pooling through the verb's own ``collect_run_number_pool``
        # lets the hook drop any number some mentioned run sources.
        union_strings, union_floats = _union_number_pool(experiment_dir, audited_runs)

        for run_id in audited_runs:
            try:
                result = verify_relay(
                    experiment_dir=experiment_dir,
                    spec=VerifyRelayInput(run_id=run_id, relay_text=relay_text),
                )
            except Exception:
                continue  # a run we cannot audit is a silent pass for that run
            for m in result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                # A numeric-literal claim SOME mentioned run legitimately sources
                # is never a contradiction — whether this run flagged it as a
                # ``number`` (no match in its own pool) OR, having job_ids, as a
                # job-id-shaped ``run_id`` (a reduce number like ``437839`` under a
                # sibling run). Both are the run-14 hook/verb corpus divergence.
                claim_numeric = _is_numeric_literal_claim(m.claim)
                if claim_numeric and _number_matches_union(m.claim, union_strings, union_floats):
                    continue
                nearest_value = m.nearest_source_value
                if m.kind == "number" and claim_numeric:
                    # A genuinely fabricated number: cite only a GENUINELY-near
                    # union neighbour, never a misleading unrelated one (defect 3).
                    nearest_value = _cite_nearest(m.claim, union_floats)
                nearest = f" (journal: {nearest_value})" if nearest_value else ""
                violations.append(
                    _Violation(
                        scope_kind="run",
                        scope_id=run_id,
                        claim=m.claim,
                        journal_value=nearest_value,
                        text=f"[{run_id}] {m.claim!r}: {m.detail}{nearest}",
                        kind=m.kind,
                    )
                )

    if audit_ids:
        from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

        audited = audit_ids[:_MAX_AUDITS_AUDITED]
        for audit_id in audited:
            # The OTHER mentioned audits are passed so a claim sitting nearer a
            # sibling's id is checked against ITS OWN journal, not this one's
            # (run-14 cross-scope guard: ``causal_tune_linear`` vs
            # ``causal_tune_tree`` share section slug names).
            others = [a for a in audited if a != audit_id]
            try:
                nb_result = verify_notebook_relay(
                    experiment_dir, audit_id, relay_text, other_audit_ids=others
                )
            except Exception:
                continue  # an audit we cannot check is a silent pass for that audit
            for m in nb_result.mismatches:
                if m.kind not in _CONTRADICTION_KINDS:
                    continue
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                violations.append(
                    _Violation(
                        scope_kind="notebook",
                        scope_id=audit_id,
                        claim=m.claim,
                        journal_value=m.nearest_source_value,
                        text=f"[{audit_id}] {m.claim!r}: {m.detail}{nearest}",
                        kind=m.kind,
                    )
                )

        # G1 — the paraphrase pass: relayed diff blocks in audit context must be
        # verbatim render content. Audit-scope with no per-claim value → an empty
        # ``claim`` (append-only by construction; never poisons a decision — the
        # sign-off boundary has its own gates).
        paraphrase = _paraphrase_findings(
            experiment_dir, relay_text, audit_ids[:_MAX_AUDITS_AUDITED]
        )
        for text in paraphrase:
            violations.append(
                _Violation(
                    scope_kind="notebook", scope_id="", claim="", journal_value=None, text=text
                )
            )

    # Decision-state claims — an unjournaled decision EVENT contradicts the record.
    with contextlib.suppress(Exception):
        violations.extend(_decision_state_findings(experiment_dir, relay_text, run_ids))
    return violations
