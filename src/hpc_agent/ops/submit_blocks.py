"""``submit-s1``..``submit-s4`` — the submit workflow as human-amplification blocks.

The submit flow, decomposed (docs/design/human-amplification-blocks.md §3) into
four blocks, each a THIN orchestrator that composes existing rings and TERMINATES
at a human decision point carrying code-digested evidence (a *brief*). No decision
is resolved by the LLM: code chains deterministically as far as it can, then hands
back the brief for the ``y``/nudge propose loop (§2).

* ``submit-s1`` (resolve) — preflight + walk-submit-ambiguities. Surfaces each
  ambiguity's ``safe_default`` as a PRE-FILLED RECOMMENDATION (§6 line 181),
  never auto-applied into ``resolved`` — ``apply-safe-defaults`` is the silent
  actor this kills, so S1 does NOT call it. When the walk is clean and a
  ``resolve`` spec is supplied, chains ``resolve-submit-inputs`` to a
  ``resolved`` / ``prior_run_found`` terminator.
* ``submit-s2`` (stage & canary) — ``submit-and-verify`` stopped after a verified
  canary + the ``estimate-core-hours`` footprint. Brief: "canary green, est N
  core-hours".
* ``submit-s3`` (submit & watch) — Phase-2 main-array launch + ``monitor-flow``
  to terminal/anomaly + ``decide-monitor-arm``. Runs unattended.
* ``submit-s4`` (harvest) — ``aggregate-flow`` → a code-extracted results table
  + a slot for proposed interpretations.

Each block owns its invariants at the boundary (adding-a-primitive.md): it
validates the wire spec (the embedded models do the shape work) and fails loudly
via the composed rings. The block bodies stay THIN — they never reimplement ring
logic, only sequence it and digest the evidence into a brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.decide_monitor_arm import DecideMonitorArmSpec
from hpc_agent._wire.workflows.submit_blocks import (
    SubmitBlockResult,
    SubmitS1Spec,
    SubmitS2Spec,
    SubmitS3Spec,
    SubmitS4Spec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.cost import CostEstimate, estimate_core_hours
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.block_gate import assert_greenlit_target
from hpc_agent.ops.monitor.arm import decide_monitor_arm
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs
from hpc_agent.ops.submit_and_verify import launch_main_array, submit_and_verify
from hpc_agent.ops.submit_preflight import submit_preflight
from hpc_agent.ops.walk_submit_ambiguities import walk_submit_ambiguities

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

__all__ = ["submit_s1", "submit_s2", "submit_s3", "submit_s4"]


def _detached_block_result(block: str, verb: str, launch: Any) -> SubmitBlockResult:
    """Build the immediate-return handle for a detached block (design §3).

    The parent already ran its synchronous gate + drift guards (gate → drift →
    detach) and spawned the durable detached worker; this is the ``{started,
    watch: journal, detached_pid}`` handle it hands back so the chat is never
    held. ``needs_decision`` is False (nothing to decide yet — the brief arrives
    on completion, read from the journal) and ``next_block`` is null (the journal,
    not this process, carries the next-block suggestion once the block finishes).
    """
    return SubmitBlockResult(
        block=block,  # type: ignore[arg-type]  # caller passes the block's own literal
        stage_reached="detached",
        needs_decision=False,
        reason=(
            f"{verb} detached — the scheduler poll runs in a durable background "
            "worker; its brief arrives on completion (read the journal). The gate "
            "and drift guards already passed synchronously before the detach."
        ),
        run_id=launch.run_id,
        started=True,
        watch="journal",
        detached_pid=launch.pid,
        brief={"run_id": launch.run_id, "log_path": launch.log_path},
    )


def _detached_spec_dict(spec: Any) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME verb body synchronously (its poll is the point), so
    its spec must carry ``detach=False`` — otherwise it would re-detach forever.
    """
    dumped: dict[str, Any] = spec.model_copy(update={"detach": False}).model_dump(mode="json")
    return dumped


def _next_block(verb: str, why: str, **spec_hint: Any) -> dict[str, Any]:
    """Build the machine-computed next-block hint (``{verb, why, spec_hint}``).

    The deterministic successor suggestion (design §2, the ``_next_step_hint``
    pattern generalized): ``verb`` names the next block's CLI verb, ``why`` is the
    one-line rationale the LLM surfaces, and ``spec_hint`` is the minimal
    next-spec skeleton (run_id / canary ids / campaign_id). None is returned by
    the callers directly at a terminal / human-branch terminator — this helper is
    only invoked where a single deterministic successor exists.
    """
    return {"verb": verb, "why": why, "spec_hint": dict(spec_hint)}


