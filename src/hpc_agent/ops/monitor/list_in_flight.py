"""``list-in-flight`` primitive — enumerate runs still in flight.

Pure-dispatch primitive: walks the local journal under
*experiment_dir* and projects each in-flight :class:`RunRecord`
to a JSON-friendly row, including a freshness annotation
(``last_status_age_seconds``) derived from the cached
``last_status.checked_at`` timestamp. No SSH, no scheduler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape

# The canonical implementation lives in ``hpc_agent.infra.time`` so
# ``meta/campaign/atoms/load_context.py`` can reach it without crossing
# into this subject. Re-exported under the underscore-prefixed back-
# compat alias for the local primitive call below; the lint allow-list
# entry that used to gate this cross-subject hop is gone.
from hpc_agent.infra.time import status_age_seconds as _last_status_age_seconds
from hpc_agent.state.index import find_in_flight_runs

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord


@primitive(
    name="list-in-flight",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help="List runs with status=in_flight in the journal (recovery path).",
        experiment_dir_arg=True,
    ),
    agent_facing=True,
)
def list_in_flight(*, experiment_dir: Path) -> dict[str, Any]:
    """Return ``{"runs": [...]}`` enumerating in-flight runs.

    Each row carries ``run_id``, ``profile``, ``cluster``, ``job_ids``,
    ``total_tasks``, ``submitted_at``, ``last_status``,
    ``last_status_age_seconds``, and (when set) ``campaign_id``.
    """
    records = find_in_flight_runs(experiment_dir)

    def _row(r: RunRecord) -> dict[str, Any]:
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
