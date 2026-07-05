"""``status-snapshot`` / ``status-watch`` — the status flow as human-amplification blocks.

The status flow, decomposed (docs/design/human-amplification-blocks.md §3, §5)
into two THIN orchestrators, each composing existing monitor rings and
TERMINATING at a human decision point carrying code-digested evidence (a
*brief*). No decision is resolved by the LLM: code chains deterministically as
far as it can, then hands back the brief for the ``y``/nudge propose loop (§2).
Mirrors the submit S1–S4 blocks (``ops/submit_blocks.py``).

* ``status-snapshot`` (one-shot digest, no watch) — a cheap journal-first read
  (optionally re-derived from the cluster via ``reconcile-journal``) digested
  into "what is running where / what changed since the human last looked" (the
  §5 first-class task-state contract). The changed-since delta is computed
  against ``last_seen_by_human_at``; the watermark is then re-stamped via
  ``mark_seen_by_human``. ``needs_decision`` only when evidence demands it: a
  stalled driver (``find_stalled_runs``, the §5 watchdog) or a failed/abandoned
  run.
* ``status-watch`` (blocking poll to terminal or anomaly) — composes
  ``monitor-flow`` (which owns the throttled SSH spine and the §5 guaranteed
  harvest in its ``finally``). Terminators: a clean terminal
  (``needs_decision=False`` + a hand-off hint to the harvest block) or an
  anomaly — failed / abandoned / timeout — which raises the ``y``/nudge boundary
  with a drafted-evidence brief (error digest, counts, and a structured
  ``recommendation`` — proposed next-action DATA, never LLM text).

Each block owns its invariants at the boundary (adding-a-primitive.md): it
validates the wire spec (the embedded models do the shape work) and validates
its own semantic preconditions (``reconcile`` needs a run_id + scheduler),
failing loudly via the composed rings. The block bodies stay THIN — they never
reimplement ring logic, only sequence it and digest the evidence into a brief.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.decide_monitor_arm import DecideMonitorArmSpec
from hpc_agent._wire.workflows.status_blocks import (
    StatusBlockResult,
    StatusSnapshotSpec,
    StatusWatchSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.block_chain import next_block_hint
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.monitor.arm import decide_monitor_arm
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.ops.monitor.reconcile import _sibling_run_ids, canary_parent_of, reconcile
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.recover.notify import acknowledge_alerts, read_unacknowledged_alerts
from hpc_agent.ops.relay_render import render_relay
from hpc_agent.state.index import find_in_flight_runs, find_stalled_runs
from hpc_agent.state.journal import load_run, mark_seen_by_human
from hpc_agent.state.run_record import TERMINAL_STATUSES

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["status_snapshot", "status_watch"]


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


# The per-task count keys the cluster-side reporter persists into a record's
# ``last_status`` (TaskStatus values). The digest projects exactly these so the
# brief carries stable "running where" counts regardless of the reporter's other
# bookkeeping fields (checked_at / waves / warnings).
_COUNT_KEYS: tuple[str, ...] = ("complete", "running", "pending", "failed", "unknown")

# Journal terminal statuses that are §5 anomaly terminators when a *snapshot*
# lands on one (the human must decide recovery). ``complete`` is clean;
# ``in_flight`` is live. (A timed-out run stays ``in_flight`` in the journal.)
_ANOMALY_STATUSES: frozenset[str] = frozenset({"failed", "abandoned"})

# Proposed next-action DATA per anomaly class (§2: a structured recommendation
# the LLM renders and the human greenlights — never LLM-authored prose in code).
# ``failed`` carries positive failure evidence → classify then resubmit;
# ``abandoned`` has no on-disk evidence → reconcile to confirm before resubmit.
_RECOMMEND_FAILED: dict[str, str] = {
    "action": "classify-failed-tasks",
    "then": "resubmit-failed",
}
_RECOMMEND_ABANDONED: dict[str, str] = {
    "action": "reconcile-journal",
    "then": "confirm-abandoned-then-resubmit",
}


def _recommendation_for(status_or_lifecycle: str) -> dict[str, str]:
    """Pick the proposed next-action DATA for an anomaly's terminal class."""
    return _RECOMMEND_ABANDONED if status_or_lifecycle == "abandoned" else _RECOMMEND_FAILED