def _assert_canary_verified(experiment_dir: Path, base: SubmitFlowSpec) -> None:
    """S3 predecessor-artifact check: S2's canary is recorded validated-fresh.

    The strongest CODE-WRITTEN artifact S2 leaves is the canary TTL cache:
    ``verify-canary`` calls ``record_canary_validated`` on a green canary, keyed
    by ``(cmd_sha, framework-version)`` — the same key ``submit-flow``'s skip
    reads. So when the cache is ENABLED, a ``submit-s3`` whose ``(cmd_sha,
    version)`` is NOT validated-fresh means either S2 never verified a canary or
    the spec's ``cmd_sha`` moved (a nudge changed the tree) — refuse, pointing
    back to ``submit-s2``.

    Bounds of the artifact (documented, not worked around):
      * cache DISABLED (``HPC_NO_CANARY_SKIP=1``) → the artifact is unavailable;
        the greenlight gate + ``_assert_no_post_greenlight_drift`` carry the
        canary-verified guarantee, so this check is a no-op.
      * no ``HPC_CMD_SHA`` on the spec → nothing to key on → no-op (same fallback).
      * TTL window: a greenlight that lands after ``HPC_CANARY_TTL_SEC`` (default
        4h) legitimately re-canaries via ``submit-s2`` — a cheap, bounded canary,
        exactly the design's "mis-speculation is bounded" stance.
    """
    from hpc_agent import __version__ as _pkg_version
    from hpc_agent.state import canary_cache

    if canary_cache.cache_disabled():
        return
    cmd_sha = (base.job_env or {}).get("HPC_CMD_SHA") or ""
    if not cmd_sha:
        return
    key = canary_cache.canary_cache_key(cmd_sha=cmd_sha, version=_pkg_version or "")
    if not canary_cache.is_canary_validated_fresh(key):
        raise errors.SpecInvalid(
            "submit-s3: no validated-fresh canary for this (cmd_sha, version) — "
            "re-run submit-s2 so the canary verifies the current tree before the "
            f"main array launches (run_id={base.run_id!r})."
        )


# ── S1 helpers ──────────────────────────────────────────────────────────────


