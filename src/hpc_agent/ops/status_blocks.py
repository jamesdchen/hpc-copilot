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

import json
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
from hpc_agent.infra.env_flags import active_env_overrides
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.attention_queue import collect_queue
from hpc_agent.ops.monitor.arm import decide_monitor_arm
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path
from hpc_agent.ops.monitor.reconcile import canary_parent_of, reconcile, sibling_run_ids
from hpc_agent.ops.monitor_flow import monitor_flow
from hpc_agent.ops.overnight import morning_brief_if_any
from hpc_agent.ops.recover.notify import acknowledge_alerts, read_unacknowledged_alerts
from hpc_agent.ops.relay_render import render_relay
from hpc_agent.state.block_terminal import terminal_block_key
from hpc_agent.state.index import find_in_flight_runs, find_stalled_runs
from hpc_agent.state.journal import load_run, mark_seen_by_human
from hpc_agent.state.run_record import TERMINAL_STATUSES
from hpc_agent.state.runs import read_run_cmd_sha

if TYPE_CHECKING:
    from pathlib import Path

# The anomaly reduction is promoted to importable names (T6a) so the attention
# queue (``ops/attention_queue.py``) can AGGREGATE it — one definition of "which
# terminal status is an anomaly", "the proposed next-action DATA", and "the
# per-run digest" — never a second copy. Behaviour is unchanged; only the
# underscore is dropped.
__all__ = [
    "status_snapshot",
    "status_watch",
    "ANOMALY_STATUSES",
    "recommendation_for",
    "digest_run",
]


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
ANOMALY_STATUSES: frozenset[str] = frozenset({"failed", "abandoned"})

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


def recommendation_for(status_or_lifecycle: str) -> dict[str, str]:
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


def digest_run(record: Any) -> dict[str, Any]:
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


def _queued_age_rows(experiment_dir: Path, now_iso: str) -> list[dict[str, Any]]:
    """Enqueue age for every still-``queued`` item, longest-waiting first (§10.S3).

    The run-queue plan accepts a cost — "the window widens for items held
    pre-gate, and the morning brief should show enqueue age for that reason"
    (§10.S3) — so the digest shows which items have waited longest for a y.
    ``queue-advance``'s rows are relayed VERBATIM (S13) and its wire model
    carries no arrival stamp, so age rides BESIDE them: the ledger's own
    ``enqueued_at`` relayed as stored, plus ``age_sec`` computed by the SAME
    ``state/queue_intake.enqueue_age_sec`` that ``queue-status`` uses — one
    derivation, never two, and the renderer/LLM relays the number rather than
    doing date math (the determinism boundary).

    A record written before the stamp existed surfaces ``age_sec: null`` — age
    UNKNOWN, disclosed rather than guessed — and sorts after every known age.
    Clipped to 10 rows like ``held``; ``queued_total`` keeps the clip visible.
    The read is the tolerant, non-creating fold every queue projection uses.
    """
    from hpc_agent.state.queue_intake import (
        STATE_QUEUED,
        enqueue_age_sec,
        items_in_states,
        read_intake_items,
    )

    now_dt = parse_iso_utc_or_none(now_iso)
    rows: list[dict[str, Any]] = []
    for item in items_in_states(read_intake_items(experiment_dir), [STATE_QUEUED]):
        item_id = item.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            continue
        enqueued_at = item.get("enqueued_at")
        rows.append(
            {
                "item_id": item_id,
                "enqueued_at": (
                    enqueued_at if isinstance(enqueued_at, str) and enqueued_at else None
                ),
                "age_sec": enqueue_age_sec(enqueued_at, now_dt) if now_dt is not None else None,
            }
        )
    # Oldest first (the items the human most needs to see), unknown ages last,
    # item_id as the tie-break so two reads at one instant render identically.
    rows.sort(key=lambda row: (row["age_sec"] is None, -(row["age_sec"] or 0), row["item_id"]))
    return rows[:10]


