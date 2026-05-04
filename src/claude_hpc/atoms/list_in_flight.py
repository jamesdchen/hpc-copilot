"""``list-in-flight`` primitive — enumerate runs still in flight.

Pure-dispatch primitive: walks the local journal under
*experiment_dir* and projects each in-flight :class:`session.RunRecord`
to a JSON-friendly row, including a freshness annotation
(``last_status_age_seconds``) derived from the cached
``last_status.checked_at`` timestamp. No SSH, no scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc._internal import session
from claude_hpc._internal._primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


def _last_status_age_seconds(last_status: dict[str, Any] | None) -> int | None:
    """Return age in seconds of ``last_status.checked_at``, or None.

    Returns ``None`` when ``last_status`` is empty, has no ``checked_at``,
    or the timestamp is unparseable.  Callers use this to surface
    staleness to humans without changing the freshness contract of any
    SSH-mutating subcommand.
    """
    if not isinstance(last_status, dict):
        return None
    iso = last_status.get("checked_at")
    if not isinstance(iso, str):
        return None
    from claude_hpc._internal._time import parse_iso_utc_or_none, utcnow

    ts = parse_iso_utc_or_none(iso)
    if ts is None:
        return None
    delta = utcnow() - ts
    return max(0, int(delta.total_seconds()))


@primitive(
    name="list-in-flight",
    verb="query",
    side_effects=[],
    idempotent=True,
)
def list_in_flight(*, experiment_dir: Path) -> dict[str, Any]:
    """Return ``{"runs": [...]}`` enumerating in-flight runs.

    Each row carries ``run_id``, ``profile``, ``cluster``, ``job_ids``,
    ``total_tasks``, ``submitted_at``, ``last_status``,
    ``last_status_age_seconds``, and (when set) ``campaign_id``.
    """
    records = session.find_in_flight_runs(experiment_dir)

    def _row(r: session.RunRecord) -> dict[str, Any]:
        d: dict[str, Any] = {
            "run_id": r.run_id,
            "profile": r.profile,
            "cluster": r.cluster,
            "job_ids": r.job_ids,
            "total_tasks": r.total_tasks,
            "submitted_at": r.submitted_at,
            "last_status": r.last_status,
            "last_status_age_seconds": _last_status_age_seconds(r.last_status),
        }
        if r.campaign_id:
            d["campaign_id"] = r.campaign_id
        return d

    return {"runs": [_row(r) for r in records]}
