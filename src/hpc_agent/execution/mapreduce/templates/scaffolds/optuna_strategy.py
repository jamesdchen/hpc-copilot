# ruff: noqa: E501
"""Optuna ask/tell campaign strategy (scaffold).

A worked example of a **scalar-objective** closed-loop campaign. The
objective is just a key inside ``metrics.json`` (``_OBJECTIVE``) — the
framework never privileges it (see ``docs/design/campaign-seam.md``).

Cluster-safe by construction
----------------------------
The cluster-side dispatcher imports this file and calls ``resolve(task_id)``
on the compute node. A stateful optimizer must therefore NOT ``ask`` there —
that would create divergent trials against the synced study copy. So:

* ``import optuna`` and ``study.ask()`` happen ONLY on the orchestrator,
  inside ``_propose`` — never at module top level. The compute node reads the
  already-decided proposal file (synced under ``.hpc/campaigns/<cid>/``) and
  imports no optimizer.
* The per-iteration index is the count of COMPLETED prior iterations, which is
  identical on the orchestrator (proposing iteration N: 0..N-1 done) and the
  cluster (running iteration N: 0..N-1 done, N in-flight) — so both sides agree
  on which proposal file to read. ``_propose`` is idempotent: re-imports (the
  cmd_sha materialization, retries) reuse the persisted proposal rather than
  asking again.
* Load-idempotency is load-bearing, not a nicety: validators
  (``validate-campaign``, ``dry-run-local``), ``compute_cmd_sha``, and any
  ``--dry-run`` submit path all import this module and call
  ``total()``/``resolve()``. Index proposals by completed count as above —
  NEVER by counting on-disk proposal/iter artifacts, because a prior load
  created those: each subsequent load would resolve a fresh index and mint a
  phantom optimizer trial per validation pass.

Reconciliation
--------------
One ask per iteration ⇒ Optuna trial number == oldest-first iteration index, so
``study.tell`` is keyed by index. ``trial_token`` (the trial number) is also
round-tripped via the sidecar for the concurrent / out-of-order case.

NOTE: validate end-to-end on your cluster before a long campaign — the ask/tell
timing is the subtle part and depends on your monitor cadence (one iteration
must finish before the next is proposed).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from hpc_agent.execution.mapreduce.reduce.history import prior_records
from hpc_agent.executor_cli import flag, generic_args

# ─── Tunables ──────────────────────────────────────────────────────────────
_OBJECTIVE = "val_loss"  # metrics.json key to optimize
_DIRECTION = "minimize"
_MAX_TRIALS = 50

_EXPERIMENT_DIR = Path(__file__).resolve().parent.parent  # repo root (parent of .hpc/)
_CID = os.environ.get("HPC_CAMPAIGN_ID", "")
_CAMPAIGN_DIR = Path(__file__).resolve().parent / "campaigns" / _CID
_PROPOSALS_DIR = _CAMPAIGN_DIR / "proposals"

FLAGS: dict[str, list] = {
    "src.train": [
        *generic_args(),
        flag("lr", float, default=1e-3),
        flag("weight_decay", float, default=0.0),
    ],
}


def _history() -> list[dict]:
    return prior_records(_EXPERIMENT_DIR, _CID) if _CID else []


def _completed_count() -> int:
    return sum(1 for rec in _history() if rec["complete"])


def _proposal_path(n: int) -> Path:
    return _PROPOSALS_DIR / f"iter_{n:05d}.json"


def _propose(n: int) -> dict:
    """Orchestrator-only: tell finished trials, ask the next, persist + return.

    Idempotent on the proposal file so re-imports don't leak trials. ``import
    optuna`` is local to this function so the compute node (which only ever
    hits the ``_proposal_path(n).exists()`` fast path) never imports it.
    """
    path = _proposal_path(n)
    if path.exists():
        cached: dict = json.loads(path.read_text(encoding="utf-8"))
        return cached

    import optuna

    study = optuna.create_study(
        storage=f"sqlite:///{_CAMPAIGN_DIR / 'optuna.db'}",
        study_name=_CID or "campaign",
        direction=_DIRECTION,
        load_if_exists=True,
    )
    # Tell every finished prior iteration: oldest-first record i == trial i.
    for i, rec in enumerate(_history()):
        if rec["complete"] and i < len(study.trials) and _OBJECTIVE in rec["metrics"]:
            t = study.trials[i]
            if t.state == optuna.trial.TrialState.RUNNING:
                study.tell(t, rec["metrics"][_OBJECTIVE])

    trial = study.ask()
    proposal = {
        "params": {
            "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
            "weight_decay": trial.suggest_float("weight_decay", 1e-6, 1e-2, log=True),
        },
        "trial_token": trial.number,
    }
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal), encoding="utf-8")
    return proposal


def _current_proposal() -> dict:
    return _propose(_completed_count())


def total() -> int:
    return 0 if _completed_count() >= _MAX_TRIALS else 1


def resolve(task_id: int) -> dict:
    p = _current_proposal()
    # trial_token is reserved bookkeeping: round-tripped to the sidecar,
    # excluded from cmd_sha (see hpc_agent.state.run_sha.RESERVED_TASK_KEYS).
    return {**p["params"], "trial_token": p["trial_token"]}
