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

from typing import TYPE_CHECKING, Any, Literal

from claude_hpc._internal._primitive import primitive

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
)
def campaign_converged(
    *,
    experiment_dir: Path,
    campaign_id: str,
    max_iters: int | None = None,
    metric: str | None = None,
    target: float | None = None,
    direction: Literal["minimize", "maximize"] = "minimize",
    plateau_window: int | None = None,
    plateau_tolerance: float = 0.0,
) -> dict[str, Any]:
    """Apply stop criteria to the campaign's reduced-metric history.

    Returns ``{converged, reason, iterations, best_metric, ...}``.
    The agent reads ``converged`` to decide whether to launch the next
    iteration; ``reason`` is human-readable for the slash-command UX.
    """
    from claude_hpc.mapreduce.reduce.history import prior

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
    best = _best(metric_values, direction)

    if metric and target is not None and best is not None:
        meets = best <= float(target) if direction == "minimize" else best >= float(target)
        if meets:
            return {
                "converged": True,
                "reason": f"target_met (best {metric}={best} crossed {target})",
                "iterations": n_iters,
                "best_metric": best,
            }

    if metric and plateau_window is not None and len(metric_values) >= int(plateau_window) + 1:
        window = int(plateau_window)
        prior_best = _best(metric_values[: -window], direction)
        recent_best = _best(metric_values[-window:], direction)
        if prior_best is not None and recent_best is not None:
            improved = (
                (prior_best - recent_best) if direction == "minimize" else (recent_best - prior_best)
            )
            if improved <= float(plateau_tolerance):
                return {
                    "converged": True,
                    "reason": (
                        f"plateau (last {window} iters improved by {improved:.6g} "
                        f"<= tolerance {plateau_tolerance})"
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
