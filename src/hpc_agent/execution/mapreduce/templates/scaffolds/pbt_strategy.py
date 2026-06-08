# ruff: noqa: E501
"""Population-Based Training campaign strategy (scaffold).

A worked example of an **artifact-carrying** closed-loop campaign — the
class a scalar-objective channel cannot express (see
``docs/design/campaign-seam.md``). Each generation submits ``_POP`` members;
the next generation clones the checkpoint of a top performer and perturbs
its hyperparameters. The "what to carry forward" is a **file** (a
checkpoint under a prior task's ``result_dir``), surfaced by
``prior_records(...)["result_dirs"]``.

Cluster-safe by construction
----------------------------
The cluster-side dispatcher imports this file and calls ``resolve(task_id)``
on the compute node, so ``resolve`` MUST be a deterministic, pure function of
already-finished state. It is: the survivor selection reads only COMPLETED
prior generations (``rec["complete"]`` is True), and the in-flight generation
is excluded identically on the orchestrator and the cluster. The perturbation
is seeded by ``(generation, member)`` so it reproduces on both sides. No
external optimizer is imported anywhere — pure stdlib + ``prior_records``.

Contract for your executor
---------------------------
Each task writes ``metrics.json`` into its ``RESULT_DIR`` containing at least
the objective key (``_FITNESS_KEY``) and the hyperparameters it ran with, plus
a checkpoint file named ``_CKPT_NAME``. The next generation reads those back.
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from hpc_agent.execution.mapreduce.reduce.history import prior_records
from hpc_agent.executor_cli import flag, generic_args, gpu_args

# ─── Tunables ──────────────────────────────────────────────────────────────
_POP = 4  # members per generation (this iteration's task_count)
_MAX_GENERATIONS = 20
_FITNESS_KEY = "fitness"  # metrics.json key the executor writes (higher is better)
_CKPT_NAME = "ckpt.pt"  # checkpoint filename the executor writes into RESULT_DIR
_LR_LO, _LR_HI = 1e-5, 1e-1

# Repo root is the parent of .hpc/ (this file lives in .hpc/tasks.py).
_EXPERIMENT_DIR = Path(__file__).resolve().parent.parent
_CID = os.environ.get("HPC_CAMPAIGN_ID", "")

FLAGS: dict[str, list] = {
    "src.train": [
        *generic_args(),
        *gpu_args(),
        flag("lr", float, default=1e-3),
        flag("init_ckpt", str, default=""),  # "" → fresh init; else clone this checkpoint
    ],
}


def _completed_generations() -> list[dict]:
    """Finished prior generations, oldest-first (in-flight ones excluded)."""
    return [rec for rec in prior_records(_EXPERIMENT_DIR, _CID) if rec["complete"]] if _CID else []


def _members(result_dirs: list[str]) -> list[dict]:
    """Read each finished member's {fitness, lr, ckpt} from its result_dir."""
    members: list[dict] = []
    for rdir in result_dirs:
        try:
            m = json.loads((Path(rdir) / "metrics.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        members.append(
            {
                "fitness": float(m.get(_FITNESS_KEY, float("-inf"))),
                "lr": float(m.get("lr", 1e-3)),
                "ckpt": str(Path(rdir) / _CKPT_NAME),
            }
        )
    return members


def _perturb(lr: float, *, generation: int, member: int) -> float:
    """Deterministic multiplicative jitter, seeded by (generation, member)."""
    rng = random.Random((generation, member).__hash__())
    return float(min(_LR_HI, max(_LR_LO, lr * rng.choice([0.8, 1.0, 1.25]))))


_GENS = _completed_generations()
_GENERATION = len(_GENS)
# Survivors = top half of the most-recent finished generation, by fitness.
_SURVIVORS = (
    sorted(_members(_GENS[-1]["result_dirs"]), key=lambda x: x["fitness"], reverse=True)[
        : max(1, _POP // 2)
    ]
    if _GENS
    else []
)


def total() -> int:
    return 0 if _GENERATION >= _MAX_GENERATIONS else _POP


def resolve(task_id: int) -> dict:
    member = task_id  # 0.._POP-1
    if not _SURVIVORS:
        # Generation 0: fresh members spread across the lr range.
        lr = float(_LR_LO * (_LR_HI / _LR_LO) ** (member / max(1, _POP - 1)))
        return {"lr": lr, "init_ckpt": "", "trial_token": [_GENERATION, member]}
    # exploit: clone a survivor; explore: perturb its lr. trial_token carries
    # the opaque (generation, member) identity back through the sidecar.
    parent = _SURVIVORS[member % len(_SURVIVORS)]
    return {
        "lr": _perturb(parent["lr"], generation=_GENERATION, member=member),
        "init_ckpt": parent["ckpt"],
        "trial_token": [_GENERATION, member],
    }