def _queue_brief_section(experiment_dir: Path, now_iso: str) -> dict[str, Any] | None:
    """The run-queue paragraph of the morning digest, or ``None`` when nothing is queued.

    Run-queue plan §3 promised the brief could surface "3 items waiting on gpu
    capacity"; the maintainer ordered it built (2026-07-29). The section calls the
    placement AUTHORITY (``queue-advance``, pure) and relays its rows and its
    code-rendered text VERBATIM — S13's rule: a brief relays the authority's
    words, it never summarizes them, so this surface and a direct
    ``queue-advance`` call can never disagree. ``held`` is clipped to 10 rows
    with ``held_total`` keeping the clip visible (the bounded-projection
    posture); ``held_counts`` still spans everything. ``queued_ages`` rides
    beside the authority's rows (:func:`_queued_age_rows`): §10.S3's accepted
    cost is that the enqueue→y window widens for items held pre-gate, so the
    brief shows each queued item's enqueue age, oldest first.

    Additive + fail-open: an experiment with no queue, an unreadable ledger, or
    any read surprise yields ``None`` and the snapshot is byte-unchanged — a
    queue problem must never blank the digest the human reads first.
    """
    try:
        from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec
        from hpc_agent.ops.queue.advance import queue_advance

        decision = queue_advance(experiment_dir=experiment_dir, spec=QueueAdvanceSpec(now=now_iso))
        if decision.queued_total <= 0:
            return None
        queued_ages = _queued_age_rows(experiment_dir, now_iso)
    except Exception:  # noqa: BLE001 — a queue read must never blank the digest
        return None
    return {
        "queued_total": decision.queued_total,
        "decision": decision.decision,
        "placements_decided": len(decision.placements),
        "held": [h.model_dump(mode="json") for h in decision.held[:10]],
        "held_total": len(decision.held),
        "held_counts": dict(decision.held_counts),
        "occupancy": dict(decision.occupancy),
        "queued_ages": queued_ages,
        "text": decision.brief,
    }


def _parked_brief_section(experiment_dir: Path, now_iso: str) -> list[dict[str, Any]] | None:
    """The parked-runs paragraph of the morning digest, or ``None`` when none.

    One row per run parked on a human decision (§5 "parked ≠ stalled"), each
    carrying the park-time diagnosis POINTER (S13: pointers + counts, never the
    dossier content — the human reads the render from disk): ``diagnosis`` is
    the one shared line (:func:`state.diagnosis.diagnosis_pointer_line` —
    'attached (N proposed action(s), agent-authored, advisory) — <path>' /
    'none') so this surface, the park notification, and the doctor's parked
    note cannot drift into three phrasings.

    Additive + fail-open like :func:`_queue_brief_section`: no parked runs, or
    any read surprise, yields ``None`` and the snapshot is byte-unchanged — an
    advisory pointer must never blank the digest the human reads first.
    """
    try:
        from hpc_agent.state.diagnosis import diagnosis_pointer, diagnosis_pointer_line
        from hpc_agent.state.index import find_parked_runs

        rows: list[dict[str, Any]] = []
        for hit in find_parked_runs(now_iso, experiment_dir=experiment_dir):
            try:
                pointer = diagnosis_pointer(experiment_dir, str(hit.get("run_id") or ""))
            except Exception:  # noqa: BLE001 — advisory read stays fail-open
                pointer = None
            rows.append(
                {
                    "run_id": hit.get("run_id"),
                    "block": hit.get("block"),
                    "workflow": hit.get("workflow"),
                    "awaiting_since": hit.get("awaiting_since"),
                    "diagnosis": diagnosis_pointer_line(pointer),
                    "diagnosis_path": pointer.get("path") if pointer else None,
                }
            )
    except Exception:  # noqa: BLE001 — a parked read must never blank the digest
        return None
    return rows or None


