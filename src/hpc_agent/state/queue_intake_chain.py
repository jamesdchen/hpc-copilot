"""CHAIN INTAKE — a run the CHAIN started lands on the queue ledger too (P2.c).

The run queue's projection is only as honest as its intake. ``queue-run`` is the
ledger's one writer for runs a human or a workflow ENQUEUED, but a run started by
the block chain (the onboard chain's exit into ``submit-s1``, or a bare
``submit-s1``) never touched it — so ``queue-status`` showed an idle queue while a
run was live, ``queue-advance`` counted occupancy that did not include it, and the
wake edge (``ops/queue/chain.chain_dispatch_on_retire``) had a retirement with no
corresponding arrival. The ledger described a different fleet from the real one.

This module is the producer seat that closes it. Three properties make it safe to
call from a hot path:

* **Ungated, by the same R2 reasoning ``queue-run`` states**: recording that a run
  exists spends nothing and authorizes nothing. Gates bind where they always bind
  — at the cluster boundary of the run itself. Nothing here consults consent,
  probes a greenlight, or reaches a cluster.
* **Idempotent on re-entry**, by the ledger's own dedup rather than by a new
  check: the request token is derived from the ``run_id``
  (:func:`chain_intake_request_id`), so every re-drive of the same run computes
  the same token and :func:`~hpc_agent.state.queue_intake.append_intake_item`'s
  in-flock probe writes no second line. One journaled item per run_id, forever —
  including across the compaction tombstones (a settled, compacted run comes back
  as the synthetic tombstone record and nothing is written).
* **Never raises.** A run's progress must never depend on the queue ledger being
  writable. Every failure is swallowed and returned as ``None``; the disclosure a
  caller attaches is then simply absent, which is the honest shape.

WHY the run_id and not a fresh uuid: the ledger's join key to the real fleet is
``item_run_id`` (run ids are COMPUTED at enqueue in the plan's §10.S3 path), so a
chain-recorded item that carries the run's own id is joinable by exactly the
machinery that already exists. A random token would put an item on the ledger that
no projection could ever match to the run it describes.

WHY it lives in ``state/`` and not in ``ops/queue/``: its caller is
``ops/submit_blocks.py``, a role-root file whose ``composes=`` declares the submit
rings — not the queue subject. The layering rule (``scripts/lint_subject_imports.py``)
says a cross-subject reach routes through ``hpc_agent.state.*``, and this helper IS
substrate: it writes one ledger line and interprets nothing. Putting it here is the
sanctioned route rather than an allowlist exception. It reaches only
:mod:`hpc_agent.state.queue_intake` — no ``ops`` import, so the state-must-not-reach
-up rule holds by construction.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["CHAIN_INTAKE_ORIGIN", "chain_intake_request_id", "record_chain_intake"]

_log = logging.getLogger(__name__)

#: Stamped on every chain-recorded item so a reader can tell an item the CHAIN
#: recorded from one a caller ENQUEUED. They mean different things — one is a
#: request awaiting placement, the other a statement that a run already exists —
#: and a projection that conflated them would report a backlog that is really a
#: fleet.
CHAIN_INTAKE_ORIGIN = "chain"

#: Prefix of the derived idempotency token. Distinct from any client-minted
#: ``request_id`` shape so a chain item can never dedup against a real enqueue.
_REQUEST_PREFIX = "chain:"


def chain_intake_request_id(run_id: str) -> str:
    """The DERIVED intake token for *run_id* — deterministic, hence replay-safe.

    Every re-entry of the same run (a re-drive, a resumed park, a second driver
    racing the first) computes this same token, so the ledger's dedup probe is what
    enforces "one item per run_id" — no new uniqueness machinery, and no read
    -then-write window of our own.
    """
    if not run_id or not run_id.strip():
        raise ValueError("run_id must be a non-empty string (it is the dedup key)")
    return f"{_REQUEST_PREFIX}{run_id}"


def record_chain_intake(
    experiment_dir: Path, *, run_id: str | None, origin_block: str
) -> dict[str, Any] | None:
    """Record ONE queue-intake item for a chain-started run; ``None`` if nothing was.

    Returns the ledger's own answer: ``None`` when there was no run to record
    (a boundary that minted none), when the write failed, or when the append
    DEDUPED (the ledger returns the pre-existing record only on replay, and a
    replay is precisely "already recorded"). Otherwise the disclosure dict a
    caller may surface.

    *origin_block* names the block that observed the run into existence
    (``submit-s1``, or the onboard chain's terminal) — recorded verbatim so the
    ledger says WHERE an item came from rather than leaving a reader to guess.
    """
    if not run_id:
        return None
    try:
        from hpc_agent.state.queue_intake import append_intake_item, is_compaction_tombstone

        request_id = chain_intake_request_id(run_id)
        record = append_intake_item(
            experiment_dir,
            record={
                "run_id": run_id,
                "origin": CHAIN_INTAKE_ORIGIN,
                "origin_block": origin_block,
                "reason": (
                    "the block chain started this run directly; the item is recorded "
                    "so the queue projection reflects the real fleet. Recording "
                    "spends nothing and authorizes nothing — the gates bind at the "
                    "run's own cluster boundary."
                ),
            },
            request_id=request_id,
        )
    except Exception:  # noqa: BLE001 — a run's progress never depends on the ledger
        _log.info(
            "chain queue-intake for %s (%s) skipped (best-effort)",
            run_id,
            origin_block,
            exc_info=True,
        )
        return None
    if record is None:
        return {
            "recorded": True,
            "run_id": run_id,
            "request_id": request_id,
            "origin_block": origin_block,
        }
    # A returned record means the append found one already there: either a prior
    # entry of this same run (the idempotent case) or a compaction tombstone for a
    # run that already settled. Both are "already accounted for", stated honestly.
    return {
        "recorded": False,
        "run_id": run_id,
        "request_id": request_id,
        "origin_block": origin_block,
        "reason": (
            "the run's queue item was already recorded and its records have since "
            "been compacted (the run settled) — nothing was written."
            if is_compaction_tombstone(record)
            else "the run already has a queue item — nothing was written (idempotent)."
        ),
    }
