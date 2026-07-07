"""``aggregate-check`` / ``aggregate-run`` — the aggregate flow as blocks.

The aggregate flow, decomposed (docs/design/human-amplification-blocks.md §3)
to the finer grain of submit's S4 (submit-s4 wraps the whole aggregate flow at
coarse grain; these two blocks decompose aggregation itself). Each is a THIN
orchestrator that composes existing rings and TERMINATES at a human decision
point carrying code-digested evidence (a *brief*). No decision is resolved by
the LLM: code chains deterministically as far as it can, then hands back the
brief for the ``y``/nudge propose loop (§2).

* ``aggregate-check`` (readiness + integrity) — ``aggregate-preflight`` +
  the terminal-status readiness gate + the ``verify-aggregation-complete``
  integrity gate. Brief: which waves combined / what's missing / integrity
  issues found. Integrity issues are NEVER auto-masked — each is surfaced as a
  decision point carrying a conservative ``recommendation`` (§2, the #355
  doctrine: results/integrity are never silently massaged by the LLM).
  ``needs_decision`` is True when a readiness gate fails or an integrity issue
  exists; False (``ready``) when the run is clean to reduce.
* ``aggregate-run`` (combine + reduce + extract) — the deterministic
  ``aggregate-flow`` pipeline to a code-extracted results table + an error-sweep
  summary + an EMPTY ``proposed_interpretations`` slot the LLM fills at the
  terminator. Code extracts the results; the human concludes from them (§2).

Each block owns its invariants at the boundary (adding-a-primitive.md): it
validates the wire spec (the embedded models do the shape work) and fails loudly
via the composed rings. ``aggregate-run`` OWNS the terminal-or-explicitly-partial
invariant via the composed ``aggregate-flow`` gate — it does NOT assume
``aggregate-check`` ran first.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.aggregate_blocks import (
    AggregateBlockResult,
    AggregateCheckSpec,
    AggregateRunSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.block_chain import next_block_hint
from hpc_agent.ops.aggregate.invariants import verify_aggregation_complete
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.aggregate_preflight import aggregate_preflight
from hpc_agent.ops.block_gate import assert_greenlit_target
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import TERMINAL_STATUSES

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["aggregate_check", "aggregate_run"]


def _next_block(
    current_verb: str, stage_reached: str, why: str, **spec_hint: Any
) -> dict[str, Any] | None:
    """Delegate to the ``block_chain`` successor table (design §6/§8).

    Mirrors ``ops/submit_blocks._next_block``: the successor VERB is re-homed into
    ``block_chain.SUCCESSORS``; this thin helper keeps the emitted
    ``{verb, why, spec_hint}`` shape unchanged and returns ``None`` at a terminal /
    human-branch terminator.
    """
    return next_block_hint(current_verb, stage_reached, why=why, **spec_hint)


# ── check helpers ─────────────────────────────────────────────────────────────

# verify-aggregation-complete report fields folded verbatim into the brief so
# the human sees the raw invariant read, not just the derived issue list.
_INTEGRITY_REPORT_KEYS = (
    "ok",
    "all_waves_combined",
    "missing_waves",
    "all_tasks_present",
    "missing_tasks",
    "unexpected_tasks",
    "unexpected_aggregated_keys",
    "provenance_present",
    "columns_checked",
    "column_violations",
)


def _integrity_issues(vac: dict[str, Any], *, allow_partial: bool) -> list[dict[str, Any]]:
    """Turn each verify-aggregation-complete violation into a decision point.

    Every issue carries ``auto_masked: False`` (the load-bearing contract: an
    integrity problem is NEVER silently massaged — §2) plus a conservative
    ``recommendation`` the LLM drafts a proposal around and the human greenlights
    or nudges. ``missing_waves`` is the one issue whose recommendation bends to
    the operator's ``allow_partial`` stance; contamination / provenance / column
    issues always recommend investigation over a default.
    """
    issues: list[dict[str, Any]] = []

    missing_waves = vac.get("missing_waves") or []
    if missing_waves:
        issues.append(
            {
                "issue": "missing_waves",
                "detail": {"missing_waves": list(missing_waves)},
                "recommendation": (
                    "proceed with a partial aggregate (allow_partial set) — the "
                    "operator accepted the missing waves"
                    if allow_partial
                    else "refuse the partial aggregate; investigate the missing waves "
                    "(a partial usually masks a real cluster failure)"
                ),
                "auto_masked": False,
            }
        )

    missing_tasks = vac.get("missing_tasks") or []
    if missing_tasks:
        issues.append(
            {
                "issue": "missing_tasks",
                "detail": {"missing_tasks": list(missing_tasks)},
                "recommendation": (
                    "investigate — some tasks never wrote metrics into any wave "
                    "partial; the aggregate would be computed over a subset"
                ),
                "auto_masked": False,
            }
        )

    unexpected_tasks = vac.get("unexpected_tasks") or []
    if unexpected_tasks:
        issues.append(
            {
                "issue": "unexpected_tasks",
                "detail": {"unexpected_tasks": list(unexpected_tasks)},
                "recommendation": (
                    "investigate cross-run contamination — task ids in the pulled "
                    "partials that aren't in this run's wave_map; do not trust the "
                    "aggregate until resolved"
                ),
                "auto_masked": False,
            }
        )

    unexpected_keys = vac.get("unexpected_aggregated_keys") or []
    if unexpected_keys:
        issues.append(
            {
                "issue": "unexpected_aggregated_keys",
                "detail": {"unexpected_aggregated_keys": list(unexpected_keys)},
                "recommendation": (
                    "investigate post-reduce contamination — aggregated keys that "
                    "match no grid-point this run produced"
                ),
                "auto_masked": False,
            }
        )

    if vac.get("provenance_present") is False:
        issues.append(
            {
                "issue": "provenance_mismatch",
                "detail": {"provenance_present": False},
                "recommendation": (
                    "investigate — a pulled partial self-identifies with the wrong "
                    "run_id/wave; the partials may be stale or crossed"
                ),
                "auto_masked": False,
            }
        )

    column_violations = vac.get("column_violations") or []
    if column_violations:
        issues.append(
            {
                "issue": "column_violations",
                "detail": {"column_violations": list(column_violations)},
                "recommendation": (
                    "investigate — result files fail the declared schema "
                    "(missing columns / NaN metric); the metric may be wrong"
                ),
                "auto_masked": False,
            }
        )

    return issues


# ── run helpers ───────────────────────────────────────────────────────────────


def _results_table(aggregated_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Digest the reduced metrics into a code-extracted results table.

    ``aggregated_metrics`` maps a run_id / grid-point key → its metric dict. The
    table is a stable, row-per-key projection the LLM renders and proposes
    interpretations over — it never interprets the raw metrics itself (§2, the
    #355 doctrine extended from computing to concluding).
    """
    rows: list[dict[str, Any]] = []
    for key, metrics in sorted(aggregated_metrics.items()):
        row: dict[str, Any] = {"key": key}
        if isinstance(metrics, dict):
            row["metrics"] = metrics
        else:
            row["value"] = metrics
        rows.append(row)
    return rows