def _readiness_brief_section(records: list[Any], now_iso: str) -> list[dict[str, Any]] | None:
    """The readiness paragraph: one ledger line per cluster the digest involves.

    ``docs/design/s2-readiness.md`` pillar 1's render mandate on the surface the
    human reads FIRST. A snapshot already answers "what is running where"; this
    answers the question that decides what to do about it — whether the machine
    still believes it can reach where. Rendered with the AGE, because a verdict
    without one is the failure this substrate exists to remove.

    Scope is the clusters of the digested runs, not ``clusters.yaml``: the
    snapshot is about THESE runs, and a line for a cluster none of them touch is
    noise on the morning read. Each run's ``ssh_target`` keys the ledger when it
    has one (that is the host actually reached, and the ledger normalizes
    ``user@host`` itself); otherwise the cluster name is resolved through
    ``clusters.yaml``. Deduped — two runs on one cluster are one readiness fact.

    CONSULT-ONLY: the digest reads the durable ledger and the config, never the
    network — a status snapshot is journal-first by contract and must stay so.
    Additive + fail-open like the queue/parked sections: no involved cluster, or
    any read surprise, yields ``None`` and the brief is byte-unchanged.
    """
    try:
        from hpc_agent.ops.readiness_digest import digests_for, host_for_cluster

        pairs: list[tuple[str | None, str]] = []
        for record in records:
            cluster = str(getattr(record, "cluster", "") or "") or None
            host = str(getattr(record, "ssh_target", "") or "")
            if not host and cluster:
                host = host_for_cluster(cluster)
            if host:
                pairs.append((cluster, host))
        if not pairs:
            return None
        rows = digests_for(pairs, now=parse_iso_utc_or_none(now_iso))
    except Exception:  # noqa: BLE001 — a readiness read must never blank the digest
        return None
    return rows or None


def _slo_brief_section(experiment_dir: Path, records: list[Any]) -> list[dict[str, Any]] | None:
    """The S2 SLO paragraph: one scorecard line per run that has FINISHED.

    Pillar 6 of ``docs/design/s2-readiness.md``: "Unmeasured devx regresses",
    and the scorecard "rides run telemetry and the morning brief". ``status-
    snapshot`` IS the morning read, so it carries the same line ``monitor-
    summary`` prints — through the SAME reducer (``state/s2_slo.slo_for_run``)
    and the SAME renderer (``ops/monitor/summary.format_slo``). Never a second
    definition: two surfaces disagreeing about a scorecard is worse than one
    surface not carrying it.

    Terminal runs only. Every field settles at submit time, so an in-flight run
    would show the same numbers — but the morning read is a review of what
    happened, and pinning the line to a finished run keeps the in-flight digest
    byte-unchanged rather than growing a paragraph that repeats every tick. That
    also means the section rides a RUN-SCOPED snapshot: a fleet digest
    (``run_id=None``) gathers ``find_in_flight_runs``, which by definition holds
    no finished run. Widening the gather to reach terminal runs would change what
    ``running_where`` / ``changed_since_seen`` mean, which is a different edit
    than surfacing a scorecard.

    Additive + fail-open: a run whose SLO measured nothing contributes no row,
    and no rows at all yields ``None``.
    """
    try:
        from hpc_agent.ops.monitor.summary import format_slo
        from hpc_agent.state.s2_slo import slo_for_run

        rows: list[dict[str, Any]] = []
        for record in records:
            if record.status not in TERMINAL_STATUSES:
                continue
            line = format_slo(slo_for_run(experiment_dir, record.run_id, record))
            if line:
                rows.append({"run_id": record.run_id, "status": record.status, "slo": line})
    except Exception:  # noqa: BLE001 — a scorecard read must never blank the digest
        return None
    return rows or None


