"""Field→stage ownership facade — the §4 routing SoT (the load-bearing gap).

block-drive.md §4 routes a nudge by which stage OWNS the edited field:

| Approved spec vs last-run inputs | Route |
|---|---|
| unchanged                                   | **advance** to the next block |
| changed, field owned by the **current** block | **re-run** the block |
| changed, fields owned by a **downstream** block | **advance, carrying the edit** |

That decision needs a field→stage map per workflow — which block RESOLVES each
field. Today only submit has ``ops/submit/field_partition.py`` (a required-vs-
auto-resolvable partition, NOT a stage map). This module is the missing map for
all four families: :data:`OWNERSHIP` maps ``{workflow: {field: owning_verb}}``,
and :func:`route` turns "which fields changed" + the current block position into
the §4 advance / rerun / advance_carrying decision, using
:mod:`hpc_agent.infra.block_chain` for block ordering.

§4 itself flags ownership completeness as the load-bearing open gap: submit's
map is read off ``submit_blocks.py`` (resource resolution → S1, cost/canary →
S2, submit/watch → S3, harvest → S4); status/aggregate/campaign fields land on
the FIRST block of their family (their specs are greenlit / read once, so there
is no meaningful downstream owner yet) with ``# TODO(wave4)`` markers where the
grain is genuinely ambiguous. Do not over-engineer past what §4 needs.
"""

from __future__ import annotations

from typing import Literal

from hpc_agent.infra.block_chain import block_index

# Re-exported so subject files (e.g. ``ops/decision/journal.py``'s
# human-authorship gate) can reach the required-caller partition through this
# TOP-LEVEL facade via the package alias form (``from hpc_agent.ops import
# field_ownership``) — the direct ``hpc_agent.ops.submit.field_partition``
# import trips the subject-import lint from inside another subject. One
# source of truth: this binds, never copies, the partition's frozenset.
from hpc_agent.ops.submit.field_partition import REQUIRED_CALLER_FIELDS

__all__ = [
    "OWNERSHIP",
    "REQUIRED_CALLER_FIELDS",
    "field_owner",
    "route",
]

Route = Literal["advance", "rerun", "advance_carrying"]


# ``{workflow: {field: owning_verb}}`` — the block that RESOLVES / first consumes
# each field. Read off the four block modules + their _wire Spec models.
OWNERSHIP: dict[str, dict[str, str]] = {
    # ── submit ────────────────────────────────────────────────────────────────
    # Fields from ops/submit/field_partition.py, mapped to S1..S4 by which block
    # first resolves them (submit_blocks.py): S1 resolves the whole submit-flow
    # spec (resources + inputs); S2 is the canary + cost estimate; S3 the main
    # launch + monitor; S4 the harvest.
    "submit": {
        # S1 (resolve): goal/task_generator relayed, resources resolved
        # (resolve-resources), data/axes/configs/entry_point resolved
        # (resolve-submit-inputs).
        "goal": "submit-s1",
        "task_generator": "submit-s1",
        "cluster": "submit-s1",
        "gpu_type": "submit-s1",
        "partition": "submit-s1",
        "mpi_pe": "submit-s1",
        "data_axis": "submit-s1",
        "homogeneous_axes": "submit-s1",
        "frozen_configs": "submit-s1",
        "entry_point": "submit-s1",
        "uncovered_param": "submit-s1",
        # S2 (stage & canary): walltime/cost-cap is FIRST CONSUMED by S2's
        # cost estimate (_estimate_for_submit) and the canary submit — the §4
        # "cap the cost" nudge lands here, editing a downstream block's input so
        # an S1 greenlight can advance-carrying rather than needlessly re-resolve.
        # TODO(wave4): confirm ownership — walltime is RESOLVED as a resource in
        # S1 but first CONSUMED for cost in S2; §4 keys on the consuming stage.
        "walltime_sec": "submit-s2",
    },
    # ── status ──────────────────────────────────────────────────────────────
    # Two blocks (snapshot → watch). snapshot's read knobs vs watch's monitor
    # spec. Fields map to the block whose Spec model carries them.
    "status": {
        # status-snapshot (StatusSnapshotSpec).
        "run_id": "status-snapshot",
        "reconcile": "status-snapshot",
        "scheduler": "status-snapshot",
        "now_iso": "status-snapshot",
        "mark_seen": "status-snapshot",
        # status-watch (StatusWatchSpec).
        "monitor": "status-watch",
        "invocation_argv": "status-watch",
        "user_invoked_via_loop": "status-watch",
    },
    # ── aggregate ─────────────────────────────────────────────────────────────
    # Two blocks (check → run). check's readiness/integrity knobs vs run's
    # aggregate-flow spec.
    "aggregate": {
        # aggregate-check (AggregateCheckSpec).
        "run_id": "aggregate-check",
        "run_preflight": "aggregate-check",
        "reconcile_scheduler": "aggregate-check",
        "allow_partial": "aggregate-check",
        # aggregate-run (AggregateRunSpec).
        "aggregate": "aggregate-run",
    },
    # ── campaign ──────────────────────────────────────────────────────────────
    # Three touchpoints, but the campaign spec is greenlit ONCE at start
    # (campaign-greenlight owns the manifest contract); watch/complete only read
    # the campaign_id. So every input field lands on the FIRST block.
    # TODO(wave4): confirm ownership — campaign has no per-iteration boundary, so
    # "downstream ownership" of a spec field is not yet meaningful (§4 open gap).
    "campaign": {
        "campaign_id": "campaign-greenlight",
        "confirm": "campaign-greenlight",
        "response": "campaign-greenlight",
        "proposal": "campaign-greenlight",
        "journal": "campaign-greenlight",
    },
}


