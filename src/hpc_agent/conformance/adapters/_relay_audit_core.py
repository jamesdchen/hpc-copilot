"""The pre-delivery relay audit — the shared core for the two NON-hook shapes.

Capability 2 (relay/verbatim enforcement) decomposes into INSPECT (observe the
final agent-visible message) and ACT (force a continuation), and the contract
names TWO conforming shapes that provide it WITHOUT a Claude-Code ``Stop`` hook
(``docs/internals/harness-contract.md``, "Capability 2, split: INSPECT vs ACT"):

* a **response gateway** — an LLM proxy that runs ``verify_relay`` over the
  outgoing message BEFORE delivery and holds it back on a contradiction (ACT);
* an **OTel-GenAI** telemetry stream — a harness that reads the final message off
  standard GenAI observability spans and REPORTS a contradiction (INSPECT).

Both need the SAME question answered — "does this final message CONTRADICT the
durable journal?" — computed by exactly the audit the Stop hook runs, but driven
off the outgoing STRING rather than a transcript + Stop payload. This module is
that one shared answer, so the two shapes can never disagree with each other or
with ``verify_relay`` about what a contradiction is.

Faithful-by-construction: it runs the SAME public ``verify_relay`` /
``verify_notebook_relay`` the kit's ``relay_fixtures.reference_result`` and the
Stop hook run, keyed on the SAME public mention scans
(``relay_audit_stop.mentioned_run_ids`` / ``mentioned_audit_ids``) and the SAME
contradiction-kind set (``relay_fixtures.CONTRADICTION_KINDS``, the hook's own
``_CONTRADICTION_KINDS`` re-exported). The ``unverifiable`` kind is NOT a
contradiction (a final message legitimately carries numbers the record never
saw), exactly as the Stop hook excludes it.

Stdlib + hpc-agent PUBLIC surface only; pytest-free (the D-K1 kit boundary). No
private cross-package symbol is imported — the mention scans and the
contradiction-kind set are consumed through their public re-exports.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["RelayVerdict", "audit_final_message"]

# Cap how many mentioned runs / audits one audit pass verifies — the same cheap
# posture the Stop hook keeps (``_MAX_RUNS_AUDITED`` / ``_MAX_AUDITS_AUDITED``).
_MAX_RUNS = 5
_MAX_AUDITS = 5


class RelayVerdict(NamedTuple):
    """The pre-delivery audit outcome for one final agent-visible message.

    * ``contradicted`` — the message contradicts the durable journal (some
      mentioned run/audit yields a ``number`` / ``state`` / ``run_id`` mismatch);
    * ``kinds`` — the sorted-unique contradiction kinds found (telemetry detail);
    * ``reason`` — an itemized summary a gateway attaches when it holds the
      message back, or ``None`` when nothing contradicts.
    """

    contradicted: bool
    kinds: list[str]
    reason: str | None


def _runs_dir(experiment_dir: Path) -> Path:
    """``<journal home>/<repo_hash>/runs`` via the ONE public locator (no-scaffold)."""
    from hpc_agent.state.run_record import current_homedir, repo_hash

    return current_homedir() / repo_hash(experiment_dir) / "runs"


def _notebooks_dir(experiment_dir: Path) -> Path:
    """``<experiment>/.hpc/notebooks`` — raw path, never scaffolded."""
    return Path(experiment_dir).resolve() / ".hpc" / "notebooks"


def audit_final_message(experiment_dir: Path, final_message: str) -> RelayVerdict:
    """Audit *final_message* against *experiment_dir*'s journal, pre-delivery.

    Scans the message for the run ids and notebook audit ids it NAMES (a claim is
    only attributable to a run/audit the relay mentions), runs the public
    ``verify_relay`` / ``verify_notebook_relay`` per mention, and collects every
    CONTRADICTION-kind mismatch. Fail-open in full: an unresolvable namespace, an
    unreadable record, or any per-mention audit error contributes nothing — a
    broken audit degrades to "no contradiction found" (the verb-only posture),
    never an exception into the caller.
    """
    from hpc_agent._kernel.hooks.relay_audit_stop import mentioned_audit_ids, mentioned_run_ids
    from hpc_agent.conformance.relay_fixtures import CONTRADICTION_KINDS

    experiment_dir = Path(experiment_dir)
    run_ids = mentioned_run_ids(final_message, _runs_dir(experiment_dir))
    audit_ids = mentioned_audit_ids(final_message, _notebooks_dir(experiment_dir))

    kinds: set[str] = set()
    findings: list[str] = []
    _audit_runs(
        experiment_dir, final_message, run_ids[:_MAX_RUNS], CONTRADICTION_KINDS, kinds, findings
    )
    _audit_notebooks(
        experiment_dir, final_message, audit_ids[:_MAX_AUDITS], CONTRADICTION_KINDS, kinds, findings
    )

    if not findings:
        return RelayVerdict(contradicted=False, kinds=[], reason=None)
    reason = (
        "pre-delivery relay audit (conduct rule 10): the outgoing message "
        f"contradicts the durable records — {len(findings)} mismatch(es): " + "; ".join(findings)
    )
    return RelayVerdict(contradicted=True, kinds=sorted(kinds), reason=reason)


def _audit_runs(
    experiment_dir: Path,
    final_message: str,
    run_ids: Sequence[str],
    contradiction_kinds: frozenset[str],
    kinds: set[str],
    findings: list[str],
) -> None:
    """Verify each mentioned run and collect its contradiction-kind mismatches."""
    if not run_ids:
        return
    from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
    from hpc_agent.ops.decision.journal.verify_relay import verify_relay

    for run_id in run_ids:
        try:
            result = verify_relay(
                experiment_dir=experiment_dir,
                spec=VerifyRelayInput(run_id=run_id, relay_text=final_message),
            )
        except Exception:
            continue  # a run we cannot audit is a silent pass for that run
        for m in result.mismatches:
            if m.kind in contradiction_kinds:
                kinds.add(m.kind)
                findings.append(f"[{run_id}] {m.claim!r}: {m.detail}")


def _audit_notebooks(
    experiment_dir: Path,
    final_message: str,
    audit_ids: Sequence[str],
    contradiction_kinds: frozenset[str],
    kinds: set[str],
    findings: list[str],
) -> None:
    """Verify each mentioned notebook audit and collect its contradictions."""
    if not audit_ids:
        return
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    others = list(audit_ids)
    for audit_id in audit_ids:
        try:
            result = verify_notebook_relay(
                experiment_dir,
                audit_id,
                final_message,
                other_audit_ids=[a for a in others if a != audit_id],
            )
        except Exception:
            continue  # an audit we cannot check is a silent pass for that audit
        for m in result.mismatches:
            if m.kind in contradiction_kinds:
                kinds.add(m.kind)
                findings.append(f"[{audit_id}] {m.claim!r}: {m.detail}")