def _summary_of(last_status: Any) -> dict[str, int]:
    """Project a record's ``last_status`` into stable per-task counts.

    ``last_status`` is normally the reporter's summary dict (counts keyed
    directly). Defensively accepts a ``{"summary": {...}}`` nesting too — the
    monitor-flow envelope carries the counts under either shape depending on the
    caller — and drops any non-numeric bookkeeping fields.
    """
    if not isinstance(last_status, dict):
        return {}
    inner = last_status.get("summary")
    src = inner if isinstance(inner, dict) else last_status
    counts: dict[str, int] = {}
    for key in _COUNT_KEYS:
        val = src.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            counts[key] = int(val)
    return counts


def _changed_since_seen(record: Any) -> bool:
    """Has anything happened on *record* since the human last looked?

    Compares the last driver tick (``last_tick_at``) against the attention
    watermark (``last_seen_by_human_at``). Never looked → everything is new
    (True). Looked, but no tick recorded since → nothing changed (False).
    """
    seen = parse_iso_utc_or_none(getattr(record, "last_seen_by_human_at", None))
    if seen is None:
        return True
    tick = parse_iso_utc_or_none(getattr(record, "last_tick_at", None))
    if tick is None:
        return False
    return tick > seen


def _digest_run(record: Any) -> dict[str, Any]:
    """Code-digest one run into a "running where / changed since seen" row.

    A ``-canary`` journal entry (the 1-task sibling every canary-gated submit
    writes, #258) is marked ``is_canary`` and carries its ``parent_run_id`` so
    the brief surfaces it as the parent's child rather than a mystery run.
    """
    parent_run_id = canary_parent_of(record.run_id)
    superseded_by = getattr(record, "superseded_by", "") or None
    return {
        "run_id": record.run_id,
        "is_canary": parent_run_id is not None,
        "parent_run_id": parent_run_id,
        # Supersession conduct: a record closed because a NEW run_id explicitly
        # superseded it displays as superseded (its ``status`` stays the honest
        # journal verdict, typically ``abandoned`` with verdict_reason
        # superseded_by=<new>), and is excluded from the anomaly list — it was
        # deliberately closed, not lost. ``pending_closure`` (non-empty when the
        # old scheduler jobs could not be confirmed gone at supersession time)
        # rides along so the brief surfaces the outstanding cleanup.
        "superseded_by": superseded_by,
        "is_superseded": superseded_by is not None,
        "pending_closure": dict(getattr(record, "pending_closure", {}) or {}),
        "cluster": record.cluster,
        "ssh_target": record.ssh_target,
        "status": record.status,
        "summary": _summary_of(record.last_status),
        "last_tick_at": getattr(record, "last_tick_at", None),
        "last_seen_by_human_at": getattr(record, "last_seen_by_human_at", None),
        "changed_since_seen": _changed_since_seen(record),
    }


# ── status-snapshot ──────────────────────────────────────────────────────────


