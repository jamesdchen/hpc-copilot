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

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
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
    cli=CliShape(
        help=(
            "Run the /submit-hpc Setup priority cascade and recommend "
            "{action: monitor|reuse|interview|fresh, run_id, candidates}. "
            "Replaces the priority-list-walking prose at Step 0."
        ),
        experiment_dir_arg=True,
    ),
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

    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction
    from hpc_agent.state.index import find_in_flight_runs
    from hpc_agent.state.runs import find_existing_runs

    # The Setup cascade is a total deterministic priority ladder — express each
    # tier as an ordered kernel rule so the submit 'entry_path' point routes
    # through the same evaluator as every other decision point. Each branch
    # carries its full payload (priority + candidates + recommended run) in the
    # candidate's ``params``; the envelope is assembled from the chosen branch.
    # 'fresh' is the default catch-all (this point never escalates). The rules
    # short-circuit exactly like the prior if-ladder: a later tier's lookup runs
    # only when every earlier tier abstained.
    def _monitor(d: Path) -> CandidateAction | None:
        try:
            in_flight = find_in_flight_runs(d)
        except (OSError, ValueError):
            in_flight = []
        if not in_flight:
            return None
        candidates = [_summarize_record(r) for r in in_flight]
        return CandidateAction(
            action="monitor",
            params={
                "priority": 0,
                "recommended_run_id": candidates[0]["run_id"],
                "candidates": candidates,
            },
            rationale=(
                f"{len(in_flight)} in-flight run(s) on the journal — "
                "switch to /monitor-hpc rather than re-submitting."
            ),
        )

    def _reuse(d: Path) -> CandidateAction | None:
        sidecars = find_existing_runs(d)
        if not sidecars:
            return None
        candidates = [_summarize_sidecar(p) for p in sidecars[:10]]
        return CandidateAction(
            action="reuse",
            params={
                "priority": 1,
                "recommended_run_id": candidates[0]["run_id"],
                "candidates": candidates,
            },
            rationale=(
                f"{len(sidecars)} previous run(s) on disk — offer the user "
                'one of the recent (profile, cluster) pairs as "same as last".'
            ),
        )

    def _interview(d: Path) -> CandidateAction | None:
        if not (d / ".hpc" / "tasks.py").is_file():
            return None
        return CandidateAction(
            action="interview",
            params={"priority": 2, "recommended_run_id": None, "candidates": []},
            rationale=(
                ".hpc/tasks.py exists but no run history — skip executor "
                "discovery and the axes interview; jump to the planner."
            ),
        )

    decision = decide(
        "entry_path",
        experiment_dir,
        rules=[_monitor, _reuse, _interview],
        default=CandidateAction(
            action="fresh",
            params={"priority": 3, "recommended_run_id": None, "candidates": []},
            rationale="no in-flight runs, no sidecars, no tasks.py — full interview.",
        ),
    )
    chosen = decision.chosen
    assert chosen is not None  # a total ladder always resolves to a branch
    return {
        "priority": chosen.params["priority"],
        "action": chosen.action,
        "recommended_run_id": chosen.params["recommended_run_id"],
        "candidates": chosen.params["candidates"],
        "reason": decision.reason,
    }


@primitive(
    name="find-prior-run",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="cmd_sha",
    cli=CliShape(
        help=(
            "Look up a prior run by cmd_sha for /submit-hpc Step 6c "
            "resume detection. Returns {found, run_id, is_orphan, "
            "status, age_sec, ...}."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--cmd-sha",
                type=str,
                required=True,
                help="The cmd_sha (SHA-256 hex) to match against existing sidecars.",
            ),
        ),
    ),
    agent_facing=True,
)
def find_prior_run(
    experiment_dir: Path,
    *,
    cmd_sha: str,
) -> dict[str, Any]:
    """Look up a prior run by ``cmd_sha`` for resume detection.

    Wraps :func:`hpc_agent.state.runs.find_run_by_cmd_sha` + a sidecar
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

    from hpc_agent.state.runs import find_run_by_cmd_sha, is_orphan_sidecar

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
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}

    age_sec: int | None = None
    try:
        # Clamp at 0: a sidecar mtime in the future (clock skew, or a file
        # restored with a future timestamp) would otherwise yield a negative
        # age that violates the output schema's `age_sec >= 0` constraint.
        age_sec = max(0, int(time.time() - path.stat().st_mtime))
    except OSError:
        age_sec = None

    # The output schema types job_ids as list[str]; a legacy/hand-written
    # sidecar may carry non-string elements, so coerce to str.
    job_ids = [str(j) for j in (data.get("job_ids") or [])]

    return {
        "found": True,
        "prior_run_id": run_id,
        "is_orphan": is_orphan_sidecar(experiment_dir, run_id),
        "status": data.get("status"),
        "age_sec": age_sec,
        "profile": data.get("profile"),
        "cluster": data.get("cluster"),
        "job_ids": job_ids,
        "campaign_id": data.get("campaign_id") or None,
        "submitted_at": data.get("submitted_at"),
    }