def _harvest_ledger_tail(experiment_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Return the last marker from the guaranteed-harvest ledger, if any.

    Wave-1's ``harvest_on_terminal`` sweeper appends a JSON line to
    ``<run_id>.harvest.jsonl`` at every terminal path (§5). aggregate-run
    references — never writes — that ledger so the brief carries the sweeper's
    corroborating evidence (what it already harvested at terminal) alongside the
    fresh reduce. Best-effort: a missing / unreadable ledger is simply ``None``.
    """
    path = harvest_marker_path(experiment_dir, run_id)
    if not path.is_file():
        return None
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        parsed = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ── aggregate-check ───────────────────────────────────────────────────────────


@primitive(
    name="aggregate-check",
    verb="workflow",
    composes=["aggregate-preflight", "verify-aggregation-complete"],
    side_effects=[
        SideEffect("ssh", "<cluster> (aggregate-preflight reconcile, when scheduler supplied)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Aggregate block CHECK (readiness + integrity): aggregate-preflight "
            "+ the terminal-status readiness gate + the verify-aggregation-"
            "complete integrity gate. Brief = which waves combined, what's "
            "missing, integrity issues found — each surfaced as a decision point "
            "with a conservative recommendation, NEVER auto-masked. Terminates → "
            "y/nudge when a gate fails or an issue exists; else 'ready' → run."
        ),
        spec_arg=True,
        spec_model=AggregateCheckSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="aggregate_check", output="aggregate_block"),
    ),
    agent_facing=True,
)
def aggregate_check(experiment_dir: Path, *, spec: AggregateCheckSpec) -> AggregateBlockResult:
    """Readiness + integrity block: preflight → readiness gate → integrity gate.

    Ends at the first decision point with a full brief for the ``y``/nudge loop:
    ``not_ready`` (run not terminal / preflight failed / no journal record),
    ``integrity_review`` (verify-aggregation-complete surfaced issues — each a
    NEVER-auto-masked decision point with a recommendation), or ``ready`` (clean:
    ``needs_decision`` False, greenlight straight to aggregate-run). The integrity
    gate runs only once a local ``_combiner/`` exists (post-pull / re-check); a
    pre-run check where nothing is pulled yet reports ``integrity_checked=False``
    — aggregate-run verifies integrity itself after the pull.
    """
    run_id = spec.run_id
    brief: dict[str, Any] = {"run_id": run_id}

    # 1. Preflight (optional) — fold pass/fail into the brief.
    preflight_pass = True
    if spec.run_preflight:
        pf = aggregate_preflight(
            experiment_dir=experiment_dir,
            reconcile_scheduler=spec.reconcile_scheduler,
        )
        brief["preflight"] = {"overall": pf.get("overall")}
        preflight_pass = pf.get("overall") == "pass"

    # 2. Readiness gate — the run must exist and be terminal before a reduce is
    #    safe. The block DIGESTS this (aggregate-run enforces it by raising); a
    #    non-terminal run is a decision point (reconcile? keep watching?).
    record = load_run(experiment_dir, run_id)
    terminal = record is not None and record.status in TERMINAL_STATUSES
    brief["record_found"] = record is not None
    brief["status"] = record.status if record is not None else None
    brief["terminal"] = terminal
    brief["combined_waves"] = list(record.combined_waves) if record is not None else []
    brief["failed_waves"] = list(record.failed_waves) if record is not None else []

    # 3. Integrity gate — best-effort. verify-aggregation-complete needs a local
    #    ``_combiner/`` (raises SpecInvalid when nothing is pulled yet). A pre-run
    #    check hits that path and reports integrity_checked=False; aggregate-run
    #    then verifies after its own pull. When the combiner IS present (re-check
    #    / post-harvest), surface every violation as a never-auto-masked decision.
    integrity_checked = False
    integrity_issues: list[dict[str, Any]] = []
    integrity_report: dict[str, Any] | None = None
    try:
        vac = verify_aggregation_complete(experiment_dir, run_id=run_id)
        integrity_checked = True
        integrity_report = {k: vac.get(k) for k in _INTEGRITY_REPORT_KEYS}
        integrity_issues = _integrity_issues(vac, allow_partial=spec.allow_partial)
    except errors.SpecInvalid:
        # Combiner not pulled yet (pre-run check) — nothing to verify or mask
        # here; aggregate-run owns the post-pull integrity verification.
        integrity_checked = False
    brief["integrity_checked"] = integrity_checked
    brief["integrity_report"] = integrity_report
    brief["integrity_issues"] = integrity_issues  # never auto-masked (§2)

    # 4. Terminate. Readiness gate first (can't reduce a non-terminal run), then
    #    integrity. ``missing_waves`` under allow_partial is surfaced but not
    #    blocking; every other issue blocks.
    if record is None or not terminal or not preflight_pass:
        return AggregateBlockResult(
            block="check",
            stage_reached="not_ready",
            needs_decision=True,
            reason=(
                "no journal record — check the run_id"
                if record is None
                else f"run is {record.status!r}, not terminal; reconcile or keep watching"
                if not terminal
                else "aggregate-preflight failed; resolve before reducing"
            ),
            run_id=run_id,
            brief=brief,
        )

    blocking = [
        i for i in integrity_issues if not (i["issue"] == "missing_waves" and spec.allow_partial)
    ]
    if blocking:
        return AggregateBlockResult(
            block="check",
            stage_reached="integrity_review",
            needs_decision=True,
            reason=(
                f"{len(blocking)} integrity issue(s) need a decision; each carries a "
                "conservative recommendation and is never auto-masked."
            ),
            run_id=run_id,
            brief=brief,
        )

    return AggregateBlockResult(
        block="check",
        stage_reached="ready",
        needs_decision=False,
        reason=(
            "terminal, preflight clean, no blocking integrity issues; greenlight to aggregate-run."
        ),
        run_id=run_id,
        brief=brief,
        next_block=_next_block(
            "aggregate-check",
            "ready",
            "run is clean to reduce; combine, reduce, and extract the results table.",
            run_id=run_id,
        ),
    )


# ── aggregate-run ─────────────────────────────────────────────────────────────


@primitive(
    name="aggregate-run",
    verb="workflow",
    composes=["aggregate-flow"],
    side_effects=[
        SideEffect("ssh", "<cluster> (wave combine + rsync pull)"),
        SideEffect("sync-pull", "<ssh_target>:<remote_path> -> <experiment_dir>/_aggregated/"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.PreconditionFailed,
        errors.JournalCorrupt,
    ],
    idempotent=True,
    idempotency_key="aggregate.run_id",
    cli=CliShape(
        help=(
            "Aggregate block RUN (combine + reduce + extract): the deterministic "
            "aggregate-flow pipeline → a code-extracted results table + error-"
            "sweep summary + an EMPTY proposed_interpretations slot the LLM fills "
            "at the terminator. Owns the terminal-or-explicitly-partial invariant "
            "via aggregate-flow's gate (never assumes aggregate-check ran). "
            "Terminates → y/nudge (harvested / harvest_partial)."
        ),
        spec_arg=True,
        spec_model=AggregateRunSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="aggregate_run", output="aggregate_block"),
    ),
    agent_facing=True,
)
def aggregate_run(experiment_dir: Path, *, spec: AggregateRunSpec) -> AggregateBlockResult:
    """Combine + reduce + extract block: aggregate-flow → results table → propose.

    Runs the existing ``aggregate-flow`` (ensure waves combined → pull partials →
    reduce) and digests the reduced metrics into a stable results table. The brief
    carries the table, an error-sweep summary, the guaranteed-harvest ledger tail
    (wave-1's ``harvest_on_terminal`` corroboration), and an EMPTY
    ``proposed_interpretations`` slot the LLM fills at the ``y``/nudge boundary —
    code extracts the results; the human concludes from them (§2). Results are
    never interpreted raw by the LLM.

    aggregate-run owns the terminal-or-explicitly-partial invariant: the composed
    ``aggregate-flow`` gate raises ``PreconditionFailed`` for a non-terminal run
    unless ``ensure_all_combined=false`` (the deliberate-partial opt-in). This
    block does NOT assume ``aggregate-check`` established it.

    Precondition gate (design §2): the latest journaled decision for this run must
    be a greenlight naming ``aggregate-run`` — the human greenlit
    ``aggregate-check``'s ready brief. The terminal-or-explicitly-partial
    invariant is left to the composed ``aggregate-flow`` gate (compose, don't
    duplicate).
    """
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.aggregate.run_id,
        verb="aggregate-run",
        predecessor="aggregate-check",
    )
    agg = aggregate_flow(experiment_dir, spec=spec.aggregate)

    brief: dict[str, Any] = {
        "run_id": agg.run_id,
        "results_table": _results_table(agg.aggregated_metrics),
        "combined_waves": agg.combined_waves,
        "failed_waves": agg.failed_waves,
        # Code-extracted error sweep — the deterministic failure digest the human
        # sizes their interpretation against (never the LLM's read of raw logs).
        "error_sweep": {
            "escalation_reason": agg.escalation_reason,
            "nonempty_failing_task_ids": agg.nonempty_failing_task_ids,
            "column_violations": agg.column_violations,
        },
        "harvest_ledger": _harvest_ledger_tail(experiment_dir, agg.run_id),
        # The slot the LLM fills with proposed interpretations at y/nudge — the
        # code hands over an EMPTY list; concluding is the human's decision (§2).
        "proposed_interpretations": [],
    }
    # Per-scope PRIOR look counts recorded by the composed reduction (T3): copy
    # verbatim, the framework interprets nothing. Key ABSENT (not None) for a
    # scope-less run so a scope-less brief stays byte-identical to pre-T3. The
    # scope GATE (ScopeLocked refusal + the look-ledger side effect) fires ONCE,
    # inside the composed ``aggregate-flow`` — aggregate-run has no pre-flow seam
    # analogous to submit-s4's pre-detach gate, and it never detaches, so there
    # is no ordering hazard: the flow's gate + ledger write cover this block.
    if agg.scope_looks is not None:
        brief["scope_looks"] = agg.scope_looks

    partial = bool(agg.escalation_reason) or bool(agg.failed_waves)
    return AggregateBlockResult(
        block="run",
        stage_reached="harvest_partial" if partial else "harvested",
        needs_decision=True,
        reason=(
            "partial harvest — some waves escalated; review the results table."
            if partial
            else "harvest complete; review the results table and choose an interpretation."
        ),
        run_id=agg.run_id,
        brief=brief,
    )
