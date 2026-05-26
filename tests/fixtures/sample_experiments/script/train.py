"""Minimal script-shape experiment used by tests/incorporation/test_script_sample_experiment.py.

This fixture exists to prove the `.py`-only on-ramp works end-to-end:
discovery finds the @register_run function, tasks.py drives the
dispatch, and the function actually runs against resolved kwargs.
"""

from __future__ import annotations

from hpc_agent.experiment_kit import register_run


@register_run
def run(seed: int, lr: float) -> dict:
    """Return per-task metrics so the fixture is observable from a test."""
    return {"score": lr * (seed + 1), "seed": seed, "lr": lr}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--lr", type=float, required=True)
    args = ap.parse_args()
    print(run(**vars(args)))
