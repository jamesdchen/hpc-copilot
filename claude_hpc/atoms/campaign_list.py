"""``campaign-list`` primitive — enumerate campaigns by sidecar tag."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


@primitive(
    name="campaign-list",
    verb="query",
    side_effects=[],
    idempotent=True,
)
def campaign_list(*, experiment_dir: Path) -> dict[str, Any]:
    """List every campaign with at least one sidecar in this experiment.

    Walks every run sidecar under *experiment_dir*; skips records that
    are missing, unreadable, or carry no ``campaign_id``; counts the
    rest by campaign id. Returns the campaigns sorted alphabetically.
    """
    from claude_hpc.orchestrator.runs import find_existing_runs, read_run_sidecar

    counts: Counter[str] = Counter()
    for path in find_existing_runs(experiment_dir):
        try:
            data = read_run_sidecar(experiment_dir, path.stem)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        cid = data.get("campaign_id")
        if isinstance(cid, str) and cid:
            counts[cid] += 1
    return {
        "campaigns": [{"campaign_id": cid, "iterations": n} for cid, n in sorted(counts.items())]
    }
