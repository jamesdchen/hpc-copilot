"""``campaign-advance`` primitive — recommend the next campaign action.

Composes :func:`campaign_status`, :func:`campaign_converged`, and
:func:`campaign_budget` into a single decision: ``continue``,
``stop_converged``, ``stop_over_budget``, ``stop_circuit_breaker``, or
``wait_in_flight``. The agent reads ``decision`` to choose its next move;
``reason`` is human-readable.

Pure read. Stop criteria + budget caps come in as CLI args (defaulting
from the manifest when omitted).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import OptimizationDirection, PlateauMode
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path

# Framework default pool-occupancy target K when async_refill is on but
# --max-in-flight / the manifest leaves it unset. Matches decide-concurrency's
# k_cap default (the bound whose computation this primitive folds in).
_DEFAULT_MAX_IN_FLIGHT = 4


@primitive(
    name="campaign-advance",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Decide the next campaign action (continue / stop_converged / "
            "stop_over_budget / stop_circuit_breaker / stop_resubmit_cap / "
            "wait_in_flight / refill)."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg("--campaign-id", type=str, required=True),
            CliArg("--max-iters", type=int, default=None),
            CliArg("--metric", type=str, default=None),
            CliArg("--target", type=float, default=None),
            CliArg(
                "--direction",
                type=str,
                default=None,
                choices=get_args(OptimizationDirection),
            ),
            CliArg("--plateau-window", type=int, default=None),
            CliArg("--plateau-tolerance", type=float, default=None),
            CliArg(
                "--plateau-mode",
                type=str,
                default=None,
                choices=get_args(PlateauMode),
                help="See ``campaign-converged --help``.",
            ),
            CliArg("--max-jobs", type=int, default=None),
            CliArg("--max-tasks", type=int, default=None),
            CliArg("--max-walltime-sec", type=int, default=None),
            CliArg("--max-core-hours", type=float, default=None),
            CliArg(
                "--circuit-breaker-failures",
                type=int,
                default=None,
                help=(
                    "Loop-safety halt: stop the campaign when this many of the "
                    "most recent iterations failed consecutively (terminal "
                    "failed/abandoned runs, in submit order). No framework "
                    "default — omitted means no breaker."
                ),
            ),
            CliArg(
                "--max-task-resubmits",
                type=int,
                default=None,
                help=(
                    "Loop-safety halt: stop the campaign when any single task "
                    "slot has accrued this many resubmit attempts summed across "
                    "all the campaign's runs (the campaign-level extension of "
                    "the within-run auto-retry cap). No framework default — "
                    "omitted means no cap."
                ),
            ),
            CliArg(
                "--async-refill",
                action="store_true",
                help=(
                    "Continuous-async refill (#362): replace the wait_in_flight "
                    "barrier with a 'refill' decision that keeps --max-in-flight "
                    "iterations in flight. Defaults from the manifest's top-level "
                    "async_refill. Ordered AFTER the budget/stop halts so a "
                    "converged or circuit-broken campaign stops refilling."
                ),
            ),
            CliArg(
                "--max-in-flight",
                type=int,
                default=None,
                help=(
                    "Pool-occupancy target K for --async-refill. refill_count = "
                    "max(0, min(K, remaining_max_jobs) - in_flight). Defaults from "
                    "the manifest's top-level max_in_flight, then the framework "
                    f"default ({_DEFAULT_MAX_IN_FLIGHT}). Ignored when async is off."
                ),
            ),
        ),
        group="campaign",
    ),
)
def campaign_advance(
    *,
    experiment_dir: Path,
    campaign_id: str,
    max_iters: int | None = None,
    metric: str | None = None,
    target: float | None = None,
    direction: OptimizationDirection | None = None,
    plateau_window: int | None = None,
    plateau_tolerance: float | None = None,
    plateau_mode: PlateauMode | None = None,
    max_jobs: int | None = None,
    max_tasks: int | None = None,
    max_walltime_sec: int | None = None,
    max_core_hours: float | None = None,
    circuit_breaker_failures: int | None = None,
    max_task_resubmits: int | None = None,
    async_refill: bool = False,
    max_in_flight: int | None = None,
) -> dict[str, Any]:
    """Decide the next campaign action from history + budget.

    Decision precedence (synchronous, default):
      1. ``stop_over_budget``     — any supplied budget cap met AND the halt
                                    is not covered by a fresh acknowledgement
      2. ``wait_in_flight``       — runs are still pending (let them finish)
      3. ``stop_circuit_breaker`` — N most-recent iterations failed in a row
      4. ``stop_resubmit_cap``    — a task slot hit the campaign resubmit cap
      5. ``stop_converged``       — any supplied stop criterion fired
      6. ``continue``             — agent should plan the next iteration

    The loop-safety halts (circuit breaker, resubmit cap) sit *after*
    ``wait_in_flight`` so an in-flight retry (which carries no terminal
    verdict yet) is given the chance to succeed before they fire.

    Async-refill mode (``async_refill`` set, #362): the ``wait_in_flight``
    barrier is **replaced** by a ``refill`` rule ordered *after* the budget
    and stop_* halts — so a converged / over-budget / circuit-broken campaign
    stops refilling rather than topping the pool back up. ``refill`` carries
    ``refill_count = max(0, min(K, remaining_max_jobs) - in_flight)`` (``K`` =
    ``max_in_flight``; ``remaining_max_jobs`` may be ``None`` = unbounded);
    when the pool is already full it falls back to ``wait_in_flight``. Default
    off keeps the synchronous ladder byte-identical.

    Budget acknowledgement: ``stop_over_budget`` is a halt the loop cannot
    silently pass. When a cap is met, the campaign halts until the spend is
    explicitly acknowledged (``campaign-acknowledge-budget``). An ack
    snapshots the realised spend, so it only authorises continuing while
    spend stays at that level — the next task that burns compute re-arms the
    halt. ``needs_acknowledgement`` is set on the return when the budget halt
    is the active decision.

    Returns the underlying ``status``, ``converged``, ``budget``,
    ``circuit_breaker``, and ``resubmit_cap`` payloads so the agent can
    drill in without a second CLI call.

    Manifest defaulting: ``circuit_breaker_failures`` and
    ``max_task_resubmits`` fall back to the manifest's ``stop_criteria``
    when omitted, matching the budget/convergence caps.
    """
    from hpc_agent._kernel.decision import decide
    from hpc_agent._wire.fixtures.escalation import CandidateAction
    from hpc_agent.meta.campaign.atoms.budget import campaign_budget
    from hpc_agent.meta.campaign.atoms.circuit_breaker import consecutive_terminal_failures
    from hpc_agent.meta.campaign.atoms.converged import campaign_converged
    from hpc_agent.meta.campaign.atoms.resubmit_cap import max_task_resubmits as _max_task_resubmits
    from hpc_agent.meta.campaign.atoms.status import campaign_status
    from hpc_agent.meta.campaign.budget_ack import ack_covers_spend, read_budget_ack
    from hpc_agent.state.index import find_runs_by_campaign

    if circuit_breaker_failures is None:
        circuit_breaker_failures = _manifest_circuit_breaker_failures(experiment_dir, campaign_id)
    if max_task_resubmits is None:
        max_task_resubmits = _manifest_max_task_resubmits(experiment_dir, campaign_id)
    # Async-refill opt-in. ``--async-refill`` is a store_true (default False,
    # not None), so we can't use the None-sentinel default pattern: instead the
    # CLI flag force-enables, and an absent flag falls back to the manifest.
    if not async_refill:
        async_refill = bool(_manifest_async_refill(experiment_dir, campaign_id))
    if max_in_flight is None:
        max_in_flight = _manifest_max_in_flight(experiment_dir, campaign_id)

    status = campaign_status(experiment_dir=experiment_dir, campaign_id=campaign_id)
    budget = campaign_budget(
        experiment_dir=experiment_dir,
        campaign_id=campaign_id,
        max_jobs=max_jobs,
        max_tasks=max_tasks,
        max_walltime_sec=max_walltime_sec,
        max_core_hours=max_core_hours,
    )
    converged = campaign_converged(
        experiment_dir=experiment_dir,
        campaign_id=campaign_id,
        max_iters=max_iters,
        metric=metric,
        target=target,
        direction=direction,
        plateau_window=plateau_window,
        plateau_tolerance=plateau_tolerance,
        plateau_mode=plateau_mode,
    )
    runs = find_runs_by_campaign(experiment_dir, campaign_id)
    breaker = consecutive_terminal_failures(runs)
    breaker = {**breaker, "threshold": circuit_breaker_failures}
    resubmit_cap = _max_task_resubmits(runs)
    resubmit_cap = {**resubmit_cap, "threshold": max_task_resubmits}
    budget_ack = read_budget_ack(experiment_dir, campaign_id)

    # The decision precedence is a total deterministic ladder over the
    # computed payloads — express it as ordered kernel rules so the campaign
    # 'decide' point routes through the same evaluator as every other decision
    # point. The 'continue' catch-all is the default branch (this point never
    # escalates: every input lands on one of the outcomes).
    evidence = {
        "status": status,
        "budget": budget,
        "converged": converged,
        "breaker": breaker,
        "resubmit_cap": resubmit_cap,
        "budget_ack": budget_ack,
    }

    def _over_budget(e: dict[str, Any]) -> CandidateAction | None:
        b = e["budget"]
        if not b["exhausted"]:
            return None
        ack = e["budget_ack"]
        # A fresh acknowledgement (snapshotting spend at or above the current
        # realised level) authorises continuing this leg — do not halt. Spend
        # is monotonic, so the next task that burns compute makes the ack stale
        # and re-arms this halt, forcing a new explicit acknowledgement.
        if ack is not None and ack_covers_spend(ack, b["spent"]):
            return None
        rationale = b["reason"]
        if ack is not None:
            rationale += " (prior acknowledgement is stale — spend grew past it)"
        return CandidateAction(action="stop_over_budget", rationale=rationale)

    def _wait_in_flight(e: dict[str, Any]) -> CandidateAction | None:
        # In-flight runs must finish before a stop decision so we don't orphan
        # cluster jobs the campaign can't keep tracking.
        n = e["status"]["in_flight"]
        if n > 0:
            return CandidateAction(action="wait_in_flight", rationale=f"{n} run(s) still in flight")
        return None

    def _circuit_breaker(e: dict[str, Any]) -> CandidateAction | None:
        b = e["breaker"]
        threshold = b["threshold"]
        if threshold is not None and threshold > 0 and b["count"] >= threshold:
            return CandidateAction(
                action="stop_circuit_breaker",
                rationale=(
                    f"{b['count']} consecutive iteration failure(s) "
                    f">= circuit_breaker_failures ({threshold}); "
                    f"failing runs (newest-first): {b['run_ids']}"
                ),
            )
        return None

    def _resubmit_cap(e: dict[str, Any]) -> CandidateAction | None:
        r = e["resubmit_cap"]
        threshold = r["threshold"]
        if threshold is not None and threshold > 0 and r["count"] >= threshold:
            return CandidateAction(
                action="stop_resubmit_cap",
                rationale=(
                    f"task {r['task_id']!r} accrued {r['count']} resubmit "
                    f"attempt(s) >= max_task_resubmits ({threshold}) across the "
                    f"campaign; per-task totals: {r['per_task']}"
                ),
            )
        return None

    def _converged(e: dict[str, Any]) -> CandidateAction | None:
        if e["converged"]["converged"]:
            return CandidateAction(action="stop_converged", rationale=e["converged"]["reason"])
        return None

    def _refill(e: dict[str, Any]) -> CandidateAction | None:
        # Async-refill only (#362): keep up to K iterations in flight, capped by
        # budget headroom. This folds decide-concurrency's safe-bound computation
        # (currently unused by the driver) into the ladder. Reached only AFTER
        # over_budget + the stop_* halts, so a converged / circuit-broken /
        # over-budget campaign stops refilling instead of topping the pool back up.
        n = e["status"]["in_flight"]
        remaining_max_jobs = e["budget"]["remaining"].get("max_jobs")  # None = unbounded
        k = max_in_flight if max_in_flight is not None else _DEFAULT_MAX_IN_FLIGHT
        target = k if remaining_max_jobs is None else min(k, remaining_max_jobs)
        refill_count = max(0, target - n)
        if refill_count > 0:
            return CandidateAction(
                action="refill",
                params={"refill_count": refill_count},
                rationale=(
                    f"async refill: {n} in flight < target {target} "
                    f"(K={k}, remaining_max_jobs={remaining_max_jobs}); "
                    f"submit {refill_count} more iteration(s)"
                ),
            )
        if n > 0:
            # Pool already at/over target — wait for a slot rather than over-submit.
            # The async analogue of the synchronous wait_in_flight guard.
            return CandidateAction(
                action="wait_in_flight",
                rationale=f"async pool full: {n} in flight >= target {target}",
            )
        return None

    # Async mode REPLACES wait_in_flight with the refill rule, and orders it
    # after the budget/stop halts (decision #362). Default (sync) ladder is
    # byte-identical to before.
    rules = (
        [_over_budget, _circuit_breaker, _resubmit_cap, _converged, _refill]
        if async_refill
        else [_over_budget, _wait_in_flight, _circuit_breaker, _resubmit_cap, _converged]
    )
    outcome = decide(
        "decide",
        evidence,
        rules=rules,
        default=CandidateAction(
            action="continue",
            rationale=(
                f"{status['iterations']} iteration(s) complete, no stop criterion met, "
                "no in-flight runs"
            ),
        ),
    )
    assert outcome.chosen is not None  # a total ladder always resolves to a branch

    # refill_count rides on CandidateAction.params; surface it on the return so
    # the deterministic resolver's refill arm knows how many iterations to submit.
    refill_count: int | None = None
    if outcome.chosen.action == "refill":
        refill_count = (outcome.chosen.params or {}).get("refill_count")

    return {
        "campaign_id": campaign_id,
        "decision": outcome.chosen.action,
        "reason": outcome.reason,
        # Set only when the active decision is the budget halt: the loop must
        # call campaign-acknowledge-budget before it can continue (#224).
        "needs_acknowledgement": outcome.chosen.action == "stop_over_budget",
        "refill_count": refill_count,
        "status": status,
        "converged": converged,
        "budget": budget,
        "circuit_breaker": breaker,
        "resubmit_cap": resubmit_cap,
    }


def _manifest_circuit_breaker_failures(experiment_dir: Path, campaign_id: str) -> int | None:
    """Read ``stop_criteria.circuit_breaker_failures`` from the manifest.

    Mirrors how :func:`campaign_budget` / :func:`campaign_converged`
    default their caps from the manifest. A missing / malformed manifest
    yields ``None`` (no breaker) rather than crashing the advance read.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        return None
    if manifest is None:
        return None
    stop_criteria = manifest.get("stop_criteria") or {}
    value = stop_criteria.get("circuit_breaker_failures")
    return value if isinstance(value, int) else None


def _manifest_max_task_resubmits(experiment_dir: Path, campaign_id: str) -> int | None:
    """Read ``stop_criteria.max_task_resubmits`` from the manifest.

    Mirrors :func:`_manifest_circuit_breaker_failures` — a missing or
    malformed manifest yields ``None`` (no cap) rather than crashing the
    advance read.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        return None
    if manifest is None:
        return None
    stop_criteria = manifest.get("stop_criteria") or {}
    value = stop_criteria.get("max_task_resubmits")
    return value if isinstance(value, int) else None


def _manifest_async_refill(experiment_dir: Path, campaign_id: str) -> bool | None:
    """Read the **top-level** ``async_refill`` flag from the manifest.

    Unlike the circuit-breaker / resubmit-cap helpers (which read under
    ``stop_criteria``), the async-refill opt-in is a top-level manifest
    field — do NOT copy the ``stop_criteria.get(...)`` key path. A missing /
    malformed manifest yields ``None`` (treated as off) rather than crashing
    the advance read.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        return None
    if manifest is None:
        return None
    value = manifest.get("async_refill")
    return value if isinstance(value, bool) else None


def _manifest_max_in_flight(experiment_dir: Path, campaign_id: str) -> int | None:
    """Read the **top-level** ``max_in_flight`` (pool target K) from the manifest.

    Top-level field (see :func:`_manifest_async_refill`) — not under
    ``stop_criteria``. ``bool`` is excluded explicitly (``isinstance(True, int)``
    is ``True``) so a stray boolean never reads as a pool size. A missing /
    malformed manifest yields ``None``.
    """
    import json

    import jsonschema

    from hpc_agent.meta.campaign.manifest import read_manifest

    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        return None
    if manifest is None:
        return None
    value = manifest.get("max_in_flight")
    return value if isinstance(value, int) and not isinstance(value, bool) else None
