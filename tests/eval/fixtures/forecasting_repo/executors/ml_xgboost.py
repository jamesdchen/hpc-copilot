"""A CPU/ML forecasting executor: gradient-boosted trees over a horizon sweep.

Companion to ``ml_ridge`` in the forecasting eval fixture. Imports
``xgboost`` / ``numpy`` → classified CPU/ML, so the offline resolver defaults
it to the CPU resource profile. Paired with ``ml_ridge`` in the multi-executor
corpus cases (the ``executor`` axis lists both).
"""

from __future__ import annotations

import numpy as np  # noqa: F401  (CPU/ML classification signal)
import xgboost as xgb  # noqa: F401

from hpc_agent.experiment_kit import register_run


@register_run
def run(horizon: int = 1, seed: int = 0) -> dict:
    """Return a per-task metric for a trivial boosted-tree fit.

    Body kept minimal on purpose — the eval grades the decision to run this
    executor over ``(horizon, seed)``, not the metric it computes.
    """
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((64, 4))
    y = (x[:, 0] + x[:, 1] > 0).astype(int)
    model = xgb.XGBClassifier(n_estimators=8, max_depth=2).fit(x, y)
    return {"horizon": horizon, "seed": seed, "score": float(model.score(x, y))}
