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

from hpc_agent import errors
from hpc_agent.infra.io import atomic_locked_update
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.meta.campaign.dirs import campaign_dir

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
    """Return the current cursor state, or ``None`` if no cursor exists.

    Forward-compat guard: a cursor on disk with a
    ``cursor_schema_version`` greater than the current
    :data:`CURSOR_SCHEMA_VERSION` raises
    :class:`~hpc_agent.errors.JournalCorrupt` (as does a non-integer
    version — see below). The user is
    running an older framework binary against a cursor that a newer
    binary wrote, and silently parsing it could mis-interpret fields the
    older binary doesn't understand.

    Lower versions are accepted (backward-compat) — the field schema is
    additive within a major version, and older cursors round-trip
    through :func:`advance_cursor` which rewrites them at the current
    version. Future schema bumps should land a migration alongside the
    bump (here, or in a dedicated migrator) before raising on the older
    version.
    """
    path = cursor_path(experiment_dir, campaign_id)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    on_disk_version = data.get("cursor_schema_version")
    if on_disk_version is None:
        # Missing version key — likely a pre-v1 cursor from before the
        # version field was introduced. Treat as v1 explicitly so a
        # future v2 reader can detect "no version OR version < 2" and
        # apply v1 → v2 backfills.
        on_disk_version = 1
        data["cursor_schema_version"] = 1
    elif not isinstance(on_disk_version, int):
        # Non-int value (string, null after JSON manual edit, etc.) is
        # state corruption — surface as ``JournalCorrupt`` so callers
        # branch the same way they do for a torn run record.
        raise errors.JournalCorrupt(
            f"cursor at {path} declares non-integer cursor_schema_version="
            f"{on_disk_version!r}; wipe the cursor or fix the file"
        )
    if on_disk_version > CURSOR_SCHEMA_VERSION:
        raise errors.JournalCorrupt(
            f"cursor at {path} declares cursor_schema_version={on_disk_version}, "
            f"newer than this framework's CURSOR_SCHEMA_VERSION={CURSOR_SCHEMA_VERSION}; "
            f"upgrade hpc-agent to read this cursor"
        )
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

    # Use the doc returned by atomic_locked_update directly. A second
    # read_cursor() outside the lock would race with concurrent writers
    # and could observe a later iteration than this caller's bump.
    return atomic_locked_update(path, _bump)
