"""``load-context`` primitive — reconstruct workflow context from disk.

A fresh-context step — a subagent, a restarted session, a cron tick —
has no conversational memory: it does not know the active ``run_id``,
campaign, cluster, or config the previous step established. Skills that
rely on the agent "remembering" those values break the moment context
is compacted or a session restarts.

``load-context`` rebuilds that picture from the on-disk state alone —
run sidecars, the journal, and campaign cursors — so every skill can
open with one CLI call instead of trusting context that may be gone.

Pure read: no SSH, no scheduler, no writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent._internal import session
from hpc_agent._internal.primitive import primitive

if TYPE_CHECKING:
    from pathlib import Path

# Sidecar v2 config-snapshot keys surfaced verbatim under ``latest_run``.
# These are exactly the values skills currently cache conversationally
# (see hpc-submit/SKILL.md "Cache to Claude Code memory: ...").
_CONFIG_KEYS: tuple[str, ...] = (
    "cluster",
    "profile",
    "campaign_id",
    "project",
    "remote_path",
    "resources",
    "env",
    "env_group",
    "constraints",
    "runtime",
)


def _next_step_hint(in_flight: list[dict[str, Any]]) -> str:
    """Coarse next-action hint derived only from the in-flight set.

    - any in-flight run still in the ``monitor`` stage  -> ``monitor``
    - in-flight runs exist but all past monitoring      -> ``aggregate``
    - nothing in flight                                 -> ``submit``

    Advisory only: the skill still decides, but a fresh step gets a
    deterministic starting point instead of guessing from memory.
    """
    if not in_flight:
        return "submit"
    if any(r.get("stage") == "monitor" for r in in_flight):
        return "monitor"
    return "aggregate"


def _build_delegate(
    experiment_dir: Path,
    hint: str,
    in_flight: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe the next workflow step as a delegable unit of work.

    The ``delegate`` block is the single contract two consumers share:

    - an in-session orchestrator reads it and either runs the step
      itself (``kind == "cli"``) or spawns a fresh-context subagent
      with ``prompt`` (``kind == "agent"``);
    - the headless campaign driver reads the same block and either
      runs the ``hpc-agent`` verb directly (``cli``) or shells
      ``claude -p`` (``agent``).

    ``kind`` is the cost/determinism split: ``cli`` steps are
    deterministic and need no LLM; ``agent`` steps need judgement.
    """
    exp = str(experiment_dir)
    if hint == "submit":
        return {
            "kind": "agent",
            "step": "submit",
            "run_id": None,
            "experiment_dir": exp,
            "reason": "no runs in flight; the next step is a new submission",
            "prompt": (
                f"Submit an HPC experiment in {exp}. First run `hpc-agent "
                f"load-context --experiment-dir {exp}` and use its data as the "
                "source of truth, then drive the hpc-submit skill. A submission "
                "needs user intent, so this is an agent step."
            ),
        }
    # monitor / aggregate — pick the in-flight run that governs the step.
    run_id: str | None = None
    for row in in_flight:
        in_monitor = row.get("stage") == "monitor"
        if (hint == "monitor" and in_monitor) or (hint == "aggregate" and not in_monitor):
            run_id = row.get("run_id")
            break
    if run_id is None and in_flight:
        run_id = in_flight[0].get("run_id")
    verb = "monitor-flow" if hint == "monitor" else "aggregate-flow"
    return {
        "kind": "cli",
        "step": hint,
        "run_id": run_id,
        "experiment_dir": exp,
        "reason": f"run {run_id} is in flight; the next step is {hint}",
        "prompt": (
            f"Drive {verb} for run {run_id} in {exp}. This is a deterministic "
            "CLI step — no judgement required; a headless driver runs it "
            "directly without spawning an LLM."
        ),
    }


