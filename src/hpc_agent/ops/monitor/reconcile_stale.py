"""``reconcile-stale`` — bulk closure of scheduler-unknown in-flight runs.

The stale-in-flight class (notebook-audit.md queue item 11, Addendum 6):
35 ``ebm_resid-*`` runs died when a CARC account association was revoked and
still read ``in_flight`` weeks later. Every unscoped surface then walks those
phantoms, and any *per-run* cluster touch pays ``35 × SSH × safe-interval``
("status-snapshot is taking forever").

This primitive is the bulk-reconcile seat. It:

1. Enumerates every non-terminal (``in_flight``) run under the experiment
   (:func:`hpc_agent.state.index.find_in_flight_runs`).
2. Issues ONE scheduler query per login node via :func:`batch_status` — never
   one qstat per run. ``batch_status`` already collapses ``(ssh_target,
   scheduler)`` groups to a single ``qstat -u $USER`` / ``squeue``, so a single
   call is the whole SSH cost regardless of run count.
3. For every run whose recorded ``job_ids`` are ALL unknown to the scheduler
   (left the queue → terminal), closes the record through the EXISTING
   :func:`hpc_agent.ops.monitor.classify.settle` classifier — no status is
   invented here. With no per-run reporter summary (the cost this seat exists
   to avoid), ``settle({}, total_tasks)`` yields ``abandoned`` with reason
   ``no_on_disk_evidence`` — the same terminal verdict reconcile's settle arm
   reaches for "nothing alive + no positive evidence".
4. For a run that never recorded ``job_ids`` (submit died before the ids were
   stamped) it has no scheduler reading to check, so it is closed ONLY when its
   record predates the staleness threshold; a recent jobless record stays open
   (it may be mid-submit).

**Never-actuate posture.** Closing a record is a *journal classification*, not
a cluster action: the terminal write goes through the same ``update_run_status``
+ ``mark_run`` journal primitives reconcile uses, but — unlike reconcile's
settle arm — it does NOT harvest (an rsync pull + reduce is a per-run cluster
touch, and these runs' cluster is presumed gone). Anything the seat cannot
prove terminal — the scheduler still knows a job, a query could not run, a
young jobless record, a run ``batch_status`` could not batch (pure-API /
unresolvable scheduler) — stays ``in_flight`` and is listed in ``left_open``.

Idempotent: a re-run finds the just-closed records terminal, so
``find_in_flight_runs`` no longer returns them and the second pass is a no-op.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.monitor.batch_status import batch_status
from hpc_agent.ops.monitor.classify import settle
from hpc_agent.state.journal import mark_run, update_run_status

if TYPE_CHECKING:
    from pathlib import Path

#: A run that never recorded ``job_ids`` has no scheduler reading to key on, so
#: age is the only evidence: a record older than this many hours whose ids were
#: never stamped is submit residue (the process died before ``submit_and_record``
#: stamped the ids), not a live submit-in-progress. Conservative default — a
#: same-day jobless record stays open.
STALE_AFTER_HOURS_DEFAULT = 24


def _close_record(
    experiment_dir: Path,
    run_id: str,
    *,
    prior_last_status: dict[str, Any] | None,
    verdict: str,
    reason: str,
    now: str,
    evidence: dict[str, Any],
) -> None:
    """Mark *run_id* terminal via the EXISTING journal primitives — no harvest.

    Records the settle ``verdict_reason`` (same field reconcile writes) plus the
    bulk-closure provenance, then flips the journal status. Deliberately does
    NOT call ``harvest_on_terminal``: bulk closure is a journal classification,
    never a cluster action (the run's cluster is presumed unreachable).
    """
    recorded = {
        **(prior_last_status or {}),
        "verdict_reason": reason,
        "closed_by": "reconcile-stale",
        "checked_at": now,
        "stale_closure_evidence": evidence,
    }
    update_run_status(experiment_dir, run_id, last_status=recorded)
    mark_run(experiment_dir, run_id, status=verdict)


@primitive(
    name="reconcile-stale",
    verb="mutate",
    side_effects=[
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (under flock) — "
            "terminal close for scheduler-unknown in-flight runs",
        ),
        SideEffect("ssh", "<cluster> (one scheduler-state query per login node, via batch-status)"),
    ],
    # ``batch_status`` can raise ``SshUnreachable``; this primitive CATCHES it
    # (a cluster we cannot reach leaves its runs open — never actuate on a blip),
    # so the only error that escapes the body is a bad ``--now`` (``SpecInvalid``).
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    # The whole experiment is the closure scope: a re-run finds the just-closed
    # records terminal (no longer in-flight), so the second pass is a no-op.
    idempotency_key="experiment_dir",
    cli=CliShape(
        verb="reconcile-stale",
        requires_ssh=True,
        experiment_dir_arg=True,
        args=(
            CliArg(
                flag="--now",
                required=False,
                help="ISO-8601 UTC instant to evaluate the staleness threshold against "
                "(deterministic testing); defaults to now.",
            ),
            CliArg(
                flag="--stale-after-hours",
                type=int,
                default=STALE_AFTER_HOURS_DEFAULT,
                required=False,
                help="A run that never recorded job_ids is closed only if its record is "
                f"older than this many hours (default {STALE_AFTER_HOURS_DEFAULT}).",
            ),
        ),
        help=(
            "Bulk-close in-flight runs the scheduler no longer knows: ONE scheduler "
            "query per login node (via batch-status), then every run whose job_ids "
            "are all unknown is closed through the existing reconcile settle "
            "classification (abandoned/no-evidence) — never one-by-one SSH, never a "
            "cluster action. Ambiguous runs stay open and are listed."
        ),
    ),
    agent_facing=True,
)
def reconcile_stale(
    *,
    experiment_dir: Path,
    now: str | None = None,
    stale_after_hours: int = STALE_AFTER_HOURS_DEFAULT,
) -> dict[str, Any]:
    """Close scheduler-unknown in-flight runs in bulk; return a code-rendered summary.

    Returns::

        {
          "examined": int,           # in-flight runs looked at
          "queries": int,            # scheduler queries issued (== login nodes), 0 if unreachable
          "closed_count": int,
          "closed_by_class": {"abandoned": int, ...},
          "closed": [{"run_id", "verdict", "reason"}, ...],
          "left_open_count": int,
          "left_open": [{"run_id", "reason"}, ...],
          "unreachable": bool,       # batch-status could not reach the cluster(s)
          "summary": str,            # one-line human digest (code-rendered)
        }

    Raises :class:`errors.SpecInvalid` if *now* is a non-ISO-8601 string.
    """
    from hpc_agent.state.index import find_in_flight_runs

    now_iso = (now or "").strip() or utcnow_iso()
    now_dt = parse_iso_utc_or_none(now_iso)
    if now_dt is None:
        raise errors.SpecInvalid(f"reconcile-stale: now override {now!r} is not ISO-8601 UTC")

    records = find_in_flight_runs(experiment_dir)
    examined = len(records)

    closed: list[dict[str, str]] = []
    left_open: list[dict[str, str]] = []
    closed_by_class: Counter[str] = Counter()

    # ONE scheduler query per login node for ALL in-flight runs — the connection
    # -storm fix reused. A single unreachable login node makes batch-status
    # raise (it never silently zeroes the runs sharing it): treat that as
    # "cannot verify" and leave EVERY run open, never actuating on a blip.
    try:
        batch = batch_status(experiment_dir=experiment_dir)
    except errors.SshUnreachable as exc:
        for r in records:
            left_open.append({"run_id": r.run_id, "reason": f"batch_status_unreachable: {exc}"})
        summary = (
            f"reconcile-stale: examined {examined} in-flight run(s); scheduler unreachable "
            f"({exc}) — closed 0, left {examined} open (never actuate on a connectivity blip)."
        )
        return {
            "examined": examined,
            "queries": 0,
            "closed_count": 0,
            "closed_by_class": {},
            "closed": [],
            "left_open_count": len(left_open),
            "left_open": left_open,
            "unreachable": True,
            "summary": summary,
        }

    runs_status: dict[str, Any] = batch.get("runs", {})
    skipped_reason = {s["run_id"]: s.get("reason", "not_batched") for s in batch.get("skipped", [])}
    stale_after = timedelta(hours=max(0, int(stale_after_hours)))

    for r in records:
        info = runs_status.get(r.run_id)
        if info is not None:
            # batch-status got a scheduler reading for this run's login node.
            if info.get("job_states"):
                # The scheduler still knows at least one job — alive/known, not
                # stale. Leave it to the normal monitor/reconcile path.
                left_open.append({"run_id": r.run_id, "reason": "scheduler_still_knows"})
                continue
            # ALL recorded job_ids are unknown to the scheduler → they left the
            # queue → terminal. With no reporter summary, settle({}, N) yields
            # abandoned/no_on_disk_evidence — the existing classification, no
            # invented status.
            decision = settle({}, r.total_tasks)
            _close_record(
                experiment_dir,
                r.run_id,
                prior_last_status=r.last_status,
                verdict=str(decision.verdict),
                reason=decision.reason,
                now=now_iso,
                evidence={
                    "unknown_job_ids": list(info.get("missing_job_ids", [])),
                    "reader": "batch-status",
                },
            )
            closed.append(
                {"run_id": r.run_id, "verdict": str(decision.verdict), "reason": decision.reason}
            )
            closed_by_class[str(decision.verdict)] += 1
            continue

        # Not in runs_status: batch-status could not batch this run.
        reason = skipped_reason.get(r.run_id, "not_batched")
        if reason == "no_job_ids":
            # No scheduler reading is possible (nothing to query). Age is the
            # only evidence: close a record that predates the staleness
            # threshold, leave a recent one open (it may be mid-submit).
            submitted_dt = parse_iso_utc_or_none(r.submitted_at)
            if submitted_dt is not None and (now_dt - submitted_dt) >= stale_after:
                decision = settle({}, r.total_tasks)
                _close_record(
                    experiment_dir,
                    r.run_id,
                    prior_last_status=r.last_status,
                    verdict=str(decision.verdict),
                    reason=decision.reason,
                    now=now_iso,
                    evidence={
                        "no_job_ids": True,
                        "submitted_at": r.submitted_at,
                        "stale_after_hours": int(stale_after_hours),
                    },
                )
                closed.append(
                    {
                        "run_id": r.run_id,
                        "verdict": str(decision.verdict),
                        "reason": decision.reason,
                    }
                )
                closed_by_class[str(decision.verdict)] += 1
            else:
                left_open.append({"run_id": r.run_id, "reason": "no_job_ids_too_recent"})
        else:
            # pure_api_backend / unresolvable_scheduler / anything else we could
            # not read — ambiguous, never closed blind.
            left_open.append({"run_id": r.run_id, "reason": reason})

    class_bits = ", ".join(f"{k}:{v}" for k, v in sorted(closed_by_class.items())) or "none"
    summary = (
        f"reconcile-stale: examined {examined} in-flight run(s) across "
        f"{batch.get('queries', 0)} scheduler query/queries; closed {len(closed)} "
        f"({class_bits}); left {len(left_open)} open."
    )
    return {
        "examined": examined,
        "queries": int(batch.get("queries", 0)),
        "closed_count": len(closed),
        "closed_by_class": dict(closed_by_class),
        "closed": closed,
        "left_open_count": len(left_open),
        "left_open": left_open,
        "unreachable": False,
        "summary": summary,
    }