@primitive(
    name="status-snapshot",
    verb="workflow",
    composes=["reconcile-journal", "queue-advance"],
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

    Two s2-readiness sections ride the brief, both additive and omitted when
    empty: ``readiness`` — the standing ledger's one-liner per involved cluster
    (verdict + AGE + the sensor that is not green), CONSULTED from disk and
    never probed, so the journal-first contract is untouched — and ``slo`` — the
    S2 scorecard for each finished run, rendered by ``monitor-summary``'s own
    ``format_slo`` over ``state/s2_slo``'s reducer, never a second definition.
    Neither ever moves ``needs_decision``: they are what the human reads, not
    what the code decides on.
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
            for rid in (spec.run_id, *sibling_run_ids(spec.run_id))
            if (rec := load_run(experiment_dir, rid)) is not None
        ]
    else:
        records = list(find_in_flight_runs(experiment_dir))

    # 3. Digest BEFORE stamping — the changed-since delta must be measured
    #    against the PRIOR watermark, not the one we are about to write.
    running_where = [digest_run(r) for r in records]
    changed = [row for row in running_where if row["changed_since_seen"]]
    anomalies = [
        {**row, "recommendation": recommendation_for(row["status"])}
        for row in running_where
        # A superseded record is a deliberate closure (superseded_by names the
        # replacement), not an anomaly needing a recovery decision — it
        # displays as superseded, never as failed/abandoned-needs-attention.
        if row["status"] in ANOMALY_STATUSES and not row["is_superseded"]
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

    # "Status-snapshot v2" (D4): the brief gains an ADDITIVE ``attention`` field —
    # this experiment's queue items in the D2-revised total order — computed by the
    # SAME ``collect_queue`` seat the ``attention-queue`` verb calls (the one
    # ordering definition, so the in-flow morning read and the standalone digest
    # cannot disagree). The snapshot never re-sorts or re-collects; it embeds the
    # ordered projection verbatim as opaque dicts. Additive only: an empty queue
    # yields ``[]`` and every other brief field is byte-unchanged.
    #
    # Computed BEFORE the acknowledge/watermark step (adversarial review F3): the
    # attention embed's ``alert`` items route through the SAME
    # ``read_unacknowledged_alerts`` that populated ``brief["alerts"]`` above.
    # Acknowledging first would advance the watermark and empty the attention
    # embed, so this one brief would list alerts its own attention field lacked —
    # and those alerts would be hidden from the standalone attention-queue verb
    # forever. Collecting first makes the brief internally consistent; the
    # acknowledge below then clears them from the FUTURE standing queue (their
    # one surfacing, as designed).
    attention = [item.as_dict() for item in collect_queue(experiment_dir, now=now_iso)]

    # 5. Re-stamp the attention watermark now that the delta AND the attention
    #    embed are computed, and acknowledge the alerts this brief just surfaced
    #    (same mark_seen gate: a peek-only snapshot moves neither watermark).
    if spec.mark_seen:
        for r in records:
            mark_seen_by_human(r.run_id, at=now_iso, experiment_dir=experiment_dir)
        if alerts:
            acknowledge_alerts(experiment_dir, up_to_ts=max(a["ts"] for a in alerts))

    # Overnight morning brief (item 8 seams 2+3): fold each digested run's overnight
    # disclosure into the snapshot when a standing consent OR any consumption exists
    # for it. Journal-first (no new SSH): the brief reads the decision journal + the
    # per-scope consumption ledger. The section surfaces failed_at vs surfaced_at
    # latency and — critically — SURVIVES consent expiry: a consent that lapsed
    # overnight still discloses what it consumed, so the disclosure outlives the
    # grant. Appears once, code-rendered; an empty result yields ``[]`` (byte-stable).
    overnight = [
        b
        for r in records
        if (b := morning_brief_if_any(experiment_dir, scope_kind="run", scope_id=r.run_id))
        is not None
    ]

    # F4a — fleet-terminal completeness: the journal ground truth an
    # experiment-scope completeness claim ("both fleets drained / all runs
    # journaled") is audited against. ``has_live`` is the ONE non-terminal
    # predicate over the digested records (it also gates the watch successor
    # below); the brief additively surfaces its inverse plus each non-terminal
    # (run_id, status) pair. Additive only — an all-terminal fleet yields
    # ``all_terminal=True`` + ``[]`` and every other brief field is byte-unchanged.
    has_live = any(r.status not in TERMINAL_STATUSES for r in records)
    non_terminal_runs = [[r.run_id, r.status] for r in records if r.status not in TERMINAL_STATUSES]

    # Run-queue paragraph (run-queue plan §3/§6 Phase 2; maintainer order
    # 2026-07-29): a stuck item must not wait silently until the human thinks to
    # ask queue-status. Computed before the dict so the key can be OMITTED when
    # the queue is empty — byte-stable for every experiment that never enqueues.
    queue_section = _queue_brief_section(experiment_dir, now_iso)

    # Parked paragraph (park-time diagnosis seam): one pointer row per parked
    # run — never the dossier content. Computed like the queue section so the
    # key is OMITTED when nothing is parked (byte-stable for park-free fleets).
    parked_section = _parked_brief_section(experiment_dir, now_iso)

    # s2-readiness pillars 1 + 6 on the surface the human reads first: the
    # standing readiness ledger's one-liner per involved cluster (verdict + AGE +
    # the sensor that is not green, CONSULTED — never probed), and the S2 SLO
    # line for each finished run, through monitor-summary's own renderer. Both
    # computed like the queue/parked sections so the key is OMITTED when there is
    # nothing to say (byte-stable for a fleet with no clusters / no finished run).
    readiness_section = _readiness_brief_section(records, now_iso)
    slo_section = _slo_brief_section(experiment_dir, records)

    brief: dict[str, Any] = {
        "now": now_iso,
        "running_where": running_where,
        "changed_since_seen": changed,
        "stalled_runs": stalled,
        "anomalies": anomalies,
        "alerts": alerts,
        "open_ssh_circuits": open_ssh_circuits,
        "attention": attention,
        "overnight": overnight,
        "fleet_terminal": {"all_terminal": not has_live, "non_terminal": non_terminal_runs},
        # Env-vs-record drift disclosure (run-12 finding 24 addendum, B15): echo
        # every exported HPC_* override verbatim on the surface an agent already
        # reads at the top of a session. The seat that let HPC_SSH_ENGINE sit
        # exported for days contradicting the durable record — pure disclosure,
        # never judged. Rides the brief dict (no wire contract). Empty when unset.
        "active_env_overrides": active_env_overrides(),
    }
    if queue_section is not None:
        brief["queue"] = queue_section
    if parked_section is not None:
        brief["parked"] = parked_section
    if readiness_section is not None:
        brief["readiness"] = readiness_section
    if slo_section is not None:
        brief["slo"] = slo_section

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
    # ``has_live`` was computed once above (F4a) and reused here.
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
        "recommendation": recommendation_for(mon.lifecycle_state),
    }


