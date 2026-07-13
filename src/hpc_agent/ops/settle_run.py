"""``settle-run`` — a human-directed terminal settle through the SAME machinery.

Run-12 finding 25 (``docs/design/history/run12-findings.md`` §25). Closing run 12
required journal surgery: completion was proven by two independent sources (a
foreground reporter RC=0 over all 2700 tasks; the result tree on disk) but the
framework path could not finish on the old wheel, so the human hand-edited the run
record (status → complete). It worked, but it BYPASSED ``harvest_on_terminal`` (no
summary pull, no transition stamp) and carried prose evidence instead of typed
counts.

The generator fix (upstream-fixes G2): every transition the system can make on
PROBED evidence must also be makeable on DIRECTED evidence through the SAME
machinery. ``settle-run`` does exactly what the reconcile settle arm does
(``ops/monitor/reconcile.py``) — ``update_run_status`` → ``mark_run`` →
transition-gated ``harvest_on_terminal`` — but keyed off human-directed evidence:

(a) journals the directed evidence as a DECISION (a sign-off with provenance);
(b) sets the terminal status via the SAME ``mark_run`` the probe path uses;
(c) runs the SAME transition-gated ``harvest_on_terminal`` (summary pull +
    transition stamp) — gated so an idempotent re-settle of an already-terminal
    run does NOT re-fire the harvest, exactly like the reconcile arm.

**The load-bearing guards** (each CAN fire): a missing run, a NON-terminal
``status``, and an EMPTY ``evidence`` are all refused — a directed settle with no
evidence is a bare status flip, which is the surgery this verb replaces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Callable

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.settle_run import SettleRunInput, SettleRunResult
from hpc_agent.cli._dispatch import CliShape, SchemaRef

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["settle_run"]


@primitive(
    name="settle-run",
    verb="workflow",
    composes=["mark-run-terminal", "aggregate-flow"],
    side_effects=[
        SideEffect(
            "writes-journal",
            "<experiment>/.hpc/decisions/run/<run_id>.jsonl (the directed-settle "
            "sign-off) + the run record's terminal status",
        ),
        SideEffect(
            "ssh",
            "<cluster> (harvest_on_terminal summary pull; best-effort, on a transition)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Human-directed terminal settle (run-12 finding 25): given directed "
            "terminal evidence, journal it as a sign-off, set the terminal status, "
            "and run the SAME mark_run + harvest_on_terminal transition the probe "
            "path runs — no journal surgery. Refuses a non-terminal status or empty "
            "evidence. The harvest fires only on a status TRANSITION (an idempotent "
            "re-settle does not re-pull)."
        ),
        spec_arg=True,
        spec_model=SettleRunInput,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="settle_run"),
    ),
    agent_facing=True,
)
def settle_run(
    experiment_dir: Path,
    *,
    spec: SettleRunInput,
    _aggregate: Callable[[Path, str], Any] | None = None,
    _sweep: Callable[[str], dict[int, list[str]]] | None = None,
) -> SettleRunResult:
    """Journal the directed evidence, set the terminal status, harvest.

    ``_aggregate`` / ``_sweep`` are injected seams forwarded to
    ``harvest_on_terminal`` (test-only; production leaves them at the defaults).
    """
    from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.ops.monitor.harvest_guard import harvest_on_terminal
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import load_run, mark_run, update_run_status

    run_id = spec.run_id
    status = spec.status

    # Guard 1: the run must exist.
    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.SpecInvalid(
            f"settle-run: no run record for run_id={run_id!r} — a directed settle sets "
            "the terminal state of an EXISTING run; there is nothing to settle for a run "
            "that was never submitted."
        )

    # Guard 2: the status must be terminal (settle-run only sets a TERMINAL state).
    if status not in {str(s) for s in TERMINAL_STATUSES}:
        raise errors.SpecInvalid(
            f"settle-run: status {status!r} is not terminal — settle-run only sets a "
            f"terminal state ({sorted(str(s) for s in TERMINAL_STATUSES)}). For a "
            "non-terminal correction use the monitor/reconcile path."
        )

    # Guard 3: directed evidence is required (an empty-evidence settle is a bare flip).
    evidence = spec.evidence.strip()
    if not evidence:
        raise errors.SpecInvalid(
            "settle-run: evidence is required — a directed settle journals WHAT proves the "
            "terminal state (e.g. 'foreground reporter RC=0 all-2700; result tree on disk'). "
            "A settle with no evidence is the surgical status-flip this verb replaces."
        )

    prior_status = str(getattr(record, "status", "") or "")

    # (a) Journal the directed evidence as a DECISION — the sign-off with provenance.
    decision = append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=run_id,
        block="settle-run",
        response="y",
        proposal=evidence,
        resolved={"status": status, "terminal_cause": status},
        provenance={
            "directed": True,
            "kind": "human-directed-settle",
            "evidence": evidence,
            "artifact_refs": list(spec.artifact_refs or []),
            "task_counts": dict(spec.task_counts or {}),
            "source": spec.provenance or "human-directed",
        },
    )

    # Record the typed evidence in last_status (the counts the prose hand-edit lacked).
    last_status: dict[str, Any] = {
        "verdict": status,
        "verdict_reason": "human_directed_settle",
        "verdict_source": "human_directed",
        "evidence": evidence,
        "artifact_refs": list(spec.artifact_refs or []),
        "checked_at": utcnow_iso(),
    }
    if spec.task_counts:
        last_status["task_counts"] = dict(spec.task_counts)
    update_run_status(experiment_dir, run_id, last_status=last_status)

    # (b) The SAME terminal transition the probe path runs.
    updated = mark_run(experiment_dir, run_id, status=status)

    # (c) The SAME transition-gated harvest — only fire when the status actually
    #     changed (an idempotent re-settle must not re-pull; each fire pays an rsync
    #     + reduce + a ledger append), identical to the reconcile settle arm.
    harvested = False
    harvest: dict[str, Any] = {}
    if status != prior_status:
        harvest = harvest_on_terminal(
            experiment_dir,
            run_id,
            terminal_cause=status,
            record=updated,
            _aggregate=_aggregate,
            _sweep=_sweep,
        )
        harvested = True

    stage = cast(
        Literal["settled", "already_terminal"],
        "settled" if status != prior_status else "already_terminal",
    )
    reason = f"settled {run_id!r} → {status} on directed evidence" + (
        " (harvest ran)" if harvested else " (already terminal — harvest not re-fired)"
    )
    return SettleRunResult(
        stage_reached=stage,
        run_id=run_id,
        status=status,
        prior_status=prior_status,
        harvested=harvested,
        harvest=harvest,
        decision_ts=str(decision.get("ts", "")),
        reason=reason,
    )
