"""Campaign cursor — atomic iteration counter at ``<campaign_dir>/cursor.json``.

For most strategies, "iterations completed" is just "count of sidecars
tagged with the campaign_id" — :func:`campaign_status` already returns
that. The cursor exists for strategies where sidecar count and
iteration count diverge: branching campaigns (PBT generations,
hyperband brackets) submit multiple sidecars per "iteration", and
walk-forward CV may need an explicit step counter independent of which
fold was last submitted.

The framework provides atomicity (advisory flock + atomic rename) so
concurrent submits can't tear the counter; what "iteration" means is
the strategy's call.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc._internal.io import atomic_locked_update
from claude_hpc._internal.time import utcnow_iso
from claude_hpc.campaign.dirs import campaign_dir

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "CURSOR_FILENAME",
    "CURSOR_SCHEMA_VERSION",
    "advance_cursor",
    "cursor_path",
    "read_cursor",
]

CURSOR_SCHEMA_VERSION: int = 1
CURSOR_FILENAME: str = "cursor.json"


def cursor_path(experiment_dir: Path | str, campaign_id: str) -> Path:
    """Return ``<experiment_dir>/.hpc/campaigns/<campaign_id>/cursor.json``.

    Creates the parent directory idempotently via :func:`campaign_dir`.
    """
    return campaign_dir(experiment_dir, campaign_id) / CURSOR_FILENAME


def read_cursor(experiment_dir: Path | str, campaign_id: str) -> dict[str, Any] | None:
    """Return the current cursor state, or ``None`` if no cursor exists."""
    path = cursor_path(experiment_dir, campaign_id)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def advance_cursor(
    experiment_dir: Path | str,
    campaign_id: str,
    *,
    last_run_id: str = "",
) -> dict[str, Any]:
    """Atomically increment the cursor and return the new state.

    Concurrent submits are safe: writes happen under ``fcntl`` advisory
    lock with an atomic rename. ``last_run_id`` is recorded as
    metadata; it is never read back by the framework.
    """
    path = cursor_path(experiment_dir, campaign_id)

    def _bump(doc: dict[str, Any] | None) -> dict[str, Any]:
        prior_iter = 0
        if isinstance(doc, dict):
            raw = doc.get("iteration", 0)
            if isinstance(raw, int):
                prior_iter = raw
        return {
            "cursor_schema_version": CURSOR_SCHEMA_VERSION,
            "iteration": prior_iter + 1,
            "last_run_id": last_run_id,
            "updated_at": utcnow_iso(),
        }

    atomic_locked_update(path, _bump)
    new_state = read_cursor(experiment_dir, campaign_id)
    assert new_state is not None  # we just wrote it
    return new_state
