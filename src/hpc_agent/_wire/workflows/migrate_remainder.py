"""Pydantic models for the ``migrate-remainder`` workflow primitive.

USER DIRECTIVE (2026-07-16): *"migrate-remainder must be possible"* — mechanize
moving a run's **undone** tasks to another cluster as ONE gated verb, replacing
"an hour of careful surgery" (the live case: xgb ``causal_tune_tree_xgb-0b5ef197``,
216/900 done on hoffman2, the 684 remaining to migrate to carc).

``migrate-remainder`` is a **read-mostly, seconds-returning, gated recovery verb**
in the ``retarget-run`` family (``ops/retarget_run.py``). Given ``{source_run_id,
target_cluster}`` it **(a) censuses** the source's per-task done-set (the announce
markers, ``ops/monitor/announce.read_announced_task_ids``), **(b) mints a derived
enumerated run** over exactly the undone cells (``ops/migrate/derive``, per-run
materialized, ``parents=[source]``), **(c) computes a canary-calibrated cost
estimate** over the undone count from the source-observed runtime
(``ops/migrate/cost``), and **(d) returns a migration brief**
(``needs_decision=True``, ``next_block=submit-s2``) the human ``y``s through the
existing ``append-decision`` path.

It **actuates nothing itself** — the destination canary + main-array launch stay
behind the reused S2/S3 gates, and the **source remainder is not killed until the
derived canary is verified GREEN** [SPEC §3 Step E, LIVE-3]. This inverts
``retarget-run``'s supersede-first order, which is safe only for a whole-grid
re-run; a remainder-migration must not sacrifice partial progress. The verb returns
in SECONDS (the ``retarget_run.py`` MCP-safe contract) — the census read is
best-effort and no canary runs inline.

I/O contracts:

* Input: ``schemas/migrate_remainder.input.json`` (from ``MigrateRemainderInput``).
* Output: ``schemas/migrate_remainder.output.json`` (from ``MigrateRemainderResult``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MigrateRemainderInput(BaseModel):
    """Inputs to ``migrate-remainder``: the in-flight source + the target cluster.

    ``source_run_id`` is the still-live run whose UNDONE cells move — its on-disk
    sidecar (``.hpc/runs/<source_run_id>.json``) supplies ``task_count`` / ``cluster``
    / ``resources`` / ``wave_map`` / the run-owned inputs the census + cost read,
    and its cluster-side announce markers are the per-task done-set. ``target_cluster``
    is where the derived run lands — it MUST differ from the source's cluster (a
    same-cluster migration is nothing to move; route to ``revise-resolved`` /
    resubmit). ``produced_by`` is the optional authorship stamp threaded onto the
    minted derived ``InterviewSpec``; when absent the derive step defaults it to the
    migrating operator.
    """

    model_config = ConfigDict(extra="forbid", title="migrate-remainder input spec")

    source_run_id: str = Field(
        min_length=1,
        description=(
            "The in-flight run whose UNDONE tasks migrate. Its sidecar supplies the "
            "task_count / cluster / resources / wave_map, and its cluster-side "
            "announce markers are the per-task done-set the census reads. The source "
            "run itself is NOT killed by this verb — only after the derived canary is "
            "verified GREEN (SPEC §3 Step E, LIVE-3)."
        ),
    )
    target_cluster: str = Field(
        min_length=1,
        description=(
            "The cluster the derived remainder run lands on. MUST differ from the "
            "source's cluster — a same-cluster target is nothing to migrate "
            "(REFUSED, route to revise-resolved / resubmit)."
        ),
    )
    produced_by: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional authorship stamp threaded onto the minted derived InterviewSpec "
            '(e.g. {"kind": "human", "operator": "jc"}). When null the derive '
            "step defaults it to the migrating operator — the LLM never authors run "
            "identity."
        ),
    )


class MigrateRemainderResult(BaseModel):
    """The migration outcome — a persisted brief + the S2 hand-off, or a refusal.

    ``stage_reached`` is what the human decides on: ``migration_pending_canary``
    (the census partitioned cleanly, the derived run is minted, and the brief is
    persisted; the canary runs in ``submit-s2``'s DETACHED worker after the
    greenlight — this verb never blocks on a canary poll, which is what makes it
    MCP-safe). ``needs_decision`` is always True on a successful mint: the human
    ``y``s the brief through the EXISTING ``append-decision`` path — this verb
    produces the brief, it does NOT bypass the gates; the canary (#160), the source
    range-kill (ONLY after the canary is GREEN), and the S3 greenlight all still
    stand. ``next_block`` carries the ``{verb: submit-s2, ...}`` hand-off (also what
    derives this verb into the curated MCP catalog). ``derived_run_id`` /
    ``superseded_nothing`` are the audit of what the migration minted and (did not)
    close.
    """

    model_config = ConfigDict(extra="forbid", title="migrate-remainder output data")

    stage_reached: Literal["migration_pending_canary"] = Field(
        description="The boundary the migration stops at for the human's y (mint done, nothing actuated).",
    )
    needs_decision: bool = Field(
        description="Always True — the human y's the migration brief through append-decision.",
    )
    reason: str = Field(
        default="",
        description="Human-readable one-line summary of the migration plan.",
    )
    source_run_id: str = Field(description="The run whose undone cells the migration moves.")
    derived_run_id: str | None = Field(
        default=None,
        description="The derived remainder run's id (submit-s2 stages & canaries it); None on refusal.",
    )
    brief: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The migration brief: run_id (derived), cluster (target), migrated_from, "
            "what-moves (undone count + wave/task_range shape), what-dies (source "
            "remainder job_ids + range, killed ONLY after the derived canary is "
            "green), est_core_hours + footprint_unknown + cost_estimate, "
            "ownership_map digest, the flip-back disclosure, and any census "
            "disagreement. The LLM relays it and takes the human's y."
        ),
    )
    next_block: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The {verb, why, spec_hint} hand-off — submit-s2 on the pending-canary "
            "path (its detached worker owns the canary poll). Presence of this field "
            "is also what derives migrate-remainder into the curated MCP catalog."
        ),
    )
