"""``alerts-ack`` — advance the doctor.alerts watchdog acknowledgment watermark (§5).

A thin maintenance wrapper over :func:`hpc_agent.ops.recover.notify.acknowledge_alerts`.
Proving run #3 gave the alert log a "seen" watermark, but until now the ONLY way
to advance it was as a side effect of a ``status-snapshot --mark-seen`` — so a
human who saw the alerts elsewhere (the ``doctor`` envelope, the SessionStart
count hook) had no direct way to dismiss them. This verb closes that gap: it
advances the watermark to the newest recorded alert (or a caller-supplied
instant) and reports how many it cleared.

Notify only, never act (§5): acknowledging surfaces nothing on the cluster and
NEVER truncates the log (an append-only audit trail) — it only moves the watermark
that decides which alerts still count as "new".
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.alerts_ack import AlertsAckResult, AlertsAckSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.recover.notify import (
    acknowledge_alerts,
    newest_alert_ts,
    read_unacknowledged_alerts,
)


@primitive(
    name="alerts-ack",
    verb="mutate",
    side_effects=[
        SideEffect("file_write", "~/.claude/hpc/<repo_hash>/doctor.alerts.seen"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Acknowledge the unacknowledged `doctor` watchdog alerts for this "
            "experiment dir — advance the doctor.alerts 'seen' watermark to the "
            "newest recorded alert (or an --spec up_to_ts) so they stop surfacing "
            "as new (proving run #3: detection without delivery is silence). The "
            "same watermark a status snapshot advances via --mark-seen, exposed "
            "standalone. Monotonic + idempotent: re-running never resurrects an "
            "already-acknowledged alert. Notify only — it never truncates the "
            "append-only alert log and never touches the cluster."
        ),
        spec_arg=True,
        spec_required=False,
        experiment_dir_arg=True,
        spec_model=AlertsAckSpec,
        schema_ref=SchemaRef(input="alerts_ack"),
    ),
    agent_facing=True,
)
def alerts_ack(*, experiment_dir: Path, spec: AlertsAckSpec | None = None) -> AlertsAckResult:
    """Advance the alert acknowledgment watermark under *experiment_dir*.

    Targets ``spec.up_to_ts`` when supplied, else the newest alert currently in
    the log, else ``now`` (an empty/unreadable log — the watermark simply moves
    to now, a harmless no-op with nothing to acknowledge). Delegates the actual,
    monotonic watermark write to :func:`notify.acknowledge_alerts`; reports how
    many alerts the advance cleared (before − after) and how many remain.
    """
    experiment_dir = Path(experiment_dir)
    now = utcnow_iso()
    up_to = spec.up_to_ts if spec is not None else None
    target = up_to or newest_alert_ts(experiment_dir) or now

    # Clamp a caller-supplied FUTURE watermark to now. A spec ``up_to_ts`` of
    # e.g. 2099-01-01 would otherwise pre-acknowledge alerts that do not exist
    # yet: every alert later written (its ``ts`` is stamped at utcnow, always
    # < 2099) would land at-or-before the watermark and never surface. Clamping
    # to ``now`` is safe — a real recorded alert's ts is never in the future, so
    # the cap only ever trims an unreachable caller value, never a live alert.
    # The clamp lives HERE (the verb owns caller input); ``acknowledge_alerts``
    # keeps its pure monotonic contract, so ``status-snapshot --mark-seen`` and
    # every other caller share one definition of "advance the watermark".
    now_dt = parse_iso_utc_or_none(now)
    target_dt = parse_iso_utc_or_none(target)
    if now_dt is not None and target_dt is not None and target_dt > now_dt:
        target = now

    before = len(read_unacknowledged_alerts(experiment_dir))
    acknowledge_alerts(experiment_dir, up_to_ts=target)
    after = len(read_unacknowledged_alerts(experiment_dir))

    return AlertsAckResult(
        acknowledged_up_to=target,
        acknowledged_count=max(0, before - after),
        remaining=after,
    )
