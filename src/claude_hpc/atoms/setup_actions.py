"""``suggest-setup-action`` primitive — pick the right /submit-hpc Setup branch.

Replaces the priority-list prose at /submit-hpc Step 0 ("In-flight run
journal" → "Previous run" → "tasks.py exists" → "fresh") with a
deterministic primitive that runs all four checks and returns the
recommended action + the run_ids the agent needs to surface to the
user.

The agent's job collapses to: call the primitive, branch on
``data.action``, surface the candidate run_ids verbatim. No
priority-list-walking prose, no "is the journal newer than the
sidecar" judgment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from claude_hpc import errors
from claude_hpc._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path


def _summarize_record(record: Any) -> dict[str, Any]:
    """Render a RunRecord as the dict shape /submit-hpc Step 0 needs."""
    return {
        "run_id": record.run_id,
        "profile": record.profile,
        "cluster": record.cluster,
        "job_ids": list(record.job_ids),
        "total_tasks": int(record.total_tasks),
        "submitted_at": record.submitted_at,
        "campaign_id": record.campaign_id or None,
        "last_status": (dict(record.last_status) if isinstance(record.last_status, dict) else None),
    }


def _summarize_sidecar(path: Any) -> dict[str, Any]:
    """Parse a per-experiment sidecar path into the Setup priority-1 shape."""
    import json

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    return {
        "run_id": path.stem,
        "profile": data.get("profile"),
        "cluster": data.get("cluster"),
        "campaign_id": data.get("campaign_id") or None,
        "submitted_at": data.get("submitted_at"),
        "task_count": int(data.get("task_count") or 0),
    }


@primitive(
    name="suggest-setup-action",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-agent suggest-setup-action --experiment-dir <path>",
    agent_facing=True,
)
def suggest_setup_action(experiment_dir: Path) -> dict[str, Any]:
    """Run the /submit-hpc Setup priority cascade and recommend an action.

    Priority order (matches the slash command's prose):

    * **0 — `monitor`**: at least one in-flight run exists in the
      journal. Action: hand off to ``/monitor-hpc <run_id>`` rather
      than starting a new submit. ``candidates`` lists every in-flight
      record so the agent can ask which one if multiple.
    * **1 — `reuse`**: at least one per-experiment sidecar exists at
      ``<experiment>/.hpc/runs/<run_id>.json`` (a previous submit).
      Action: surface the recent (profile, cluster) pairs so the user
      can pick "same as last <profile>". ``candidates`` is the
      newest-first list.
    * **2 — `interview`**: ``.hpc/tasks.py`` exists but no run
      sidecars yet. Action: skip the executor-discovery + axes
      interview (tasks.py already encodes the axis); jump to Step 4b
      (planner) → Step 6c (cmd_sha + sidecar).
    * **3 — `fresh`**: nothing exists yet. Action: full interview
      starting at Step 1.

    Returns
    -------
    ``{priority, action, recommended_run_id, candidates, reason}``.
    ``recommended_run_id`` is the single best candidate (newest by
    submitted_at / mtime); ``candidates`` is the full list at that
    priority for the agent to surface to the user. ``reason`` is a
    one-line human-readable explanation of why this priority was
    picked.

    The field is named ``recommended_run_id`` (not ``run_id``) so the
    schema-defs consistency check — which forbids nullable ``run_id``
    in any output — stays satisfied while still letting priorities 2/3
    return null for "no specific run."
    """
    if experiment_dir is None:
        raise errors.SpecInvalid("experiment_dir is required")

    from claude_hpc._internal import session
    from claude_hpc.state.runs import find_existing_runs

    # Priority 0 — in-flight runs.
    try:
        in_flight = session.find_in_flight_runs(experiment_dir)
    except (OSError, ValueError):
        in_flight = []
    if in_flight:
        candidates = [_summarize_record(r) for r in in_flight]
        return {
            "priority": 0,
            "action": "monitor",
            "recommended_run_id": candidates[0]["run_id"],
            "candidates": candidates,
            "reason": (
                f"{len(in_flight)} in-flight run(s) on the journal — "
                "switch to /monitor-hpc rather than re-submitting."
            ),
        }

    # Priority 1 — per-experiment sidecars (any prior submit).
    sidecars = find_existing_runs(experiment_dir)
    if sidecars:
        candidates = [_summarize_sidecar(p) for p in sidecars[:10]]
        return {
            "priority": 1,
            "action": "reuse",
            "recommended_run_id": candidates[0]["run_id"],
            "candidates": candidates,
            "reason": (
                f"{len(sidecars)} previous run(s) on disk — offer the user "
                'one of the recent (profile, cluster) pairs as "same as last".'
            ),
        }

    # Priority 2 — tasks.py exists, no run history yet.
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    if tasks_py.is_file():
        return {
            "priority": 2,
            "action": "interview",
            "recommended_run_id": None,
            "candidates": [],
            "reason": (
                ".hpc/tasks.py exists but no run history — skip executor "
                "discovery and the axes interview; jump to the planner."
            ),
        }

    # Priority 3 — fresh experiment.
    return {
        "priority": 3,
        "action": "fresh",
        "recommended_run_id": None,
        "candidates": [],
        "reason": "no in-flight runs, no sidecars, no tasks.py — full interview.",
    }


@primitive(
    name="find-prior-run",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="cmd_sha",
    cli="hpc-agent find-prior-run --experiment-dir <path> --cmd-sha <hex>",
    agent_facing=True,
)
def find_prior_run(
    experiment_dir: Path,
    *,
    cmd_sha: str,
) -> dict[str, Any]:
    """Look up a prior run by ``cmd_sha`` for resume detection.

    Wraps :func:`claude_hpc.state.runs.find_run_by_cmd_sha` + a sidecar
    read so the slash command's Step 6c ("I found a prior run with the
    same cmd_sha — resume or fresh?") routes through one CLI call
    instead of inline Python.

    Returns
    -------
    ``{found, prior_run_id, is_orphan, status, age_sec, profile,
    cluster, job_ids, campaign_id, submitted_at}``. ``found=False``
    when no sidecar matches and the rest of the keys are None/empty —
    distinguishes "no prior run" from "prior run is orphan." The field
    is named ``prior_run_id`` (not ``run_id``) so the schema-defs
    consistency check — which forbids nullable ``run_id`` keys — stays
    happy on the ``found=False`` branch.

    The ``is_orphan`` field signals the half-baked-sidecar case
    (sidecar on disk but no journal job_ids). Resume detection should
    treat orphans as not-a-real-prior; the slash command can offer
    ``prune-orphan-sidecars`` to clean them up explicitly.
    """
    if not cmd_sha:
        raise errors.SpecInvalid("cmd_sha is required")

    import json
    import time

    from claude_hpc.state.runs import find_run_by_cmd_sha, is_orphan_sidecar

    path = find_run_by_cmd_sha(experiment_dir, cmd_sha)
    if path is None:
        return {
            "found": False,
            "prior_run_id": None,
            "is_orphan": False,
            "status": None,
            "age_sec": None,
            "profile": None,
            "cluster": None,
            "job_ids": [],
            "campaign_id": None,
            "submitted_at": None,
        }

    run_id = path.stem
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}

    age_sec: int | None = None
    try:
        age_sec = int(time.time() - path.stat().st_mtime)
    except OSError:
        age_sec = None

    return {
        "found": True,
        "prior_run_id": run_id,
        "is_orphan": is_orphan_sidecar(experiment_dir, run_id),
        "status": data.get("status"),
        "age_sec": age_sec,
        "profile": data.get("profile"),
        "cluster": data.get("cluster"),
        "job_ids": list(data.get("job_ids") or []),
        "campaign_id": data.get("campaign_id") or None,
        "submitted_at": data.get("submitted_at"),
    }
