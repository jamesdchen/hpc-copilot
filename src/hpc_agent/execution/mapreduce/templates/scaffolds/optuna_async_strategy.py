# ruff: noqa: E501
"""Optuna ask/tell campaign strategy — ASYNC-refill variant (scaffold, #362).

The continuous-async sibling of ``optuna_strategy.py``. Same scalar-objective
ask/tell contract, same orchestrator-vs-compute split, same ``trial_token``
reserved round-trip — but built to keep **K trials in flight at once** instead
of one-at-a-time. ``scaffold-strategy --name optuna --async-refill`` emits this
file; a synchronous campaign keeps the default ``optuna_strategy.py``.

Three differences from the synchronous template, and why each is load-bearing
-----------------------------------------------------------------------------
1. **Tell by ``trial_token`` (out-of-order safe).** The sync template tells
   ``study.trials[i]`` for the i-th oldest record, assuming ``record i ==
   trial i``. Under async refill, trials finish out of order — record 5 may be
   trial 2. So we tell the trial named by the record's own
   ``trial_tokens`` (the reserved key the seam round-trips through the sidecar),
   never by position.

2. **``constant_liar`` sampler.** Asking again while earlier trials are still
   RUNNING (untold) would otherwise repeat near-identical proposals. The
   ``constant_liar`` TPE sampler treats in-flight trials as provisionally-bad,
   decorrelating concurrent asks so K simultaneous trials are K *distinct*
   points.

3. **Index by the SUBMITTED count, not the COMPLETED count.** The sync template
   indexes the proposal by ``_completed_count()`` — fine when exactly one trial
   is ever in flight. Under refill, several iterations are submitted before any
   completes, so successive submits in one tick must each ask a *new* trial.
   The submitted-iteration count (``len(_history())``) advances by one per
   submit (each writes a campaign sidecar), so submit N reads/writes proposal
   ``iter_{N}`` and asks trial N; submit N+1 reads ``iter_{N+1}`` and asks
   trial N+1. The ``constant_liar`` sampler keeps those two asks distinct.

Cluster-safety + load-idempotency (UNCHANGED, and still load-bearing)
---------------------------------------------------------------------
* ``import optuna`` and ``study.ask()`` happen ONLY on the orchestrator inside
  ``_propose`` — never at module top level — so the compute node imports no
  optimizer.
* The compute node reads its kwargs from the sidecar's ``trial_params`` (written
  at submit time by ``compute-run-id``), NOT by calling ``resolve()`` — so it
  never re-asks. This template's submitted-count index is therefore an
  orchestrator-only concern; it depends on that fast path (every async-scaffold
  submit populates ``trial_params``, so the dispatcher never falls back to a
  compute-node ``resolve()`` re-import).
* ``_propose`` is idempotent on its proposal file: within one submit the index
  (``len(_history())``) is stable until that submit writes its sidecar, so the
  cmd_sha-materialization re-import, validators, and ``--dry-run`` all reuse the
  persisted proposal rather than minting a phantom trial. Index by submitted
  count as above — NEVER by counting on-disk proposal artifacts, because a prior
  load created those.

NOTE: validate end-to-end on your cluster before a long campaign — the
async ask/tell + K-in-flight timing is the subtle part. See the live-verify
runbook (``docs/runbooks/campaign-async-live-verify.md``).
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


def _submitted_count() -> int:
    """Number of iterations submitted so far (complete + in flight).

    The async proposal index: each submit writes one campaign sidecar, so this
    advances by one per submit, giving every refilled iteration its own trial.
    Stable within a single submit (that submit's sidecar is not written until
    after compute-run-id materializes the task list), so re-imports are
    idempotent on the proposal file.
    """
    return len(_history())


def _proposal_path(n: int) -> Path:
    return _PROPOSALS_DIR / f"iter_{n:05d}.json"


def _tell_finished(study, optuna) -> None:
    """Tell every finished prior trial by its ``trial_token`` (out-of-order safe).

    Reads each completed record's ``trial_tokens`` (the reserved key the sidecar
    round-trips) and tells the trial of that number — never by record position,
    which breaks the moment results land out of order under refill. RUNNING-guarded
    so a re-tell (re-import / retry) is a no-op.
    """
    n_trials = len(study.trials)
    for rec in _history():
        if not rec["complete"] or _OBJECTIVE not in rec["metrics"]:
            continue
        tokens = rec.get("trial_tokens") or []
        if not tokens:
            continue
        # One ask per iteration (B=1), so the iteration's first token IS its
        # trial number. (A chunked B>1 variant would loop the tokens here.)
        trial_number = tokens[0]
        if not isinstance(trial_number, int) or not (0 <= trial_number < n_trials):
            continue
        trial = study.trials[trial_number]
        if trial.state == optuna.trial.TrialState.RUNNING:
            study.tell(trial, rec["metrics"][_OBJECTIVE])


def _propose(n: int) -> dict:
    """Orchestrator-only: tell finished trials, ask the next, persist + return.

    Idempotent on the proposal file so re-imports don't leak trials. ``import
    optuna`` is local to this function so the compute node (which only ever
    reads the persisted proposal / sidecar trial_params) never imports it.
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
        # constant_liar decorrelates concurrent (untold, in-flight) asks so K
        # simultaneous trials are K distinct points — the correctness half of
        # async refill.
        sampler=optuna.samplers.TPESampler(constant_liar=True),
        load_if_exists=True,
    )
    _tell_finished(study, optuna)

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
    # Index by submitted count (not completed) so each refilled iteration asks a
    # distinct trial — the async difference from the synchronous template.
    return _propose(_submitted_count())


def total() -> int:
    # B=1 per submit (granularity decision #362): the campaign resolver loops
    # refill_count submits, each re-importing this module to ask the next trial.
    # Do NOT return refill_count here.
    return 0 if _completed_count() >= _MAX_TRIALS else 1


def resolve(task_id: int) -> dict:
    p = _current_proposal()
    # trial_token is reserved bookkeeping: round-tripped to the sidecar,
    # excluded from cmd_sha (see hpc_agent.state.run_sha.RESERVED_TASK_KEYS).
    return {**p["params"], "trial_token": p["trial_token"]}