# The block-terminal store + the detached lease + the doctor dead-worker scan all
# key a detached watch under its VERB ("status-watch") — the SAME string
# ``_spawn_detached`` stamps into the lease, so ``scan_dead_detached_workers``'s
# ``read_terminal_with_fallback(run_id, lease["block"])`` finds a finished watch's
# terminal (a short "watch" key would make the scan mis-read every finished watch
# as a dead-no-terminal worker). Sourced from the ONE key derivation
# (:func:`hpc_agent.state.block_terminal.terminal_block_key`) so this recorder can
# never drift from the submit recorder / replay reader / doctor scan — the verb is
# already canonical, so this is an identity call that documents the shared seam.
_WATCH_BLOCK_KEY = terminal_block_key("status-watch")


def _detached_spec_dict(spec: StatusWatchSpec) -> dict[str, Any]:
    """Serialize *spec* with ``detach`` forced OFF for the detached child.

    The child runs the SAME monitor-poll body synchronously (its poll IS the one
    cold dial per lifetime), so its spec must carry ``detach=False`` — a truthy
    detach would fork forever (mirrors ``ops/submit_blocks._detached_spec_dict``).
    """
    return spec.model_copy(update={"detach": False}).model_dump(mode="json")


def _replay_watch_terminal(experiment_dir: Path, run_id: str) -> StatusBlockResult | None:
    """Return a finished watch's recorded terminal for the CURRENT tree, else None.

    The idempotent re-invoke seam (mirrors the S3 clean-terminal replay): after the
    detached worker reaches a genuine terminal (``watch_terminal`` / ``watch_anomaly``
    — NOT ``watch_timeout``, which is a keep-watching continuation and is
    deliberately never recorded) it records the result; a later re-invoke replays it
    with ZERO ssh instead of re-dialing a completed run. This is what makes the
    ``worker_exited → one block-drive tick`` contract surface the terminal brief
    (never re-detach). A moved/absent ``cmd_sha`` → None (re-execute).
    """
    from hpc_agent.state.block_terminal import read_terminal

    record = read_terminal(experiment_dir, run_id, _WATCH_BLOCK_KEY)
    if record is None:
        return None
    current_sha = read_run_cmd_sha(experiment_dir, run_id)
    if not current_sha or str(record.get("cmd_sha") or "") != current_sha:
        return None
    try:
        return StatusBlockResult.model_validate(record["result"])
    except (KeyError, TypeError, ValueError):
        return None


