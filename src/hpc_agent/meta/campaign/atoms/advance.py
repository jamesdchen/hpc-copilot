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
from hpc_agent.meta.campaign.atoms._concurrency import (
    DEFAULT_MAX_IN_FLIGHT as _DEFAULT_MAX_IN_FLIGHT,
)
from hpc_agent.meta.campaign.atoms.resubmit_cap import (
    DEFAULT_MAX_TASK_RESUBMITS as _DEFAULT_MAX_TASK_RESUBMITS,
)

if TYPE_CHECKING:
    from pathlib import Path


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
                    "the within-run auto-retry cap). Defaults from the manifest "
                    "(stop_criteria.max_task_resubmits or "
                    "anomaly_policy.resubmit_cap), then the framework backstop "
                    f"({_DEFAULT_MAX_TASK_RESUBMITS}) — the loud-fail default "
                    "fires even when the manifest is silent."
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
                    "max(0, min(K - in_flight, remaining_max_jobs)). Defaults from "
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

    Async-refill mode (``async_refill`` set, #362): the steady-state
    ``wait_in_flight`` barrier is **replaced** by a ``refill`` rule ordered
    *after* the budget and stop_* halts — so a converged / over-budget /
    circuit-broken campaign stops refilling rather than topping the pool back
    up. ``refill`` carries ``refill_count = max(0, min(K - in_flight,
    remaining_max_jobs))`` (``K`` = ``max_in_flight``; ``remaining_max_jobs``
    may be ``None`` = unbounded, and already excludes in-flight jobs); when the
    pool is already full it falls back to
    ``wait_in_flight``. Crucially, ``wait_in_flight`` STILL fires for the
    terminal-stop case: when a circuit-breaker / resubmit-cap / convergence stop
    is pending but runs are still in flight, the async ladder drains them
    (``_drain_before_stop``) before emitting the stop, so a terminal stop never
    orphans an in-flight cluster job — exactly as the sync ladder's
    ``wait_in_flight`` guard does. Default off keeps the synchronous ladder
    byte-identical.

    Budget acknowledgement: ``stop_over_budget`` is a halt the loop cannot
    silently pass. When a cap is met, the campaign halts until the spend is
    explicitly acknowledged (``campaign-acknowledge-budget``). An ack
    snapshots the realised spend, so it only authorises continuing while
    spend stays at that level — the next task that burns compute re-arms the
    halt. ``needs_acknowledgement`` is set on the return when the budget halt
    is the active decision.

    Returns the underlying ``status``, ``converged``, ``budget``,
    ``circuit_breaker``, and ``resubmit_cap`` payloads so the agent can
    drill in without a second CLI call, plus ``anomaly_brief`` — a drafted
    what-tripped / evidence / recommendation block, non-``None`` only on a
    loud-fail terminator (``stop_circuit_breaker`` / ``stop_resubmit_cap``),
    for the human ``y``/nudge decision (design §5). Data only; never acted on.

    Manifest defaulting: ``circuit_breaker_failures`` and
    ``max_task_resubmits`` fall back to the manifest's ``stop_criteria``,
    then to ``anomaly_policy`` (``circuit_breaker_failures`` /
    ``resubmit_cap``), matching the budget/convergence caps. The resubmit cap
    additionally defaults to the framework backstop
    (:data:`~hpc_agent.meta.campaign.atoms.resubmit_cap.DEFAULT_MAX_TASK_RESUBMITS`)
    so the loud-fail guard fires even when the manifest is silent — the only
    loop-safety halt that is on by default (the circuit breaker stays opt-in).
    ``anomaly_policy.on_anomaly`` (``surface`` | ``park``) shapes the
    ``anomaly_brief`` recommendation without changing the decision.
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

    # Anomaly policy (design §4): the greenlit spec's anomaly-handling block.
    # ``on_anomaly`` shapes the anomaly brief's recommendation; its resubmit /
    # breaker thresholds are a fallback default source below.
    policy = _manifest_anomaly_policy(experiment_dir, campaign_id)
    on_anomaly = str(policy.get("on_anomaly", "surface")) if policy else "surface"

    if circuit_breaker_failures is None:
        circuit_breaker_failures = _manifest_circuit_breaker_failures(experiment_dir, campaign_id)
    if circuit_breaker_failures is None and policy is not None:
        circuit_breaker_failures = _as_positive_int(policy.get("circuit_breaker_failures"))

    if max_task_resubmits is None:
        max_task_resubmits = _manifest_max_task_resubmits(experiment_dir, campaign_id)
    if max_task_resubmits is None and policy is not None:
        max_task_resubmits = _as_positive_int(policy.get("resubmit_cap"))
    if max_task_resubmits is None:
        # Loud-fail default (design §5): the resubmit backstop fires even when the
        # manifest is silent — ">2× same task → stop and surface" — overridable by
        # an explicit arg / manifest value resolved above.
        max_task_resubmits = _DEFAULT_MAX_TASK_RESUBMITS
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
        trip = _breaker_trip(b)
        if trip is None:
            return None
        kind, tripped_count, tripped_run_ids = trip
        threshold = b["threshold"]
        if kind == "never_dispatched":
            # Honest attribution (F1 twin): the halt is a control-plane/dispatch
            # fault (submit-once never-dispatched safe-resubmits), NOT N failed
            # experiments. Same terminal decision, truthful rationale.
            rationale = (
                f"{tripped_count} consecutive never-dispatched submit(s) "
                f">= circuit_breaker_failures ({threshold}) — control-plane/dispatch "
                "fault, no iteration executed; check cluster connectivity/reconcile; "
                f"never-dispatched runs (newest-first): {tripped_run_ids}"
            )
        else:
            rationale = (
                f"{tripped_count} consecutive iteration failure(s) "
                f">= circuit_breaker_failures ({threshold}); "
                f"failing runs (newest-first): {tripped_run_ids}"
            )
        return CandidateAction(action="stop_circuit_breaker", rationale=rationale)

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
        # Two INDEPENDENT caps on how many to submit this tick:
        #   pool_room  = K - in_flight      — don't exceed K concurrent
        #   remaining_max_jobs              — jobs still affordable. campaign-budget's
        #     ``remaining`` is cap - spent, and ``spent`` already counts in-flight runs
        #     (they carry sidecars from submit time) — so it must NOT be reduced by
        #     in_flight again. The old ``min(K, remaining) - n`` double-counted the
        #     in-flight jobs and under-filled the pool by ``n`` on a tight budget.
        pool_room = max(0, k - n)
        refill_count = (
            pool_room if remaining_max_jobs is None else max(0, min(pool_room, remaining_max_jobs))
        )
        if refill_count > 0:
            return CandidateAction(
                action="refill",
                params={"refill_count": refill_count},
                rationale=(
                    f"async refill: {n} in flight (K={k}, "
                    f"remaining_max_jobs={remaining_max_jobs}); "
                    f"submit {refill_count} more iteration(s)"
                ),
            )
        if n > 0:
            # Pool full or out of budget room — wait for a slot rather than over-submit.
            # The async analogue of the synchronous wait_in_flight guard.
            return CandidateAction(
                action="wait_in_flight",
                rationale=(
                    f"async pool full: {n} in flight, no refill room "
                    f"(K={k}, remaining_max_jobs={remaining_max_jobs})"
                ),
            )
        return None

    def _terminal_stop_pending(e: dict[str, Any]) -> bool:
        # True when a TERMINAL stop (circuit breaker / resubmit cap / convergence)
        # would fire. Budget is excluded: it's a recoverable, ack-gated halt
        # handled by _over_budget, which — like the sync ladder — does not wait.
        # Reuses the very rules below, so the drain check can never drift from
        # what actually fires.
        return any(rule(e) is not None for rule in (_circuit_breaker, _resubmit_cap, _converged))

    def _drain_before_stop(e: dict[str, Any]) -> CandidateAction | None:
        # Async-refill (#362): honour the issue's "wait_in_flight still fires for
        # the *stop*-decision case (don't orphan jobs on a terminal stop)". When a
        # terminal stop is pending but runs are still in flight, drain them
        # (wait_in_flight) BEFORE emitting the stop — mirroring the sync ladder's
        # wait_in_flight guard, which the async ladder otherwise drops. Only once
        # the pool has drained (in_flight == 0) do the terminal stop rules below
        # fire. Ordered after _over_budget (the recoverable halt that, as in sync,
        # does not wait) and before the stop rules + refill.
        n = e["status"]["in_flight"]
        if n > 0 and _terminal_stop_pending(e):
            return CandidateAction(
                action="wait_in_flight",
                rationale=(
                    f"terminal stop pending but {n} run(s) still in flight — "
                    "draining before stop so no cluster job is orphaned"
                ),
            )
        return None

    # Async mode REPLACES the steady-state wait_in_flight with the refill rule
    # (ordered last, after the budget/stop halts, #362), but reintroduces
    # wait_in_flight via _drain_before_stop for the terminal-stop case so a stop
    # never orphans in-flight runs. Default (sync) ladder is byte-identical.
    rules = (
        [_over_budget, _drain_before_stop, _circuit_breaker, _resubmit_cap, _converged, _refill]
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

    # Anomaly brief (design §5): when a loud-fail guard terminates the block,
    # package what-tripped + evidence counts + a drafted recommendation the
    # caller can surface for a human y/nudge (or a park, per on_anomaly). Data
    # only — None on every non-anomaly decision; campaign-advance never acts.
    anomaly_brief = _anomaly_brief(outcome.chosen.action, breaker, resubmit_cap, on_anomaly)

    return {
        "campaign_id": campaign_id,
        "decision": outcome.chosen.action,
        "reason": outcome.reason,
        # Set only when the active decision is the budget halt: the loop must
        # call campaign-acknowledge-budget before it can continue (#224).
        "needs_acknowledgement": outcome.chosen.action == "stop_over_budget",
        "refill_count": refill_count,
        # Non-None only on a loud-fail terminator (stop_circuit_breaker /
        # stop_resubmit_cap); the drafted brief for the human decision point.
        "anomaly_brief": anomaly_brief,
        "status": status,
        "converged": converged,
        "budget": budget,
        "circuit_breaker": breaker,
        "resubmit_cap": resubmit_cap,
    }


def _as_positive_int(value: Any) -> int | None:
    """Coerce a manifest field to a positive int, or ``None``.

    ``bool`` is excluded explicitly (``isinstance(True, int)`` is ``True``) so a
    stray boolean never reads as a cap/threshold.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _breaker_trip(breaker: dict[str, Any]) -> tuple[str, int, list[str]] | None:
    """Which trailing streak trips the circuit breaker, if any.

    Returns ``(kind, count, run_ids)`` — *kind* is ``"iteration_failure"`` or
    ``"never_dispatched"`` — or ``None`` when neither streak meets the
    threshold. Genuine iteration failures take PRECEDENCE over never-dispatched
    when both are at threshold: a real experiment failure is the louder signal,
    and reporting it keeps the historical rationale for the common case.

    The single home for the "which streak, with what evidence" decision, so the
    breaker rule (``_circuit_breaker``) and the anomaly brief (``_anomaly_brief``)
    can never disagree about what tripped or misattribute a control-plane fault
    (never-dispatched safe-resubmits) to N failed experiments (F1 twin).
    """
    threshold = breaker.get("threshold")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        return None
    count = int(breaker.get("count", 0))
    if count >= threshold:
        return ("iteration_failure", count, list(breaker.get("run_ids", [])))
    nd_count = int(breaker.get("never_dispatched_count", 0))
    if nd_count >= threshold:
        return (
            "never_dispatched",
            nd_count,
            list(breaker.get("never_dispatched_run_ids", [])),
        )
    return None


def _anomaly_brief(
    decision: str,
    breaker: dict[str, Any],
    resubmit_cap: dict[str, Any],
    on_anomaly: str,
) -> dict[str, Any] | None:
    """Build the structured anomaly brief for a tripped loud-fail guard.

    Emitted only when *decision* is a loud-fail terminator
    (``stop_circuit_breaker`` / ``stop_resubmit_cap``) — the anomalies design §5
    treats as block terminators. Data only: it packages *what tripped*, the
    *evidence counts*, and a *drafted recommendation* for a human ``y``/nudge
    (or a park, per ``anomaly_policy.on_anomaly``). No LLM, no autonomous
    action — ``campaign-advance`` never acts on it; the caller surfaces it.

    Returns ``None`` for every non-anomaly decision.
    """
    recommended_action = "park_campaign" if on_anomaly == "park" else "surface_for_decision"
    framing = "park the campaign" if on_anomaly == "park" else "surface for a y/nudge decision"

    if decision == "stop_resubmit_cap":
        task_id = resubmit_cap["task_id"]
        count = resubmit_cap["count"]
        threshold = resubmit_cap["threshold"]
        return {
            "tripped": "resubmit_cap",
            "decision": decision,
            "on_anomaly": on_anomaly,
            "recommended_action": recommended_action,
            "evidence": {
                "count": count,
                "threshold": threshold,
                "task_id": task_id,
                "per_task": resubmit_cap["per_task"],
            },
            "recommendation": (
                f"Task {task_id!r} accrued {count} resubmit attempt(s) across the "
                f"campaign, meeting the cap ({threshold}). Recommend: {framing} — "
                "inspect this slot's failure mode before authorising any further resubmit."
            ),
        }
    if decision == "stop_circuit_breaker":
        threshold = breaker["threshold"]
        trip = _breaker_trip(breaker)
        # The caller only passes this decision when a streak tripped, so *trip*
        # is non-None; the iteration-streak fallback keeps the brief total.
        kind = trip[0] if trip is not None else "iteration_failure"
        count = trip[1] if trip is not None else int(breaker.get("count", 0))
        run_ids = trip[2] if trip is not None else list(breaker.get("run_ids", []))
        if kind == "never_dispatched":
            recommendation = (
                f"{count} consecutive never-dispatched submit(s) met the circuit "
                f"breaker ({threshold}) — a control-plane/dispatch fault, no "
                "iteration executed; never-dispatched runs (newest-first): "
                f"{run_ids}. Recommend: {framing} — check cluster "
                "connectivity/reconcile before resuming (this is NOT an "
                "experiment failure)."
            )
        else:
            recommendation = (
                f"{count} consecutive iteration failure(s) met the circuit breaker "
                f"({threshold}); failing runs (newest-first): {run_ids}. "
                f"Recommend: {framing} — diagnose the shared failure before resuming."
            )
        return {
            "tripped": "circuit_breaker",
            "breaker_kind": kind,
            "decision": decision,
            "on_anomaly": on_anomaly,
            "recommended_action": recommended_action,
            "evidence": {
                "count": count,
                "threshold": threshold,
                "run_ids": run_ids,
                "kind": kind,
                "last_status": breaker.get("last_status"),
            },
            "recommendation": recommendation,
        }
    return None


def _manifest_anomaly_policy(experiment_dir: Path, campaign_id: str) -> dict[str, Any] | None:
    """Read the top-level ``anomaly_policy`` block from the manifest.

    Mirrors :func:`_manifest_async_refill` (a top-level field, not under
    ``stop_criteria``). A missing / malformed manifest — or a non-dict
    ``anomaly_policy`` — yields ``None`` (policy defaults apply) rather than
    crashing the advance read.
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
    value = manifest.get("anomaly_policy")
    return value if isinstance(value, dict) else None


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
