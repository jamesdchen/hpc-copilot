"""Stochastic distribution sampler for the DES queue simulator.

Extracted from :mod:`queue_simulator` for navigability. The base
module (:mod:`queue_simulator`) owns the deterministic
``simulate_one_pass`` algorithm + the per-job placement helpers; this
module owns the Monte Carlo wrapper that runs N replications and
returns p10/p50/p90 quantiles.
"""

from __future__ import annotations

import dataclasses
import random
from typing import TYPE_CHECKING, Any

from hpc_agent_pro.forecast.queue_simulator import (
    DEFAULT_WALLTIME_ACTUAL_BAND,
    SimJob,
    SimResult,
    simulate_one_pass,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hpc_agent.infra.inspect._common import ClusterSnapshot


def simulate_distribution(
    snapshot: ClusterSnapshot,
    *,
    candidate: SimJob,
    user_profiles: dict[str, Any] | None = None,
    n_replications: int = 64,
    max_horizon_sec: float = 7 * 86400.0,
    seed: int | None = None,
    arrival_sampler: Any = None,
    residual_sampler: Any = None,
    walltime_actual_band: tuple[float, float] = DEFAULT_WALLTIME_ACTUAL_BAND,
) -> SimResult:
    """Run ``n_replications`` simulations with sampled inputs.

    Variance comes from:

    * the sampled future arrival stream (per-user non-homogeneous
      Poisson; via ``arrival_sampler(seed)``) and
    * the sampled actual-walltime per running job (per-user empirical
      ratio; via ``residual_sampler(seed)``).

    When samplers are not provided, only the per-arrival jitter inside
    ``simulate_one_pass`` provides variance; this still yields a
    legitimate (narrower) distribution.

    Returns p10/p50/p90 of the candidate's wait time.
    """
    if n_replications < 1:
        raise ValueError("n_replications must be >= 1")
    waits: list[float] = []
    last_state: dict[str, Any] = {}
    rng = random.Random(seed)
    for _i in range(n_replications):
        # Independent sub-seeds for arrival, residual, and the
        # per-pass policy rng inside simulate_one_pass. Sharing one
        # sub-seed correlates the first draws of distinct
        # random.Random instances, tightening the predicted p10/p90
        # band and hiding genuine variance. The third sub-seed
        # (``pass_seed``) decouples the candidate-walltime sample
        # from the arrival stream — v2 fixed arr↔res but kept
        # arr↔policy coupled via ``seed=arr_seed`` (v3 BUG-5V3-2).
        arr_seed = rng.randint(0, 2**31 - 1)
        res_seed = rng.randint(0, 2**31 - 1)
        pass_seed = rng.randint(0, 2**31 - 1)
        arr = arrival_sampler(arr_seed) if arrival_sampler is not None else None
        res = residual_sampler(res_seed) if residual_sampler is not None else None
        out = simulate_one_pass(
            snapshot,
            candidate=dataclasses.replace(candidate),
            user_profiles=user_profiles,
            arrival_stream=arr,
            residual_lifetimes=res,
            max_horizon_sec=max_horizon_sec,
            seed=pass_seed,
            walltime_actual_band=walltime_actual_band,
        )
        waits.append(out.predicted_start_offset_sec)
        last_state = out.predicted_state_at_horizon
    waits.sort()

    def _pct(p: float) -> float:
        if not waits:
            return max_horizon_sec
        if len(waits) == 1:
            return waits[0]
        k = (len(waits) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(waits) - 1)
        frac = k - lo
        return waits[lo] + frac * (waits[hi] - waits[lo])

    return SimResult(
        candidate_job_id=candidate.job_id,
        predicted_start_offset_sec=_pct(0.5),
        predicted_state_at_horizon=last_state,
        n_replications=n_replications,
        p10_wait_sec=_pct(0.1),
        p50_wait_sec=_pct(0.5),
        p90_wait_sec=_pct(0.9),
    )


def _job_iter(jobs: Iterable[SimJob]) -> list[SimJob]:
    """Public-style helper for callers that want to iterate w/o exposing list mutability."""
    return list(jobs)
