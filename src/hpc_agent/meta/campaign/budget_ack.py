"""Budget-halt acknowledgement — durable record at ``<campaign_dir>/budget_ack.json``.

The budget governor (#224) makes ``stop_over_budget`` a halt the loop
**cannot** silently sail past: once realised spend meets a cap,
:func:`campaign_advance` keeps returning ``stop_over_budget`` until the
operator (or the agent, deliberately) acknowledges it.

The acknowledgement is a *snapshot* of the realised spend at the moment
it was acknowledged. Because campaign spend is monotonic, an ack made at
spend ``S`` only authorises continuing while spend stays at ``S``: the
moment one more task burns compute, ``S' > S`` and the ack goes stale,
re-arming the halt. That makes a bare acknowledgement self-limiting — it
buys exactly one more leg, not an open-ended bypass — while *raising the
cap* (recorded alongside the ack and written through to the manifest)
buys real headroom. Either way the decision is explicit and audited.

Pure I/O, mirroring :mod:`hpc_agent.meta.campaign.cursor`: advisory
flock + atomic rename, defensive parse (a malformed ack is treated as
absent rather than crashing the advance read).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from hpc_agent.infra.io import atomic_locked_update
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.meta.campaign.dirs import campaign_dir

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "BUDGET_ACK_FILENAME",
    "BUDGET_ACK_SCHEMA_VERSION",
    "ack_covers_spend",
    "ack_path",
    "read_budget_ack",
    "write_budget_ack",
]

BUDGET_ACK_SCHEMA_VERSION: int = 1
BUDGET_ACK_FILENAME: str = "budget_ack.json"

# The realised-spend metrics an ack snapshots. ``core_hours`` is a float
# (compared with a tolerance); the rest are integer counters.
_INT_METRICS: tuple[str, ...] = ("jobs", "tasks", "walltime_sec")
_FLOAT_METRICS: tuple[str, ...] = ("core_hours",)
# Float slop so a re-read of the same spend (after JSON round-trip) is not
# spuriously judged "grown".
_CORE_HOURS_TOL: float = 1e-6


def ack_path(experiment_dir: Path | str, campaign_id: str) -> Path:
    """Return ``<experiment_dir>/.hpc/campaigns/<campaign_id>/budget_ack.json``.

    Creates the parent directory idempotently via :func:`campaign_dir`.
    """
    return campaign_dir(experiment_dir, campaign_id) / BUDGET_ACK_FILENAME


def read_budget_ack(experiment_dir: Path | str, campaign_id: str) -> dict[str, Any] | None:
    """Return the current acknowledgement, or ``None`` if none / unreadable.

    Defensive by design: a missing file, malformed JSON, or a non-object
    payload all read as "no acknowledgement" so a corrupt ack can never
    relax the budget halt — the safe default is to keep halting.
    """
    path = ack_path(experiment_dir, campaign_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_budget_ack(
    experiment_dir: Path | str,
    *,
    campaign_id: str,
    acknowledged_spend: dict[str, Any],
    raised_caps: dict[str, Any] | None = None,
    note: str = "",
    acknowledged_at: str | None = None,
) -> Path:
    """Write the acknowledgement atomically and return its path.

    *acknowledged_spend* is the realised-spend snapshot (the ``spent``
    block from :func:`campaign_budget`) that this ack authorises. Spend
    growing past any metric here re-arms the halt. *raised_caps* records
    the new caps the operator set in the same gesture (audit only — the
    caller is responsible for writing them through to the manifest).
    """
    payload: dict[str, Any] = {
        "budget_ack_schema_version": BUDGET_ACK_SCHEMA_VERSION,
        "campaign_id": campaign_id,
        "acknowledged_at": acknowledged_at or utcnow_iso(),
        "acknowledged_spend": dict(acknowledged_spend),
        "raised_caps": dict(raised_caps) if raised_caps else None,
        "note": note,
    }
    target = ack_path(experiment_dir, campaign_id)
    # Serialise on the same campaign-dir flock the cursor / manifest use so a
    # concurrent advance reads a whole ack, never a torn one.
    atomic_locked_update(target, lambda _existing: payload)
    return target


def ack_covers_spend(ack: dict[str, Any], spent: dict[str, Any]) -> bool:
    """Does *ack* still authorise the current realised *spent*?

    True iff every snapshotted spend metric is still ``>=`` the current
    realised value — i.e. spend has not grown past what was acknowledged.
    Missing snapshot metrics are treated as ``0`` (an ack that predates a
    metric cannot cover any positive spend on it), which keeps the guard
    conservative: when in doubt, the halt stays armed.
    """
    snapshot = ack.get("acknowledged_spend")
    if not isinstance(snapshot, dict):
        return False
    for key in _INT_METRICS:
        current = int(spent.get(key, 0) or 0)
        acked = int(snapshot.get(key, 0) or 0)
        if current > acked:
            return False
    for key in _FLOAT_METRICS:
        current_f = float(spent.get(key, 0.0) or 0.0)
        acked_f = float(snapshot.get(key, 0.0) or 0.0)
        if current_f > acked_f + _CORE_HOURS_TOL:
            return False
    return True