@primitive(
    name="status-snapshot",
    verb="workflow",
    composes=["reconcile-journal"],
    side_effects=[SideEffect("ssh", "<cluster> (only when reconcile=True)")],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.JournalCorrupt,
        errors.ClusterUnknown,
    ],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Status block (snapshot): a one-shot, journal-first digest of what is "
            "running where and what changed since the human last looked. Computes "
            "the changed-since delta against last_seen_by_human_at, then re-stamps "
            "the watermark. Sets needs_decision only on evidence — a stalled "
            "driver or a failed/abandoned run. Optional reconcile re-derives ground "
            "truth from the cluster first (the only SSH path). Terminates → y/nudge."
        ),
        spec_arg=True,
        spec_model=StatusSnapshotSpec,
        experiment_dir_arg=True,
        # Declares an SSH side_effect (the composed reconcile-journal touches the
        # cluster when spec.reconcile=True). Per the requires-ssh consistency
        # contract, a declared SSH side_effect ⇒ requires_ssh=True, mirroring
        # aggregate-check (conditionally-SSH, still declared True). The SSH path
        # is opt-in via spec.reconcile; the flag marks the capability honestly.
        requires_ssh=True,
        schema_ref=SchemaRef(input="status_snapshot", output="status_block"),
    ),
    agent_facing=True,
)
def status_snapshot(experiment_dir: Path, *, spec: StatusSnapshotSpec) -> StatusBlockResult:
    """One-shot digest: what is running where + what changed since last looked.

    Journal-first and cluster-free unless ``spec.reconcile`` re-derives ground
    truth first. Digests durable state (§5 task-state contract) into the brief,
    surfaces stalled-driver evidence (§5 watchdog) and failed/abandoned runs, and
    re-stamps the attention watermark. ``needs_decision`` is True only when the
    evidence demands one — the snapshot never manufactures a decision point.
    """
    now_iso = spec.now_iso or utcnow_iso()

    # 1. Optional cluster re-derive (the only SSH path). Owns its precondition:
    #    reconcile is a single-run, scheduler-scoped ground-truth check.
    if spec.reconcile:
        if spec.run_id is None:
            raise errors.SpecInvalid("reconcile=True requires a run_id to re-derive.")
        if not spec.scheduler:
            raise errors.SpecInvalid("reconcile=True requires a scheduler to query alive jobs.")
        reconcile(experiment_dir, spec.run_id, scheduler=spec.scheduler)

    # 2. Gather the run(s) to digest — one run PLUS its paired ``-canary`` /
    #    parent sibling (#258: every canary-gated submit writes a sibling
    #    journal entry; a single-run snapshot that skipped it left an in-flight
    #    canary invisible to the human — proving run #3, finding c), or the
    #    whole in-flight fleet.
    if spec.run_id is not None:
        records = [
            rec
            for rid in (spec.run_id, *_sibling_run_ids(spec.run_id))
            if (rec := load_run(experiment_dir, rid)) is not None
        ]
    else:
        records = list(find_in_flight_runs(experiment_dir))

    # 3. Digest BEFORE stamping — the changed-since delta must be measured
    #    against the PRIOR watermark, not the one we are about to write.
    running_where = [_digest_run(r) for r in records]
    changed = [row for row in running_where if row["changed_since_seen"]]
    anomalies = [
        {**row, "recommendation": _recommendation_for(row["status"])}
        for row in running_where
        # A superseded record is a deliberate closure (superseded_by names the
        # replacement), not an anomaly needing a recovery decision — it
        # displays as superseded, never as failed/abandoned-needs-attention.
        if row["status"] in _ANOMALY_STATUSES and not row["is_superseded"]
    ]

    # 4. Stalled-driver evidence (§5 dead-man's switch) — a live run whose
    #    next_tick_due is in the past. Detection only; the recommendation is a
    #    re-arm proposal the human greenlights (the watchdog never restarts, §5).
    stalled = find_stalled_runs(now_iso, experiment_dir)

    # 4b. Unacknowledged watchdog alerts (proving run #3: doctor DETECTED the
    #     stalled canary driver and wrote doctor.alerts.log, but nothing
    #     DELIVERED it — detection without delivery is silence). Fail-open read;
    #     each alert is surfaced by exactly one snapshot, then acknowledged via
    #     the alert watermark below. The log itself is an audit trail and is
    #     never truncated.
    alerts = read_unacknowledged_alerts(experiment_dir)

    # 4c. Open ssh circuits (2026-07-05 incident: hoffman2's breaker was OPEN
    #     with a recorded cooldown deadline, but no surface the agent read said
    #     so — it improvised raw ssh probes and mis-diagnosed a VPN outage).
    #     One line per breaker-dark host, read from the local _ssh_circuit
    #     state files (no SSH, fail-open). Surfacing only — the differential
    #     and remediation live in the net-triage verb.
    from hpc_agent.ops.recover.net_triage import open_circuit_lines

    open_ssh_circuits = open_circuit_lines()

    # 5. Re-stamp the attention watermark now that the delta is computed, and
    #    acknowledge the alerts this brief is about to surface (same mark_seen
    #    gate: a peek-only snapshot moves neither watermark).
    if spec.mark_seen:
        for r in records:
            mark_seen_by_human(r.run_id, at=now_iso, experiment_dir=experiment_dir)
        if alerts:
            acknowledge_alerts(experiment_dir, up_to_ts=max(a["ts"] for a in alerts))

    brief: dict[str, Any] = {
        "now": now_iso,
        "running_where": running_where,
        "changed_since_seen": changed,
        "stalled_runs": stalled,
        "anomalies": anomalies,
        "alerts": alerts,
        "open_ssh_circuits": open_ssh_circuits,
    }

    needs_decision = bool(stalled or anomalies)
    if needs_decision:
        parts: list[str] = []
        if anomalies:
            parts.append(f"{len(anomalies)} failed/abandoned run(s)")
        if stalled:
            parts.append(f"{len(stalled)} stalled driver(s)")
        return StatusBlockResult(
            block="snapshot",
            stage_reached="snapshot_anomaly",
            needs_decision=True,
            reason=f"{' and '.join(parts)} need a decision; each carries a proposed next action.",
            relay=render_relay("snapshot", "snapshot_anomaly", brief),
            run_id=spec.run_id,
            brief=brief,
        )
    # A live (non-terminal) run means watching is the deterministic next step;
    # an all-terminal / empty fleet has no successor block → next_block null.
    has_live = any(r.status not in TERMINAL_STATUSES for r in records)
    return StatusBlockResult(
        block="snapshot",
        stage_reached="snapshot_clean",
        needs_decision=False,
        reason=(
            f"{len(changed)} of {len(running_where)} run(s) changed since last looked; "
            "nothing needs a decision."
        ),
        relay=render_relay("snapshot", "snapshot_clean", brief),
        run_id=spec.run_id,
        brief=brief,
        # Runtime-gated: the table's successor for a clean snapshot is status-watch,
        # but only when a live run exists to watch (an all-terminal / empty fleet
        # emits None). The ``if has_live and spec.run_id`` guard applies that
        # runtime condition AND the single-run requirement: status-watch embeds a
        # MonitorFlowSpec keyed on ONE run_id, so a fleet digest (run_id=None) has
        # no single run to watch and emits None (the driver can't materialize a
        # watch spec for "the fleet"). The spec_hint is the successor's VALID
        # minimal StatusWatchSpec — ``monitor={run_id}`` — which the driver passes
        # verbatim when it chains this ungated hop in code (block_drive._chain).
        next_block=(
            _next_block(
                "status-snapshot",
                "snapshot_clean",
                "live run(s) in flight; watch to a terminal state.",
                monitor={"run_id": spec.run_id},
            )
            if has_live and spec.run_id is not None
            else None
        ),
    )


