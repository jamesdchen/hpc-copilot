"""``campaign-converged`` primitive — apply user-supplied stop criteria.

Pure compute over the campaign history. Three independent triggers,
ANY of which fires returns ``converged=true``:

* ``max_iters``        — iterations completed >= N
* ``target``           — best observed metric crosses a threshold
* ``plateau_window``   — best metric hasn't improved by > tolerance in last N iters

If no triggers are supplied, the primitive returns ``converged=false``
with reason ``"no_criteria"``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, get_args

import jsonschema

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire._shared import OptimizationDirection, PlateauMode
from hpc_agent.cli._dispatch import CliArg, CliShape

if TYPE_CHECKING:
    from pathlib import Path


def _best(values: list[float], direction: str) -> float | None:
    if not values:
        return None
    return min(values) if direction == "minimize" else max(values)


def _extract_metric(history: list[dict[str, Any]], metric: str) -> list[float]:
    """Pull *metric* from each iteration's reduced dict; skip empty/missing."""
    out: list[float] = []
    for entry in history:
        if not entry:
            continue
        value = entry.get(metric)
        if isinstance(value, (int, float)):
            out.append(float(value))
    return out


@primitive(
    name="campaign-converged",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help="Apply user-supplied stop criteria to a campaign's history.",
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
                help=(
                    "Plateau baseline. ``all_time_best`` (default): fires when the "
                    "recent ``--plateau-window`` iters didn't beat the all-time prior "
                    "best — 'no new record in N iters'. ``prior_window``: fires when "
                    "they didn't beat the prior window of equal size — 'improvements "
                    "have stalled'. The prior_window mode requires 2*window history."
                ),
            ),
        ),
        group="campaign",
    ),
)
def campaign_converged(
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
) -> dict[str, Any]:
    """Apply stop criteria to the campaign's reduced-metric history.

    Returns ``{converged, reason, iterations, best_metric, ...}``.
    The agent reads ``converged`` to decide whether to launch the next
    iteration; ``reason`` is human-readable for the slash-command UX.

    Manifest defaults: any arg left as ``None`` falls back to the
    matching field under ``stop_criteria`` in
    ``<campaign_dir>/manifest.json`` if the manifest exists. Explicit
    CLI args always win.

    ``plateau_mode`` (defaults to ``"all_time_best"``) selects which
    baseline the recent window is compared to:

    * ``"all_time_best"`` (default): fires when the recent ``window``
      iterations failed to beat the all-time prior best by more than
      ``tolerance``. Read as "no new record in N iters." Good fit when
      each iter is expensive and a single record is the stop signal.
      Requires at least ``window + 1`` history points.
    * ``"prior_window"``: fires when the recent ``window`` iterations
      failed to beat the *prior window of equal size* by more than
      ``tolerance``. Read as "improvements have stalled vs the last
      window." Good fit for fine-tuning campaigns where the first
      record isn't the answer. Requires ``2 * window`` history points.
    """
    from hpc_agent.execution.mapreduce.reduce.history import prior
    from hpc_agent.meta.campaign.manifest import read_manifest

    manifest_stop: dict[str, Any] = {}
    try:
        manifest = read_manifest(experiment_dir, campaign_id)
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError):
        # See campaign_budget.py for the rationale; we narrow this so
        # ^C during a long scan isn't silently swallowed.
        manifest = None
    if manifest is not None:
        manifest_stop = manifest.get("stop_criteria") or {}

    if max_iters is None:
        max_iters = manifest_stop.get("max_iters")
    if metric is None:
        metric = manifest_stop.get("metric")
    if target is None:
        target = manifest_stop.get("target")
    resolved_direction: OptimizationDirection = (
        direction if direction is not None else (manifest_stop.get("direction") or "minimize")
    )
    if plateau_window is None:
        plateau_window = manifest_stop.get("plateau_window")
    if plateau_tolerance is None:
        plateau_tolerance = float(manifest_stop.get("plateau_tolerance") or 0.0)
    resolved_mode: PlateauMode = (
        plateau_mode
        if plateau_mode is not None
        else (manifest_stop.get("plateau_mode") or "all_time_best")
    )

    history = prior(experiment_dir, campaign_id)
    n_iters = sum(1 for entry in history if entry)  # only completed iters

    if max_iters is not None and n_iters >= int(max_iters):
        return {
            "converged": True,
            "reason": f"max_iters_reached ({n_iters} >= {max_iters})",
            "iterations": n_iters,
            "best_metric": None,
        }

    metric_values = _extract_metric(history, metric) if metric else []
    best = _best(metric_values, resolved_direction)

    if metric and target is not None and best is not None:
        meets = best <= float(target) if resolved_direction == "minimize" else best >= float(target)
        if meets:
            return {
                "converged": True,
                "reason": f"target_met (best {metric}={best} crossed {target})",
                "iterations": n_iters,
                "best_metric": best,
            }

    if metric and plateau_window is not None:
        window = int(plateau_window)
        if window <= 0:
            # Explicit short-circuit: a non-positive plateau window means
            # the caller asked for "no plateau check" — say so plainly
            # rather than silently slicing an empty window.
            return {
                "converged": False,
                "reason": f"plateau_check_disabled (plateau_window={window})",
                "iterations": n_iters,
                "best_metric": best,
            }
        # Two modes — see docstring. Both compute (prior_best,
        # recent_best, improved) and fire the same plateau envelope; the
        # only difference is which baseline ``prior_best`` measures.
        if resolved_mode == "prior_window":
            min_history = 2 * window
            prior_slice = (
                metric_values[-2 * window : -window] if len(metric_values) >= min_history else []
            )
        else:
            min_history = window + 1
            prior_slice = metric_values[:-window] if len(metric_values) >= min_history else []
        if len(metric_values) >= min_history:
            prior_best = _best(prior_slice, resolved_direction)
            recent_best = _best(metric_values[-window:], resolved_direction)
            if prior_best is not None and recent_best is not None:
                improved = (
                    (prior_best - recent_best)
                    if resolved_direction == "minimize"
                    else (recent_best - prior_best)
                )
                if improved <= float(plateau_tolerance):
                    baseline_label = (
                        f"vs prior {window}"
                        if resolved_mode == "prior_window"
                        else "vs all-time best"
                    )
                    return {
                        "converged": True,
                        "reason": (
                            f"plateau (last {window} iters improved by {improved:.6g} "
                            f"<= tolerance {plateau_tolerance} {baseline_label})"
                        ),
                        "iterations": n_iters,
                        "best_metric": best,
                    }

    if max_iters is None and metric is None:
        return {
            "converged": False,
            "reason": "no_criteria",
            "iterations": n_iters,
            "best_metric": best,
        }

    return {
        "converged": False,
        "reason": "criteria_not_met",
        "iterations": n_iters,
        "best_metric": best,
    }
