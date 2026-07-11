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
from hpc_agent.ops.scope_gate import assert_scopes_unlocked
from hpc_agent.state.block_terminal import terminal_block_key
from hpc_agent.state.journal import load_run
from hpc_agent.state.run_record import TERMINAL_STATUSES

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["aggregate_check", "aggregate_run"]


# The block-terminal store, the detached lease, and the doctor dead-worker scan
# all key a detached aggregate-run under its VERB ("aggregate-run") — the SAME
# string ``_spawn_detached`` stamps into the lease. Sourced from the ONE key
# derivation (:func:`state.block_terminal.terminal_block_key`) so this recorder
# can never drift from the replay reader / doctor scan (the verb is already
# canonical, so this is an identity call that documents the shared seam).
_AGG_RUN_BLOCK_KEY = terminal_block_key("aggregate-run")


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
    "wave_map_present",
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
    # Scan BACKWARD for the newest PARSEABLE marker. A crash mid-append can leave
    # a torn final line; the whole-line-atomic append seam keeps every *prior*
    # line intact, so a torn tail falls back to the last good marker rather than
    # stranding a finished run's harvest evidence. Only an entirely-unparseable
    # ledger yields None.
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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


# ── aggregate-run detach-by-contract helpers (design §3; run-#10 F-K) ─────────


def _agg_run_cmd_sha(experiment_dir: Path, run_id: str) -> str:
    """The run's tree fingerprint (``cmd_sha``) from its sidecar, or ``""``.

    The identity a terminal replay is keyed on (mirrors
    ``ops/submit_blocks._current_cmd_sha`` / ``ops/status_blocks._watch_cmd_sha``):
    a nudge that re-resolves the run rewrites the sidecar ``cmd_sha``, so a
    mismatch is "the tree moved → do not replay a stale outcome". An
    unreadable/absent sidecar yields ``""`` → the replay refuses (re-execute).
    """
    from hpc_agent.state.runs import read_run_sidecar

    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (OSError, ValueError, errors.HpcError):
        return ""
    return str((sidecar or {}).get("cmd_sha") or "")