def field_owner(workflow: str, field: str) -> str | None:
    """Return the block verb that RESOLVES *field* in *workflow*, or ``None``.

    Looks up :data:`OWNERSHIP`. Policy for an unknown field (or unknown
    workflow): return ``None`` — meaning "unattributed; treat as the CURRENT
    block and re-run to be safe". :func:`route` applies that conservative default
    (an unowned changed field forces a re-run rather than a blind advance, so an
    edit is never silently carried past the block that might depend on it).
    """
    return OWNERSHIP.get(workflow, {}).get(field)


def route(
    workflow: str,
    current_verb: str,
    changed_fields: set[str],
    stage_reached: str,
) -> Route:
    """The §4 routing decision for a set of edited fields at *current_verb*.

    Rules (block-drive.md §4), comparing block positions via
    :func:`hpc_agent.infra.block_chain.block_index`:

    * no changed fields → ``"advance"`` (a plain ``y`` — advance to the
      code-determined next block);
    * any changed field owned by *current_verb* OR by a block EARLIER than it →
      ``"rerun"`` (recompute the owning block's derived fields and emit a fresh
      brief). The earlier-owner case is a rewind/cascade — treated as a re-run for
      now. TODO(wave4): cascade/rewind semantics are open per §4 (whether a rewind
      re-runs everything downstream, and e.g. whether S2's canary re-fires);
    * all changed fields owned strictly DOWNSTREAM of *current_verb* →
      ``"advance_carrying"`` (no needless re-run — the S2 "cap the cost" nudge
      edits S3's inputs, so an S2 greenlight advances carrying the edit).

    An unowned changed field (``field_owner`` → None) is conservatively treated as
    current-block-owned → ``"rerun"``. *stage_reached* is accepted for the
    driver's call shape (it computes the advance target via
    ``block_chain.successor_verb(current_verb, stage_reached)``); the routing
    decision itself does not branch on it.
    """
    if not changed_fields:
        return "advance"

    current_idx = block_index(current_verb)
    for field in changed_fields:
        owner = field_owner(workflow, field)
        if owner is None:
            # Unattributed edit → treat as current-block, re-run to be safe.
            return "rerun"
        # An owner at or before the current block forces a re-run (current-block
        # recompute, or an earlier-block rewind).
        if block_index(owner) <= current_idx:
            return "rerun"

    # Every changed field is owned strictly downstream — carry the edit forward.
    return "advance_carrying"
