"""``doctor`` — driver-watchdog scan (§5 dead-man's switch).

A read-only ``query`` primitive. Scans live (``in_flight``) runs for a missed
driver-tick deadline — a ``next_tick_due`` stamped by
:func:`hpc_agent.state.journal.stamp_tick` that is now in the past — and surfaces
each as a DRAFTED recovery proposal plus the detection evidence.

Detection is the watchdog's *whole* job. It NEVER restarts or re-arms anything
(design §5: "The watchdog never restarts anything") — safe recovery is already
guaranteed by tick idempotency, so the human just decides *whether* to re-arm.
This is the deterministic verb an OS-scheduled task (Task Scheduler / cron) runs
out-of-session; the watch-the-watcher recursion bottoms out at the OS scheduler.

Pure local filesystem read — the per-run journal records under
``~/.claude/hpc/<repo>/``. No SSH, no scheduler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.doctor import DoctorResult, DoctorSpec, StalledRunProposal
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.state.index import find_stalled_runs


def _overdue_seconds(next_tick_due: str | None, now: str) -> int | None:
    """Whole seconds by which *next_tick_due* precedes *now*, or ``None``."""
    due_dt = parse_iso_utc_or_none(next_tick_due)
    now_dt = parse_iso_utc_or_none(now)
    if due_dt is None or now_dt is None:
        return None
    return max(0, int((now_dt - due_dt).total_seconds()))


def _draft_proposal(stalled: dict[str, Any], *, now: str) -> StalledRunProposal:
    """Turn one ``find_stalled_runs`` hit into a drafted (never-enacted) proposal."""
    last_tick_at = stalled.get("last_tick_at")
    next_tick_due = stalled.get("next_tick_due")
    overdue = _overdue_seconds(next_tick_due, now)
    since = last_tick_at or "an unknown time"
    proposal = (
        f"driver stalled since {since}, status {stalled.get('status')}: next tick was due "
        f"{next_tick_due} but has not fired (now {now}). Re-arm the driver? "
        f"Re-running is safe — tick idempotency loses nothing."
    )
    return StalledRunProposal(
        run_id=stalled["run_id"],
        status=stalled.get("status", "in_flight"),
        last_tick_at=last_tick_at,
        next_tick_due=next_tick_due,
        cluster=stalled.get("cluster"),
        ssh_target=stalled.get("ssh_target"),
        proposal=proposal,
        evidence={
            "last_tick_at": last_tick_at,
            "next_tick_due": next_tick_due,
            "now": now,
            "overdue_seconds": overdue,
        },
    )


@primitive(
    name="doctor",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Driver watchdog (dead-man's switch). Scan live runs for a missed "
            "driver-tick deadline and surface each as a DRAFTED recovery proposal "
            "plus the evidence. Read-only, no SSH, no scheduler. It NEVER restarts "
            "or re-arms anything — detection is its whole job; safe recovery is "
            "guaranteed by tick idempotency. Run it out-of-session from an OS "
            "scheduler (Task Scheduler / cron)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=DoctorSpec,
        schema_ref=SchemaRef(input="doctor"),
    ),
    agent_facing=True,
)
def doctor(*, experiment_dir: Path, spec: DoctorSpec) -> dict[str, Any]:
    """Scan for stalled drivers under *experiment_dir*; return drafted proposals.

    *spec.now* optionally overrides the evaluation instant (for deterministic
    testing); it defaults to the current UTC time. Each live run whose stamped
    ``next_tick_due`` is before that instant is a stalled driver — returned with
    a drafted recovery proposal the human decides on. No side effects.

    Raises :class:`errors.SpecInvalid` if *spec.now* is a non-ISO-8601 string.
    """
    experiment_dir = Path(experiment_dir)
    now = (spec.now or "").strip() or utcnow_iso()
    if parse_iso_utc_or_none(now) is None:
        raise errors.SpecInvalid(f"doctor: now override {spec.now!r} is not ISO-8601 UTC")

    stalled = find_stalled_runs(now, experiment_dir=experiment_dir)
    proposals = [_draft_proposal(hit, now=now) for hit in stalled]
    result = DoctorResult(now=now, stalled_count=len(proposals), stalled=proposals)
    dumped: dict[str, Any] = result.model_dump(mode="json")

    # Opt-in (§5): the OS-scheduled scan surfaces stalls as an OS notification
    # instead of printing JSON no one reads. Notify only — never acts. Default
    # spec.notify is False, so the plain in-session verb is unchanged.
    if spec.notify and proposals:
        from hpc_agent.ops.recover.notify import raise_stall_notification

        raise_stall_notification(dumped["stalled"], experiment_dir=experiment_dir)

    return dumped
