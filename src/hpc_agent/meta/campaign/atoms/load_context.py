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

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliShape
from hpc_agent.state.index import find_in_flight_runs

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


def _is_onboarded(experiment_dir: Path) -> bool:
    """True when the repo carries the dispatch contract a submit needs.

    ``.hpc/tasks.py`` is the artifact ``submit-flow`` and the cluster-side
    dispatcher require; its absence means the repo has never been
    onboarded (``wrap-entry-point`` has not run). Mirrors the signal
    ``hpc-agent setup``'s recommender uses to distinguish ``interview`` /
    ``fresh`` from a ready-to-submit repo.
    """
    return (experiment_dir / ".hpc" / "tasks.py").is_file()


# Framework default pool-occupancy target K for an async-refill campaign whose
# manifest omits ``max_in_flight``. Kept in sync with
# ``advance._DEFAULT_MAX_IN_FLIGHT`` and ``decide_concurrency``'s k_cap default.
_DEFAULT_MAX_IN_FLIGHT = 4


def _campaign_async_config(experiment_dir: Path, campaign_id: str) -> tuple[bool, int | None]:
    """Return ``(async_refill, max_in_flight)`` from a campaign's manifest.

    A missing / malformed manifest yields ``(False, None)`` — async refill is
    an explicit opt-in, so the absence of a readable manifest means the
    synchronous barrier (treat as off). Top-level manifest fields (#362), not
    under ``stop_criteria``.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        return False, None
    if not manifest:
        return False, None
    async_on = manifest.get("async_refill") is True
    raw_k = manifest.get("max_in_flight")
    k = raw_k if isinstance(raw_k, int) and not isinstance(raw_k, bool) else None
    return async_on, k


def _async_free_slot(
    experiment_dir: Path,
    in_flight: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
) -> bool:
    """True when an async-refill campaign has a free per-campaign slot.

    Counts in-flight runs **per campaign** (the rows carry ``campaign_id``) and
    compares each async campaign's count to its ``max_in_flight`` (K). A single
    campaign with ``in_flight < K`` is enough to route a refill step. Sync
    campaigns are skipped, so a repo with no async opt-in always returns
    ``False`` and the synchronous routing below is unchanged.
    """
    if not campaigns:
        return False
    counts: dict[str, int] = {}
    for row in in_flight:
        cid = row.get("campaign_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    for camp in campaigns:
        cid = camp["campaign_id"]
        async_on, k = _campaign_async_config(experiment_dir, cid)
        if not async_on:
            continue
        if counts.get(cid, 0) < (k or _DEFAULT_MAX_IN_FLIGHT):
            return True
    return False


def _next_step_hint(
    experiment_dir: Path,
    in_flight: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    *,
    onboarded: bool,
) -> str:
    """Coarse next-action hint from the in-flight set and known campaigns.

    - async-refill campaign with a free slot (``in_flight < K``)  -> ``decide``
      (refill — even while OTHER runs are in flight)
    - any in-flight run still in the ``monitor`` stage  -> ``monitor``
    - in-flight runs exist but all past monitoring      -> ``aggregate``
    - nothing in flight, a campaign exists              -> ``decide``
    - nothing in flight, no campaign, repo onboarded    -> ``submit``
    - nothing in flight, no campaign, NOT onboarded     -> ``onboard``

    ``decide`` distinguishes "a campaign finished an iteration and needs
    its next one chosen" from a cold ``submit`` of a fresh experiment. In an
    async-refill campaign (#362) the ``decide`` (refill) step is **no longer
    gated on ``in_flight == 0``**: whenever a campaign has an empty slot it
    refills, and only when every async campaign's pool is full does the
    synchronous monitor/aggregate routing take over to drain the in-flight
    runs. ``onboard`` catches the un-onboarded repo (no ``.hpc/tasks.py``).

    Advisory only: the skill still decides, but a fresh step gets a
    deterministic starting point instead of guessing from memory.
    """
    # Async-refill: a free per-campaign slot routes a refill (decide) step
    # even while runs are in flight. Checked first so refilling keeps the pool
    # full; when no async campaign has a free slot this is False and the
    # synchronous routing below is byte-identical.
    if _async_free_slot(experiment_dir, in_flight, campaigns):
        return "decide"
    if not in_flight:
        if campaigns:
            return "decide"
        return "submit" if onboarded else "onboard"
    if any(r.get("stage") == "monitor" for r in in_flight):
        return "monitor"
    return "aggregate"


def _decide_campaign_id(
    campaigns: list[dict[str, Any]], latest_run: dict[str, Any] | None
) -> str | None:
    """Pick the campaign whose next iteration a ``decide`` step advances.

    Prefer the campaign of the most recent run — that is the iteration
    that just finished — and fall back to the first known campaign.
    """
    if latest_run is not None:
        cid = latest_run.get("campaign_id")
        if isinstance(cid, str) and cid:
            return cid
    if campaigns:
        first: str = campaigns[0]["campaign_id"]
        return first
    return None


def _build_delegate(
    experiment_dir: Path,
    hint: str,
    in_flight: list[dict[str, Any]],
    campaigns: list[dict[str, Any]],
    latest_run: dict[str, Any] | None,
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
    from hpc_agent._kernel.extension.spawn_prompt import render_spawn_prompt

    exp = str(experiment_dir)
    if hint == "onboard":
        return {
            "kind": "agent",
            "step": "onboard",
            "run_id": None,
            "campaign_id": None,
            "experiment_dir": exp,
            "reason": (
                "repo is not onboarded (no .hpc/tasks.py); run "
                "wrap-entry-point to build the dispatch/resource contract "
                "before any submission"
            ),
            # No SpawnRequest: onboarding is the ``wrap-entry-point`` /
            # ``/wrap-entry-point-hpc`` interview, not one of the
            # submit/status/aggregate/campaign workflows the spawn
            # contract enumerates.
            "spawn_request": None,
            "prompt": (
                f"This repo at {exp} is not onboarded — there is no "
                ".hpc/tasks.py, so there is nothing to submit yet. Run "
                "wrap-entry-point (slash command /wrap-entry-point-hpc) to "
                "interview the entry point and generate the dispatch "
                "contract (tasks.py + EXECUTOR/result_dir_template/run_id). "
                "Do NOT hand-write tasks.py or reverse-engineer the "
                "contract; onboard first, then submit."
            ),
        }
    if hint == "submit":
        return {
            "kind": "agent",
            "step": "submit",
            "run_id": None,
            "campaign_id": None,
            "experiment_dir": exp,
            "reason": "no runs in flight; the next step is a new submission",
            "spawn_request": {
                "workflow": "submit",
                "experiment_dir": exp,
                "fields": {},
            },
            "prompt": render_spawn_prompt(workflow="submit", experiment_dir=exp, fields={}),
        }
    if hint == "decide":
        campaign_id = _decide_campaign_id(campaigns, latest_run)
        decide_fields: dict[str, Any] = {
            "campaign_id": campaign_id,
            "step": "decide",
        }
        return {
            "kind": "agent",
            "step": "decide",
            "run_id": None,
            "campaign_id": campaign_id,
            "experiment_dir": exp,
            "reason": (
                f"campaign {campaign_id!r} is ready to decide/refill its next "
                "iteration(s) (a free slot in async-refill mode, or an idle "
                "campaign in synchronous mode)"
            ),
            # step stays "decide" even for an async refill: the deterministic
            # resolver dispatches its decide chain on fields.step == "decide",
            # then campaign-advance returns decision="refill" and the resolver's
            # refill arm submits refill_count iterations (#362, plan §1.4).
            "spawn_request": {
                "workflow": "campaign",
                "experiment_dir": exp,
                "fields": decide_fields,
            },
            "prompt": render_spawn_prompt(
                workflow="campaign", experiment_dir=exp, fields=decide_fields
            ),
        }
    # monitor / aggregate — pick the in-flight run that governs the step.
    governing: dict[str, Any] | None = None
    for row in in_flight:
        in_monitor = row.get("stage") == "monitor"
        if (hint == "monitor" and in_monitor) or (hint == "aggregate" and not in_monitor):
            governing = row
            break
    if governing is None and in_flight:
        governing = in_flight[0]
    run_id = governing.get("run_id") if governing else None
    campaign_id = governing.get("campaign_id") if governing else None
    verb = "monitor-flow" if hint == "monitor" else "aggregate-flow"
    return {
        "kind": "cli",
        "step": hint,
        "run_id": run_id,
        "campaign_id": campaign_id,
        "experiment_dir": exp,
        "reason": f"run {run_id} is in flight; the next step is {hint}",
        "prompt": (
            f"Drive {verb} for run {run_id} in {exp}. This is a deterministic "
            "CLI step — no judgement required; a headless driver runs it "
            "directly without spawning an LLM."
        ),
        "spawn_request": None,
    }


@primitive(
    name="load-context",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Reconstruct workflow context (latest run + config snapshot, "
            "in-flight runs, campaigns) from on-disk state. Run this first "
            "in any fresh-context step instead of relying on memory."
        ),
        experiment_dir_arg=True,
    ),
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
    - ``needs_onboarding`` — ``True`` when the repo has no
      ``.hpc/tasks.py`` (``wrap-entry-point`` has not run); callers
      should route to onboarding before attempting a submit.
    - ``next_step_hint`` — ``submit`` / ``monitor`` / ``aggregate`` /
      ``decide`` / ``onboard``, derived from the in-flight set, known
      campaigns, and onboarding state (``decide`` when a campaign is idle
      and awaiting its next iteration, OR — for an async-refill campaign
      (#362) — whenever it has a free slot, even with runs still in flight;
      ``onboard`` when the repo has no ``.hpc/tasks.py``).
    - ``delegate`` — the next step as a delegable unit of work
      (``kind`` ``cli``/``agent``, ``step``, ``run_id``,
      ``campaign_id``, ``prompt``, ``spawn_request``); consumed by an
      in-session orchestrator or the headless campaign driver.
    - ``warnings`` — non-fatal notes (orphan sidecar, unreadable
      cursor).

    A step should treat this as its only source of truth and never fall
    back to conversational memory for run_id / campaign / cluster.
    """
    from pathlib import Path as _Path

    from hpc_agent import errors
    from hpc_agent.infra.time import status_age_seconds as _last_status_age_seconds
    from hpc_agent.meta.campaign.atoms.list_campaigns import campaign_list
    from hpc_agent.meta.campaign.cursor import read_cursor
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
        except (FileNotFoundError, OSError, ValueError, errors.HpcError):
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
    for record in find_in_flight_runs(experiment_dir):
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

    onboarded = _is_onboarded(experiment_dir)
    hint = _next_step_hint(experiment_dir, in_flight, campaigns, onboarded=onboarded)
    return {
        "experiment_dir": str(experiment_dir),
        "latest_run": latest_run,
        "in_flight": in_flight,
        "campaigns": campaigns,
        "needs_onboarding": not onboarded,
        "next_step_hint": hint,
        "delegate": _build_delegate(experiment_dir, hint, in_flight, campaigns, latest_run),
        "warnings": warnings,
    }
