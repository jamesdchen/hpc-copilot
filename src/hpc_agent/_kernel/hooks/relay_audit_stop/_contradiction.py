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

# Mismatch kinds that contradict the durable record (surfaced); the
# ``unverifiable`` kind is deliberately excluded (see package docstring). The
# notebook-audit relay (T11) deliberately REUSES these kinds — a wrong section
# status / module ``passed`` verdict is a ``state`` contradiction, a mismatched
# sha-hex a ``number`` one — so no new kind is added to the blocking set (and no
# wire-enum / schema change): the semantics stay coherent (a status IS a
# lifecycle-family claim; a sha is a value claim).
_CONTRADICTION_KINDS = frozenset({"number", "state", "run_id"})


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

        for run_id in run_ids[:_MAX_RUNS_AUDITED]:
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
                nearest = f" (journal: {m.nearest_source_value})" if m.nearest_source_value else ""
                violations.append(
                    _Violation(
                        scope_kind="run",
                        scope_id=run_id,
                        claim=m.claim,
                        journal_value=m.nearest_source_value,
                        text=f"[{run_id}] {m.claim!r}: {m.detail}{nearest}",
                    )
                )

    if audit_ids:
        from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

        for audit_id in audit_ids[:_MAX_AUDITS_AUDITED]:
            try:
                nb_result = verify_notebook_relay(experiment_dir, audit_id, relay_text)
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