def _detached_agg_run_spec_dict(spec: AggregateRunSpec) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME aggregate-run body synchronously (its harvest IS the
    point), so its spec must carry ``detach=False`` — a truthy detach would fork
    forever (mirrors ``ops/submit_blocks._detached_spec_dict``).
    """
    return spec.model_copy(update={"detach": False}).model_dump(mode="json")


def _replay_agg_run_terminal(experiment_dir: Path, run_id: str) -> AggregateBlockResult | None:
    """Return a finished aggregate-run worker's recorded terminal for the CURRENT
    tree, else ``None`` (run #7 idempotent re-invoke).

    Replays ONLY when the current sidecar ``cmd_sha`` equals the one recorded with
    the terminal — proof the outcome still applies. A moved/absent ``cmd_sha`` (a
    nudge), an absent record, or a corrupt record all return ``None`` so the caller
    re-executes (never replays a possibly-stale harvest). The replayed result was
    already finalized on first completion, so the caller returns it as-is.
    """
    from hpc_agent.state.block_terminal import read_terminal

    record = read_terminal(experiment_dir, run_id, _AGG_RUN_BLOCK_KEY)
    if record is None:
        return None
    current_sha = _agg_run_cmd_sha(experiment_dir, run_id)
    if not current_sha or str(record.get("cmd_sha") or "") != current_sha:
        return None
    try:
        return AggregateBlockResult.model_validate(record["result"])
    except (KeyError, TypeError, ValueError):
        return None


def _record_agg_run_terminal(experiment_dir: Path, result: AggregateBlockResult) -> None:
    """Record a genuine aggregate-run terminal so a re-invoke replays it.

    Called on the harvested / harvest_partial terminals (the detached handle,
    ``stage_reached="detached"``, is not terminal and is never recorded). A run
    with no run_id carries nothing to key on.
    """
    if not result.run_id:
        return
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=result.run_id,
        block=_AGG_RUN_BLOCK_KEY,
        cmd_sha=_agg_run_cmd_sha(experiment_dir, result.run_id),
        result_dump=result.model_dump(mode="json"),
    )


def _detached_agg_run_result(
    *, run_id: str, pid: int, log_path: str | None
) -> AggregateBlockResult:
    """The immediate-return handle for a detached aggregate-run (design §3).

    ``needs_decision`` is False (nothing to decide yet — the results brief arrives
    on completion, read from the journal) and ``next_block`` is null (the journal,
    not this process, carries the next-block suggestion). ``block_drive._chain``
    exits on this via ``_is_detached`` (started / watch / detached_pid /
    stage=="detached").
    """
    return AggregateBlockResult(
        block="run",
        stage_reached="detached",
        needs_decision=False,
        reason=(
            "aggregate-run detached — the combine + rsync harvest runs in a durable "
            "background worker; its results brief arrives on completion (read the "
            "journal). The greenlight and scope gates already passed synchronously "
            "before the detach."
        ),
        run_id=run_id,
        brief={"run_id": run_id, "log_path": log_path},
        started=True,
        watch="journal",
        detached_pid=pid,
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

    Detach-by-contract (design §3; run-#10 F-K): with ``detach`` ON (default) the
    greenlight + scope gates fire synchronously, then a durable detached worker
    owns the harvest (combine SSH round-trips + rsync pull + the breaker-deadline
    wait-and-retry) and the block returns a ``{started, watch: journal,
    detached_pid}`` handle immediately — the results-table brief is read from the
    journal on completion. A re-invoke after the worker finished REPLAYS the
    recorded terminal (no re-combine, no SSH).
    """
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.aggregate.run_id,
        verb="aggregate-run",
        predecessor="aggregate-check",
    )
    # Scope gate (rigor-primitives T3), gate → detach ordering PROOF (same shape as
    # submit-s4): a locked evidence-scope refuses SYNCHRONOUSLY in the parent, never
    # inside a detached child's log where a "spent a reserved look" refusal is
    # invisible. Defense in depth — ONE definition (assert_scopes_unlocked), TWO
    # call sites: the detached CHILD re-hits the very same gate inside aggregate_flow,
    # so a scope locked in the window between this parent check and the child's reduce
    # is still caught. Fail-safe: a scope-less run passes silently.
    assert_scopes_unlocked(experiment_dir, spec.aggregate.run_id)
    # Detach-by-contract (design §3): the gates above fired SYNCHRONOUSLY (gate →
    # detach — a gate failure surfaces here, loudly, never inside a detached child).
    if spec.detach:
        # Idempotent re-invoke (run #7): a prior detached worker may have already
        # driven this block to its terminal for the current tree — replay that
        # recorded outcome instead of re-spawning a redundant worker (no SSH, no
        # re-combine). A moved cmd_sha (a nudge) or no record → fall through and
        # spawn; a still-LIVE worker is refused by the single-lease.
        replay = _replay_agg_run_terminal(experiment_dir, spec.aggregate.run_id)
        if replay is not None:
            return replay

        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="aggregate-run",
            experiment_dir=str(experiment_dir),
            spec=_detached_agg_run_spec_dict(spec),
        )
        return _detached_agg_run_result(
            run_id=launch.run_id, pid=launch.pid, log_path=launch.log_path
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
    # look-ledger side effect fires ONCE, inside the composed ``aggregate-flow``;
    # the scope GATE now fires TWICE (defense in depth) — the synchronous
    # pre-detach ``assert_scopes_unlocked`` above AND the child's own check inside
    # ``aggregate-flow`` — with one definition, so the two can never disagree.
    if agg.scope_looks is not None:
        brief["scope_looks"] = agg.scope_looks

    partial = bool(agg.escalation_reason) or bool(agg.failed_waves)
    result = AggregateBlockResult(
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
    # Record the genuine terminal so a re-invoke (the block-drive tick after the
    # detached worker exits) REPLAYS this brief instead of re-combining a
    # completed harvest. This runs on the synchronous path — which is exactly what
    # the detached child executes — so the parent's replay finds it.
    _record_agg_run_terminal(experiment_dir, result)
    return result