# ── status-watch ─────────────────────────────────────────────────────────────


def _watch_anomaly_brief(mon: Any, summary: dict[str, int]) -> dict[str, Any]:
    """Draft the code-digested evidence for a failed/abandoned watch terminator.

    Pulls the counts, the failed-wave ledger, the escalation reason, and any
    classified error the reporter attached (``failure_features``), plus a
    structured ``recommendation`` (proposed next-action DATA, §2). No LLM text is
    generated in code — the human concludes from the evidence.
    """
    error_digest: dict[str, Any] | None = None
    last_status = mon.last_status if isinstance(mon.last_status, dict) else {}
    ff = last_status.get("failure_features")
    if isinstance(ff, dict):
        error_digest = {
            "classified_error": ff.get("classified_error"),
            "log_path": ff.get("log_path"),
            "cluster_log_tail": ff.get("cluster_log_tail"),
        }
    return {
        "lifecycle_state": mon.lifecycle_state,
        "summary": summary,
        "failed_waves": list(mon.failed_waves),
        "escalation_reason": mon.escalation_reason,
        "error_digest": error_digest,
        "recommendation": _recommendation_for(mon.lifecycle_state),
    }


@primitive(
    name="status-watch",
    verb="workflow",
    composes=["monitor-flow", "decide-monitor-arm"],
    side_effects=[
        SideEffect("ssh", "<cluster> (status polls)"),
        SideEffect("writes-tick-log", "<experiment_dir>/<run_id>.monitor.jsonl"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.SshUnreachable,
        errors.JournalCorrupt,
        errors.PreconditionFailed,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="monitor.run_id",
    cli=CliShape(
        help=(
            "Status block (watch): blocking poll to terminal or anomaly via "
            "monitor-flow (which owns the throttled SSH spine and the guaranteed "
            "terminal harvest). A clean terminal hands off to the harvest block "
            "(needs_decision=False); a failed/abandoned anomaly or a timeout raises "
            "the y/nudge boundary with a drafted-evidence brief. A timeout arms the "
            "next tick when an invocation_argv is supplied."
        ),
        spec_arg=True,
        spec_model=StatusWatchSpec,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="status_watch", output="status_block"),
    ),
    agent_facing=True,
)
def status_watch(experiment_dir: Path, *, spec: StatusWatchSpec) -> StatusBlockResult:
    """Blocking poll to terminal/anomaly; digest the outcome into a brief.

    Composes ``monitor-flow`` to a terminal/timeout state — monitor-flow owns the
    connection-pacing spine and the §5 guaranteed harvest (its ``finally`` runs
    ``harvest_on_terminal`` on every path), so status-watch never re-harvests; it
    surfaces the hand-off hint. A clean ``complete`` flows on to the harvest block
    (``needs_decision=False``); a ``failed``/``abandoned`` anomaly hands back a
    drafted-evidence brief; a ``timeout`` is the "keep watching?" terminator and
    arms the next tick when an ``invocation_argv`` is supplied.
    """
    mon = monitor_flow(experiment_dir, spec=spec.monitor)
    lifecycle = mon.lifecycle_state
    summary = _summary_of(mon.last_status)

    brief: dict[str, Any] = {
        "run_id": mon.run_id,
        "lifecycle_state": lifecycle,
        "summary": summary,
        "combined_waves": list(mon.combined_waves),
        "failed_waves": list(mon.failed_waves),
        "escalation_reason": mon.escalation_reason,
        "ticks": mon.ticks,
        "elapsed_seconds": mon.elapsed_seconds,
    }

    if lifecycle == "complete":
        # Harvest already ran inside monitor-flow's finally (§5). Hand off the
        # marker + the next block rather than re-harvesting here.
        brief["harvest_handoff"] = {
            "guaranteed": True,
            "harvest_marker": str(harvest_marker_path(experiment_dir, mon.run_id)),
            "next_block": "aggregate-flow / submit-s4 (harvest)",
        }
        return StatusBlockResult(
            block="watch",
            stage_reached="watch_terminal",
            needs_decision=False,
            reason="run complete; terminal harvest guaranteed — hand off to the harvest block.",
            relay=render_relay("watch", "watch_terminal", brief),
            run_id=mon.run_id,
            brief=brief,
            next_block=_next_block(
                "status-watch",
                "watch_terminal",
                "run complete; harvest results and propose interpretations.",
                run_id=mon.run_id,
            ),
        )

    if lifecycle == "timeout":
        # Budget elapsed; cluster jobs may run on. Arm the next tick when the
        # caller supplied the argv to re-invoke (decide-monitor-arm owns the
        # cron/loop/none + cadence choice).
        if spec.invocation_argv:
            rec = load_run(experiment_dir, mon.run_id)
            total_tasks = int(rec.total_tasks) if rec is not None else 0
            brief["monitor_arm"] = decide_monitor_arm(
                spec=DecideMonitorArmSpec(
                    run_id=mon.run_id,
                    summary=summary,
                    total_tasks=total_tasks,
                    invocation_argv=spec.invocation_argv,
                    user_invoked_via_loop=spec.user_invoked_via_loop,
                )
            )
        return StatusBlockResult(
            block="watch",
            stage_reached="watch_timeout",
            needs_decision=True,
            reason=(
                "monitor wall-clock budget hit; cluster jobs may run on — keep watching or stop?"
            ),
            relay=render_relay("watch", "watch_timeout", brief),
            run_id=mon.run_id,
            brief=brief,
            # Still in flight → the deterministic continuation is to keep watching.
            next_block=_next_block(
                "status-watch",
                "watch_timeout",
                "budget elapsed but jobs may run on; keep watching to a terminal state.",
                run_id=mon.run_id,
            ),
        )

    # failed / abandoned → §5 anomaly terminator with a drafted-evidence brief.
    brief["anomaly"] = _watch_anomaly_brief(mon, summary)
    return StatusBlockResult(
        block="watch",
        stage_reached="watch_anomaly",
        needs_decision=True,
        reason=(
            f"run reached '{lifecycle}' "
            f"({mon.escalation_reason or 'no escalation reason'}); review the evidence brief."
        ),
        relay=render_relay("watch", "watch_anomaly", brief),
        run_id=mon.run_id,
        brief=brief,
    )
