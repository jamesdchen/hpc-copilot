"""Canonical scratch directory for closed-loop campaign state.

The framework writes nothing under ``campaign_dir(experiment_dir,
campaign_id)`` itself — it just returns the path and creates it
idempotently. Strategy libraries (Optuna's ``JournalFileStorage`` /
SQLite, PBT population checkpoints, walk-forward cursor files) put
their state files there. Centralising the convention here means every
adapter doesn't reinvent its own location.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["campaign_dir"]


def campaign_dir(experiment_dir: Path | str, campaign_id: str) -> Path:
    """Return ``experiment_dir/.hpc/campaigns/<campaign_id>/``, creating it.

    Raises
    ------
    ValueError
        If *campaign_id* is empty or contains a path separator. The same
        constraint applied to ``run_id`` (filesystem-safe slug) holds
        here so the directory name can never escape ``.hpc/campaigns/``.
    """
    if not campaign_id:
        raise ValueError("campaign_id must be a non-empty string")
    if "/" in campaign_id or "\\" in campaign_id or campaign_id in (".", ".."):
        raise ValueError(f"campaign_id must be filesystem-safe; got {campaign_id!r}")
    target = Path(experiment_dir) / ".hpc" / "campaigns" / campaign_id
    target.mkdir(parents=True, exist_ok=True)
    return target