def _record_watch_terminal(experiment_dir: Path, result: StatusBlockResult) -> None:
    """Record a watch's genuine terminal so a re-invoke replays instead of re-dialing.

    Called only for the FINAL states (``watch_terminal`` / ``watch_anomaly``);
    ``watch_timeout`` is NOT recorded — it is the "keep watching" continuation, so
    recording it would replay a stale timeout and wedge the self-loop instead of
    re-spawning a fresh watch. A run with no run_id carries nothing to key on.
    """
    if not result.run_id:
        return
    from hpc_agent.state.block_terminal import record_terminal

    record_terminal(
        experiment_dir,
        run_id=result.run_id,
        block=_WATCH_BLOCK_KEY,
        cmd_sha=read_run_cmd_sha(experiment_dir, result.run_id),
        result_dump=result.model_dump(mode="json"),
    )


def _detached_watch_result(*, run_id: str, pid: int, log_path: str | None) -> StatusBlockResult:
    """The immediate-return handle for a detached watch (design §3).

    ``needs_decision`` is False (nothing to decide yet — the brief arrives on
    completion, read from the journal) and ``next_block`` is null (the journal, not
    this process, carries the next-block suggestion once the worker finishes).
    ``block_drive._chain`` exits on this via ``_is_detached`` (started / watch /
    detached_pid / stage=="detached"), so the ungated ``snapshot→watch`` hop
    becomes spawn-and-return.
    """
    brief: dict[str, Any] = {"run_id": run_id, "log_path": log_path}
    return StatusBlockResult(
        block="watch",
        stage_reached="detached",
        needs_decision=False,
        reason=(
            "status-watch detached — the monitor poll runs in a durable background "
            "worker owning the one cold dial to terminal; its brief arrives on "
            "completion (read the journal)."
        ),
        relay=render_relay("watch", "detached", brief),
        run_id=run_id,
        brief=brief,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


def _live_watch_handle(experiment_dir: Path, run_id: str) -> StatusBlockResult | None:
    """A handle for an ALREADY-LIVE detached watch worker, else None.

    The unattended cron tick re-fires ``block-drive`` while the worker is still
    polling; without this it would try to spawn a second worker and
    ``_guard_single_lease`` would raise. Peeks the journal-global lease (the SAME
    store ``wait-detached`` reads); a live pid → the "already watching" handle (no
    second spawn, no dial). A dead/absent/torn lease → None (the caller spawns; the
    single-lease reclaims the dead pid — the dead-lease re-spawn seam).
    """
    from hpc_agent._kernel.lifecycle.detached import pid_alive
    from hpc_agent.state.run_record import current_homedir

    lease_path = current_homedir() / "_detached" / f"{_WATCH_BLOCK_KEY}-{run_id}.lease.json"
    try:
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        pid = int(lease.get("pid", -1))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if pid <= 0 or not pid_alive(pid):
        return None
    return _detached_watch_result(run_id=run_id, pid=pid, log_path=lease.get("log_path"))


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

    Detach-by-contract (design §3; connection-broker.md 2026-07-07): with ``detach``
    ON (default) the ONE cold dial moves into a durable detached worker so no
    UNATTENDED path dials inline. The parent, in order: (1) replays a recorded
    terminal for the current tree (no ssh) — the ``worker_exited → one block-drive
    tick`` seam; (2) returns the handle of an already-live worker (the cron tick
    re-fires mid-watch) — no second spawn; (3) spawns a fresh worker (a dead/absent
    lease is reclaimed — the dead-lease re-spawn seam; a live sibling is refused by
    the single-lease). The child re-enters this body with ``detach=False`` and owns
    the poll to terminal, recording its genuine terminal for the replay.
    """
    if spec.detach:
        run_id = spec.monitor.run_id
        replay = _replay_watch_terminal(experiment_dir, run_id)
        if replay is not None:
            return replay
        live = _live_watch_handle(experiment_dir, run_id)
        if live is not None:
            return live
        from hpc_agent._kernel.lifecycle.detached import (
            DetachedLeaseHeld,
            launch_submit_block_detached,
        )

        try:
            launch = launch_submit_block_detached(
                verb="status-watch",
                experiment_dir=str(experiment_dir),
                spec=_detached_spec_dict(spec),
            )
        except DetachedLeaseHeld:
            # Lost the decide→spawn race to a sibling launch between the peek above
            # and the guard inside the launcher — the sibling is now the live
            # watcher, so return its handle rather than surfacing a lease error.
            live = _live_watch_handle(experiment_dir, run_id)
            if live is not None:
                return live
            raise
        return _detached_watch_result(
            run_id=launch.run_id, pid=launch.pid, log_path=launch.log_path
        )

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
        # U4 incremental harvest: how much of the finished work is already
        # readable LOCALLY. status-watch is the surface that spans the hours —
        # the detached long-poll, and submit-s3's own watch_timeout successor —
        # so it is precisely where "N complete, M pulled locally" has to appear.
        # Without it the human watching a 12-hour array through THIS block sees
        # the counts climb and learns nothing about whether any of it came home.
        # Byte accounting only; it licenses no partial aggregation.
        "incremental_harvest": mon.incremental_harvest,
    }

    if lifecycle == "complete":
        # Harvest already ran inside monitor-flow's finally (§5). Hand off the
        # marker + the next block rather than re-harvesting here.
        brief["harvest_handoff"] = {
            "guaranteed": True,
            "harvest_marker": str(harvest_marker_path(experiment_dir, mon.run_id)),
            "next_block": "aggregate-flow / submit-s4 (harvest)",
        }
        result = StatusBlockResult(
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
        # Record the genuine terminal so a re-invoke (the block-drive tick after the
        # detached worker exits) REPLAYS this hand-off instead of re-dialing a
        # completed run.
        _record_watch_terminal(experiment_dir, result)
        return result

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
    result = StatusBlockResult(
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
    # A failed/abandoned watch is a genuine terminal — record it so a re-invoke
    # replays the evidence brief (no re-dial). (watch_timeout above is a
    # keep-watching continuation and is deliberately NOT recorded.)
    _record_watch_terminal(experiment_dir, result)
    return result
