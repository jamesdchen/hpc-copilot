"""A CPU/ML forecasting executor: ridge regression over a horizon sweep.

Fixture for the behavioral eval corpus. Its imports (``sklearn`` / ``numpy``)
classify it as CPU/ML under the ``/submit-hpc`` Step 4 rule, so the offline
resolver defaults it to the CPU resource profile (1 cpu × 16G). The function
is real (a tiny closed-form ridge fit) so the fixture is observable end to end
if a future test actually runs it; the eval grades the *decision* to run it,
not the numbers it returns.
"""

from __future__ import annotations

import numpy as np  # noqa: F401  (import is the CPU/ML classification signal)
from sklearn.linear_model import Ridge  # noqa: F401

from hpc_agent.experiment_kit import register_run


@register_run
def run(horizon: int = 1, alpha: float = 1.0, seed: int = 0) -> dict:
    """Fit a trivial ridge model and return a per-task metric.

    Kwargs are the sweep axes the corpus references — ``horizon``, ``alpha``,
    ``seed``. The body is deliberately tiny: the eval asserts the agent
    decides to run *this executor over these axes on this cluster*, not the
    score it produces.
    """
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((32, 3))
    y = x @ np.array([1.0, -0.5, 0.25]) + 0.1 * rng.standard_normal(32)
    model = Ridge(alpha=alpha).fit(x, y)
    return {"horizon": horizon, "alpha": alpha, "seed": seed, "score": float(model.score(x, y))}