@primitive(
    name="load-context",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent load-context --experiment-dir <path>",
    agent_facing=True,
)
def load_context(*, experiment_dir: Path) -> dict[str, Any]:
    """Return the on-disk workflow context for *experiment_dir*.

    The envelope's ``data`` carries:

    - ``latest_run`` — the newest run sidecar projected to its identity
      plus the v2 config snapshot (cluster/resources/env/...), or
      ``None`` when no run exists.
    - ``in_flight`` — journal records still in flight, one row each.
    - ``campaigns`` — every campaign with a sidecar, plus its cursor
      iteration when a cursor file exists.
    - ``next_step_hint`` — ``submit`` / ``monitor`` / ``aggregate``,
      derived purely from the in-flight set.
    - ``delegate`` — the next step as a delegable unit of work
      (``kind`` ``cli``/``agent``, ``step``, ``run_id``, ``prompt``);
      consumed by an in-session orchestrator or the headless campaign
      driver.
    - ``warnings`` — non-fatal notes (orphan sidecar, unreadable
      cursor).

    A step should treat this as its only source of truth and never fall
    back to conversational memory for run_id / campaign / cluster.
    """
    from pathlib import Path as _Path

    from hpc_agent.atoms.campaign_list import campaign_list
    from hpc_agent.atoms.list_in_flight import _last_status_age_seconds
    from hpc_agent.campaign.cursor import read_cursor
    from hpc_agent.state.runs import (
        find_existing_runs,
        is_orphan_sidecar,
        read_run_sidecar,
    )

    # Resolve to an absolute path: the delegate block embeds it into
    # prompts a fresh-context consumer (a subagent, the headless driver)
    # reads from a different cwd, so a relative path would not resolve.
    experiment_dir = _Path(experiment_dir).resolve()

    warnings: list[str] = []

    # --- latest run sidecar: the config snapshot a fresh step needs ---
    latest_run: dict[str, Any] | None = None
    runs = find_existing_runs(experiment_dir)
    if runs:
        run_id = runs[0].stem
        try:
            sidecar = read_run_sidecar(experiment_dir, run_id)
        except (FileNotFoundError, OSError, ValueError):
            sidecar = None
        if sidecar is not None:
            latest_run = {
                "run_id": run_id,
                "cmd_sha": sidecar.get("cmd_sha"),
                "submitted_at": sidecar.get("submitted_at"),
                "task_count": sidecar.get("task_count"),
                "result_dir_template": sidecar.get("result_dir_template"),
                "job_ids": sidecar.get("job_ids"),
            }
            for key in _CONFIG_KEYS:
                latest_run[key] = sidecar.get(key)
            orphan = is_orphan_sidecar(experiment_dir, run_id)
            latest_run["is_orphan"] = orphan
            if orphan:
                warnings.append(
                    f"latest run {run_id} is an orphan sidecar (no job ids) "
                    "— it never reached the scheduler; resubmit or prune it"
                )

    # --- in-flight journal records ---
    in_flight: list[dict[str, Any]] = []
    for record in session.find_in_flight_runs(experiment_dir):
        in_flight.append(
            {
                "run_id": record.run_id,
                "campaign_id": record.campaign_id or None,
                "cluster": record.cluster,
                "ssh_target": record.ssh_target,
                "remote_path": record.remote_path,
                "job_ids": record.job_ids,
                "total_tasks": record.total_tasks,
                "stage": record.stage,
                "status": record.status,
                "last_status_age_seconds": _last_status_age_seconds(record.last_status),
            }
        )

    # --- campaigns + cursors ---
    campaigns: list[dict[str, Any]] = []
    for entry in campaign_list(experiment_dir=experiment_dir)["campaigns"]:
        campaign_id = entry["campaign_id"]
        row: dict[str, Any] = {
            "campaign_id": campaign_id,
            "iterations_submitted": entry["iterations"],
        }
        # Only read a cursor when its campaign dir already exists —
        # read_cursor() would otherwise create it, and load-context
        # declares no side effects.
        camp_root = _Path(experiment_dir) / ".hpc" / "campaigns" / campaign_id
        if camp_root.is_dir():
            try:
                cursor = read_cursor(experiment_dir, campaign_id)
            except ValueError as exc:
                cursor = None
                warnings.append(f"campaign {campaign_id} cursor unreadable: {exc}")
            if cursor is not None:
                row["cursor_iteration"] = cursor.get("iteration")
                row["cursor_last_run_id"] = cursor.get("last_run_id") or None
        campaigns.append(row)

    hint = _next_step_hint(in_flight)
    return {
        "experiment_dir": str(experiment_dir),
        "latest_run": latest_run,
        "in_flight": in_flight,
        "campaigns": campaigns,
        "next_step_hint": hint,
        "delegate": _build_delegate(experiment_dir, hint, in_flight),
        "warnings": warnings,
    }