def _recommendations_from_ambiguities(
    ambiguities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Surface each ambiguity's ``safe_default`` as a ``recommendation`` (§6).

    The design kills ``apply-safe-defaults`` as a SILENT ACTOR: the old
    auto-applied default survives ONLY as a pre-filled recommendation inside the
    brief. So each ambiguity is passed through verbatim (``safe_default`` intact)
    with an added ``recommendation`` mirror the LLM proposes and the human
    greenlights — but nothing is written into ``resolved`` here.
    """
    out: list[dict[str, Any]] = []
    for amb in ambiguities:
        entry = dict(amb)
        # The pre-filled recommendation is exactly the (never-auto-applied)
        # safe_default. A REQUIRED_CALLER_FIELDS ambiguity has none (the
        # partition guard forbids it) → recommendation stays None: genuine
        # judgment the human must supply.
        entry["recommendation"] = amb.get("safe_default")
        out.append(entry)
    return out


# ── S2 helpers ──────────────────────────────────────────────────────────────


def _estimate_for_submit(base: SubmitFlowSpec) -> CostEstimate:
    """Compute the pre-dispatch core-hours footprint from the submit spec.

    ``total_tasks × walltime × cores`` — all three already live on the submit
    spec (``total_tasks`` + ``resources.{walltime_sec,cpus}``). Delegates to the
    single ``estimate_core_hours`` kernel (cost.py is untouched). A missing
    walltime yields a zero-cost estimate (the kernel is defensive), which reads
    as "unknown footprint" in the brief rather than raising.
    """
    resources = base.resources
    walltime_s = resources.walltime_sec if (resources and resources.walltime_sec) else 0
    cores = resources.cpus if (resources and resources.cpus) else None
    return estimate_core_hours(
        total_tasks=base.total_tasks,
        walltime_s=walltime_s or 0,
        cores_per_task=cores,
    )


# ── S4 helpers ──────────────────────────────────────────────────────────────


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


# ── S1 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s1",
    verb="workflow",
    composes=["submit-preflight", "walk-submit-ambiguities", "resolve-submit-inputs"],
    side_effects=[SideEffect("ssh", "<cluster> (preflight probe, when run_preflight)")],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.ClusterUnknown],
    idempotent=True,
    idempotency_key="walk.experiment_dir",
    cli=CliShape(
        help=(
            "Submit block S1 (resolve): preflight + walk-submit-ambiguities to "
            "the decision boundary. Brief = the {resolved, ambiguities} envelope "
            "with each ambiguity's safe_default surfaced as a RECOMMENDATION "
            "(never auto-applied). When the walk is clean and a resolve spec is "
            "supplied, chains resolve-submit-inputs. Terminates → y/nudge."
        ),
        spec_arg=True,
        spec_model=SubmitS1Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s1"),
    ),
    agent_facing=True,
)
def submit_s1(experiment_dir: Path, *, spec: SubmitS1Spec) -> SubmitBlockResult:
    """Resolve block: preflight → walk ambiguities → (optional) resolve inputs.

    Ends at the FIRST decision point with a full brief for the ``y``/nudge loop:
    ``needs_resolution`` (the walk surfaced ambiguities, each with a pre-filled
    ``recommendation``), or — when the walk is clean and ``spec.resolve`` was
    supplied — the ``resolve-submit-inputs`` terminator (``resolved`` /
    ``prior_run_found`` / ``needs_scaffold_interview``). ``needs_decision`` is
    True in every case: S1 always ends at a human greenlight (§3).
    """
    brief: dict[str, Any] = {}

    # 1. Preflight (optional) — fold pass/fail into the brief.
    if spec.run_preflight:
        pf = submit_preflight(experiment_dir=experiment_dir, cluster=spec.walk.cluster)
        brief["preflight"] = {"overall": pf.get("overall")}

    # 2. Walk ambiguities — the envelope machinery, unchanged. Accumulates ALL
    #    decision points in one pass (never early-returns on the first miss).
    walk = walk_submit_ambiguities(spec=spec.walk)
    brief["resolved"] = dict(walk.resolved)
    brief["provenance"] = dict(walk.provenance)
    # 3. Surface each safe_default as a pre-filled recommendation — NOT applied
    #    into resolved (apply-safe-defaults is the silent actor this kills, §6).
    brief["ambiguities"] = _recommendations_from_ambiguities(walk.ambiguities)

    if walk.ambiguities:
        return SubmitBlockResult(
            block="s1",
            stage_reached="needs_resolution",
            needs_decision=True,
            reason=(
                f"{len(walk.ambiguities)} field(s) need a decision; each carries a "
                "pre-filled recommendation the human greenlights or nudges."
            ),
            brief=brief,
        )

    # 4. Walk clean. If a resolve spec was supplied, chain the deterministic
    #    input-resolution ring to its own terminator; else stop at the clean
    #    brief for the human to greenlight the resolved plan.
    if spec.resolve is None:
        return SubmitBlockResult(
            block="s1",
            stage_reached="resolved",
            needs_decision=True,
            reason="all submit inputs resolved (no ambiguities); greenlight to stage & canary.",
            brief=brief,
            next_block=_next_block(
                "submit-s2",
                "inputs resolved; stage & canary the run for review.",
            ),
        )

    rr = resolve_submit_inputs(experiment_dir, spec=spec.resolve)
    brief["resolve"] = {
        "stage_reached": rr.stage_reached,
        "reason": rr.reason,
        "run_id": rr.run_id,
        "submit_spec": rr.submit_spec,
        "sidecar_path": rr.sidecar_path,
        "prior_run_id": rr.prior_run_id,
        "prior_status": rr.prior_status,
    }
    # Only a CLEAN resolve (submit-flow spec built) has a single deterministic
    # successor (submit-s2). ``prior_run_found`` (resume-vs-fresh) and
    # ``needs_scaffold_interview`` are genuine human branches → next_block null.
    next_block = (
        _next_block(
            "submit-s2",
            "inputs resolved; stage & canary the run for review.",
            run_id=rr.run_id,
        )
        if rr.stage_reached == "resolved"
        else None
    )
    return SubmitBlockResult(
        block="s1",
        stage_reached=rr.stage_reached,
        needs_decision=True,
        reason=rr.reason,
        run_id=rr.run_id,
        brief=brief,
        next_block=next_block,
    )


# ── S2 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s2",
    verb="workflow",
    composes=["submit-and-verify"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (canary only)"),
        SideEffect("ssh", "<cluster> (canary poll + log scan)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Submit block S2 (stage & canary): submit-and-verify STOPPED after a "
            "verified canary (the main array does NOT launch), plus the "
            "estimate-core-hours footprint. Brief = 'canary green, est N "
            "core-hours'. Terminates → y/nudge; S3 launches the main array."
        ),
        spec_arg=True,
        spec_model=SubmitS2Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s2"),
    ),
    agent_facing=True,
)
def submit_s2(experiment_dir: Path, *, spec: SubmitS2Spec) -> SubmitBlockResult:
    """Stage & canary block: canary-only submit + verify, STOP, estimate cost.

    Runs ``submit-and-verify`` with ``stop_after_canary=True`` so the main array
    never launches, then attaches the pre-dispatch core-hours estimate. Ends at
    the "canary green, est N core-hours" brief for the ``y``/nudge loop. A failed
    or deduped canary is its own terminator — S2 surfaces it (an anomaly is a
    block terminator too, §5) rather than sailing into the main array.

    Precondition gate (design §2): the latest journaled decision for this run must
    be a greenlight naming ``submit-s2`` — the human greenlit S1's resolved brief.
    A missing/mismatched greenlight fails loudly (``assert_greenlit_target``).
    """
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.submit.submit.run_id,
        verb="submit-s2",
        predecessor="S1",
    )
    # Detach-by-contract (design §3): the greenlight gate above fired
    # SYNCHRONOUSLY (gate → detach — a gate failure surfaces here, loudly, never
    # inside a detached child). With detach ON (default), spawn a durable
    # background worker to own the canary poll and return the handle immediately.
    if spec.detach:
        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached

        launch = launch_submit_block_detached(
            verb="submit-s2",
            experiment_dir=str(experiment_dir),
            spec=_detached_spec_dict(spec),
        )
        return _detached_block_result("s2", "submit-s2", launch)

    sv = submit_and_verify(experiment_dir, spec=spec.submit, stop_after_canary=True)

    # Cost estimate from the submit spec (cost.py untouched).
    est = _estimate_for_submit(spec.submit.submit)
    brief: dict[str, Any] = {
        "canary_run_id": sv.canary_run_id,
        "canary_job_ids": sv.canary_job_ids,
        "verified": sv.verified,
        "failure_kind": sv.failure_kind,
        "deduped": sv.deduped,
        "est_core_hours": est.est_core_hours,
        "est_gpu_hours": est.est_gpu_hours,
        "cost_estimate": {
            "total_tasks": est.total_tasks,
            "walltime_s": est.walltime_s,
            "cores_per_task": est.cores_per_task,
            "gpus_per_task": est.gpus_per_task,
            "est_core_hours": est.est_core_hours,
            "est_gpu_hours": est.est_gpu_hours,
        },
    }
    if sv.verify_result is not None:
        brief["verify_result"] = sv.verify_result.model_dump(mode="json")

    if sv.deduped:
        return SubmitBlockResult(
            block="s2",
            stage_reached="deduped",
            needs_decision=True,
            reason="the run already exists — no fresh canary fired; confirm resume-vs-fresh.",
            run_id=sv.run_id,
            brief=brief,
        )
    if not sv.verified:
        return SubmitBlockResult(
            block="s2",
            stage_reached="canary_failed",
            needs_decision=True,
            reason=f"canary failed verification ({sv.failure_kind}); propose a fix before main.",
            run_id=sv.run_id,
            brief=brief,
        )
    return SubmitBlockResult(
        block="s2",
        stage_reached="canary_verified",
        needs_decision=True,
        reason=(
            f"canary green, est. {est.est_core_hours:g} core-hours; greenlight to submit & watch."
        ),
        run_id=sv.run_id,
        brief=brief,
        next_block=_next_block(
            "submit-s3",
            "canary verified; launch the main array and watch to terminal.",
            run_id=sv.run_id,
            canary_run_id=sv.canary_run_id,
            canary_job_ids=sv.canary_job_ids,
        ),
    )


# ── S3 ──────────────────────────────────────────────────────────────────────

# monitor-flow terminal states that are §5 anomaly terminators (human decides)
# vs a clean completion that simply suggests S4.
_S3_ANOMALY_STATES: frozenset[str] = frozenset({"failed", "abandoned"})


@primitive(
    name="submit-s3",
    verb="workflow",
    composes=["submit-flow", "monitor-flow", "decide-monitor-arm"],
    side_effects=[
        SideEffect("scheduler-submit", "<cluster> (main array)"),
        SideEffect("ssh", "<cluster> (status poll + wave combine)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="submit.submit.run_id",
    cli=CliShape(
        help=(
            "Submit block S3 (submit & watch): launch the main array (Phase-2 of "
            "the two-phase gate — canary verified in S2), monitor to "
            "terminal/anomaly, and arm the next monitor tick. Runs UNATTENDED — "
            "no human boundary inside; an anomaly is a block terminator."
        ),
        spec_arg=True,
        spec_model=SubmitS3Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s3"),
    ),
    agent_facing=True,
)
def submit_s3(experiment_dir: Path, *, spec: SubmitS3Spec) -> SubmitBlockResult:
    """Submit & watch block: launch main + monitor to terminal + arm next tick.

    The canary was verified and greenlit in S2, so S3 launches the main array via
    the standalone Phase-2 path (``launch_main_array``), then watches it to a
    terminal/timeout state and arms the next monitor tick (``decide-monitor-arm``).
    No human boundary inside: a clean completion flows on to S4 (harvest); an
    anomaly (failed/abandoned) or a timeout is itself the terminator that raises
    the ``y``/nudge boundary (§5).

    Precondition gates (design §2 + §3): the latest journaled decision for this
    run must be a greenlight naming ``submit-s3`` (``assert_greenlit_target``), and
    the canary S2 verified must be recorded validated-fresh
    (``_assert_canary_verified``, the TTL-cache artifact). ``launch_main_array``
    adds the tree-drift guard. All three fail loudly before any main-array submit.
    """
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.submit.submit.run_id,
        verb="submit-s3",
        predecessor="S2",
    )
    _assert_canary_verified(experiment_dir, spec.submit.submit)

    # Detach-by-contract (design §3), ordering PROOF: gate → drift → detach.
    # Both the greenlight gate and the canary-validated gate fired above; run the
    # tree-drift guard (N's predicate) HERE too, synchronously, so a
    # post-greenlight edit fails loudly to the caller BEFORE any detach — never
    # inside a detached child that would otherwise launch the full array on code
    # the canary never verified. Only then hand the launch+monitor to a durable
    # background worker (which re-runs all three guards harmlessly + owns the poll
    # to terminal, stamping the journal so the §5 doctor covers its death).
    if spec.detach:
        from hpc_agent._kernel.lifecycle.detached import launch_submit_block_detached
        from hpc_agent.ops.submit_and_verify import _assert_no_post_greenlight_drift

        _assert_no_post_greenlight_drift(experiment_dir, spec.submit.submit)
        launch = launch_submit_block_detached(
            verb="submit-s3",
            experiment_dir=str(experiment_dir),
            spec=_detached_spec_dict(spec),
        )
        return _detached_block_result("s3", "submit-s3", launch)

    # 1. Phase-2: launch the main array (canary already verified/greenlit in S2).
    main = launch_main_array(
        experiment_dir,
        spec=spec.submit,
        canary_run_id=spec.canary_run_id,
        canary_job_ids=spec.canary_job_ids,
    )

    # 2. Monitor to terminal-or-budget (unattended, no human boundary inside).
    mon = monitor_flow(experiment_dir, spec=spec.monitor)

    # 3. Arm the next monitor tick from the final status snapshot.
    summary_raw = mon.last_status.get("summary") if isinstance(mon.last_status, dict) else None
    summary: dict[str, int] = {}
    if isinstance(summary_raw, dict):
        summary = {k: int(v) for k, v in summary_raw.items() if isinstance(v, (int, float))}
    arm = decide_monitor_arm(
        spec=DecideMonitorArmSpec(
            run_id=main.run_id,
            summary=summary,
            total_tasks=main.total_tasks,
            invocation_argv=spec.invocation_argv,
            user_invoked_via_loop=spec.user_invoked_via_loop,
        )
    )

    brief: dict[str, Any] = {
        "main_run_id": main.run_id,
        "main_job_ids": main.job_ids,
        "total_tasks": main.total_tasks,
        "canary_run_id": main.canary_run_id,
        "lifecycle_state": mon.lifecycle_state,
        "last_status": mon.last_status,
        "combined_waves": mon.combined_waves,
        "failed_waves": mon.failed_waves,
        "escalation_reason": mon.escalation_reason,
        "ticks": mon.ticks,
        "elapsed_seconds": mon.elapsed_seconds,
        "monitor_arm": arm,
    }

    if mon.lifecycle_state == "complete":
        return SubmitBlockResult(
            block="s3",
            stage_reached="watching_terminal",
            needs_decision=False,
            reason="main array complete; proceed to harvest (S4).",
            run_id=main.run_id,
            brief=brief,
            next_block=_next_block(
                "submit-s4",
                "main array complete; harvest results and propose interpretations.",
                run_id=main.run_id,
            ),
        )
    if mon.lifecycle_state == "timeout":
        return SubmitBlockResult(
            block="s3",
            stage_reached="watching_timeout",
            needs_decision=True,
            reason=(
                "monitor wall-clock budget hit; cluster jobs may run on — keep watching or stop?"
            ),
            run_id=main.run_id,
            brief=brief,
            # Still in flight; the deterministic continuation is to keep watching
            # (status-watch), which re-arms the next tick. Not S4 — nothing terminal yet.
            next_block=_next_block(
                "status-watch",
                "budget elapsed but jobs may run on; keep watching to a terminal state.",
                run_id=main.run_id,
            ),
        )
    # failed / abandoned → §5 anomaly terminator. next_block is null: recovery is a
    # genuine human branch (resubmit-failed / kill / reconcile) with no single
    # deterministic successor — the anomaly brief carries the proposed actions.
    return SubmitBlockResult(
        block="s3",
        stage_reached="watching_anomaly",
        needs_decision=True,
        reason=(
            f"main array reached '{mon.lifecycle_state}' "
            f"({mon.escalation_reason or 'no escalation reason'}); propose recovery."
        ),
        run_id=main.run_id,
        brief=brief,
    )


# ── S4 ──────────────────────────────────────────────────────────────────────


@primitive(
    name="submit-s4",
    verb="workflow",
    composes=["aggregate-flow"],
    side_effects=[SideEffect("ssh", "<cluster> (wave combine + rsync pull)")],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="aggregate.run_id",
    cli=CliShape(
        help=(
            "Submit block S4 (harvest): aggregate-flow → a code-extracted results "
            "table + a slot for proposed interpretations. Terminates → y/nudge. "
            "(Calls the existing aggregate entry; Unit B's harvest_on_terminal "
            "guarantee is a parallel add.)"
        ),
        spec_arg=True,
        spec_model=SubmitS4Spec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="submit_s4"),
    ),
    agent_facing=True,
)
def submit_s4(experiment_dir: Path, *, spec: SubmitS4Spec) -> SubmitBlockResult:
    """Harvest block: aggregate → code-extracted results table → propose interp.

    Runs the existing ``aggregate-flow`` (ensure waves combined → pull partials →
    reduce) and digests the reduced metrics into a stable results table. The
    brief carries the table plus an empty ``proposed_interpretations`` slot the
    LLM fills at the ``y``/nudge boundary — code extracts the results; the human
    concludes from them (§2). Results are never interpreted raw by the LLM.

    NOTE (Unit B dependency): a ``harvest_on_terminal`` guarantee (§5) is being
    added in parallel by Unit B; S4 calls the EXISTING ``aggregate-flow`` entry
    until that lands, at which point S4 should route through the guaranteed path.

    Precondition gate (design §2): the latest journaled decision for this run must
    be a greenlight naming ``submit-s4`` — the human greenlit S3's terminal brief.
    The terminal-or-explicitly-partial invariant is NOT re-checked here: the
    composed ``aggregate-flow`` gate owns it (compose, don't duplicate).
    """
    assert_greenlit_target(
        experiment_dir,
        run_id=spec.aggregate.run_id,
        verb="submit-s4",
        predecessor="S3",
    )
    agg = aggregate_flow(experiment_dir, spec=spec.aggregate)

    brief: dict[str, Any] = {
        "run_id": agg.run_id,
        "results_table": _results_table(agg.aggregated_metrics),
        "combined_waves": agg.combined_waves,
        "failed_waves": agg.failed_waves,
        "escalation_reason": agg.escalation_reason,
        "nonempty_failing_task_ids": agg.nonempty_failing_task_ids,
        "column_violations": agg.column_violations,
        # The slot the LLM fills with proposed interpretations at y/nudge — the
        # code hands over an EMPTY list; concluding is the human's decision (§2).
        "proposed_interpretations": [],
    }

    partial = bool(agg.escalation_reason) or bool(agg.failed_waves)
    return SubmitBlockResult(
        block="s4",
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
