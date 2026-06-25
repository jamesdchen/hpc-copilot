"""Pydantic models for the ``walk-submit-ambiguities`` query.

``walk-submit-ambiguities`` runs the ``hpc-submit`` SKILL Steps 2-6
resolution as deterministic CODE branches instead of LLM-walks-and-fills
prose, and returns the same ``needs_resolution``-shaped envelope the
SKILL produced: ``{resolved, ambiguities}``. The resolution rules and the
field partition (which fields may carry a ``safe_default``) live in code,
so the LLM only resolves the genuine ambiguities the walk surfaces.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# The discriminated task_generator union — reused so a caller-supplied
# generator validates here exactly as the interview primitive validates it.
from hpc_agent._wire.actions.interview import _TaskGenerator
from hpc_agent._wire.queries.recommend_partition import PartitionInfo


class WalkSubmitAmbiguitiesInput(BaseModel):
    """Inputs to the deterministic submit-input walk (SKILL Steps 2-6).

    Each caller-supplied field short-circuits its resolution step (the
    "caller-supplied is authoritative" contract). Absent fields are
    resolved by a deterministic rule where one exists, or accumulated as
    an :class:`~hpc_agent.ops.submit.field_partition.Ambiguity`.
    """

    model_config = ConfigDict(extra="forbid", title="walk-submit-ambiguities input")

    # ── Step 2: cluster.
    cluster: str | None = Field(
        default=None,
        description="Caller-resolved cluster; else auto-resolved from configured_clusters.",
    )
    configured_clusters: list[str] = Field(
        default_factory=list,
        description=(
            "Clusters configured in clusters.yaml. One → auto-use; multiple → "
            "ambiguity with safe_default = first lexicographically."
        ),
    )

    # ── REQUIRED_CALLER_FIELDS: goal + task_generator.
    goal: str | None = Field(
        default=None,
        description=(
            "Free-text campaign goal. REQUIRED_CALLER_FIELDS — when absent it is "
            "surfaced WITHOUT a safe_default (the guard forbids one)."
        ),
    )
    task_generator: _TaskGenerator | None = Field(
        default=None,
        description=(
            "The sweep recipe. REQUIRED_CALLER_FIELDS — the framework cannot "
            "invent it. Absent (and no tasks.py) → surfaced WITHOUT a safe_default."
        ),
    )
    tasks_py_present: bool = Field(
        default=False,
        description=(
            "Whether .hpc/tasks.py already exists. When true, an absent "
            "task_generator is NOT an ambiguity (the hand-written tasks.py path)."
        ),
    )

    # ── Step 3: entry point.
    entry_point_resolved: bool = Field(
        default=False,
        description="Whether the entry point is resolved (a @register_run / interview.json on disk).",
    )
    entry_point_candidates: list[str] | None = Field(
        default=None,
        description="Candidate entry-point paths when unresolved (e.g. ['train.py', 'main.py']).",
    )

    # ── Step 3b: uncovered required executor params (#195).
    uncovered_required_params: list[str] = Field(
        default_factory=list,
        description=(
            "Required (no-default) executor params that are NOT swept axes and "
            "lack a fixed_params value. Each must be covered or every task crashes."
        ),
    )
    uncovered_param_defaults: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-param argparse default where one exists (e.g. {'samples': 10000}). "
            "Feeds the uncovered_param safe_default's {param: <default-or-null>} map."
        ),
    )
    executor_run_name: str | None = Field(
        default=None,
        description="The executor's run_name; surfaced in the uncovered_param context block.",
    )

    # ── Step 4: data axis.
    data_axis_resolved: bool = Field(
        default=False,
        description="Whether axes.yaml has an executors.<run_name> entry for the current sha.",
    )

    # ── Step 5: homogeneous axes (cold-start only).
    homogeneous_axes_resolved: bool = Field(
        default=False,
        description="Whether axes.yaml carries homogeneous_axes.",
    )

    # ── Step 6: resources (delegated to resolve-resources).
    experiment_dir: str = Field(
        default=".",
        description="Experiment directory; passed to resolve-resources.",
    )
    profile: str | None = Field(
        default=None, description="Run profile (run_name); resolve-resources prior key."
    )
    cmd_sha: str | None = Field(default=None, description="Optional cmd_sha for the runtime prior.")
    walltime_sec: int | None = Field(default=None, description="Caller override for walltime_sec.")
    gpu_type: str | None = Field(default=None, description="Caller override for gpu_type.")
    partition: str | None = Field(default=None, description="Caller override for partition.")
    user_preferred_partition: str | None = Field(
        default=None, description="Soft partition preference forwarded to recommend-partition."
    )
    partitions: list[PartitionInfo] | None = Field(
        default=None, description="The cluster's partition list (for recommend-partition)."
    )
    mpi_pe: str | None = Field(
        default=None, description="Caller override for the SGE parallel env."
    )
    mpi_ranks: int | None = Field(
        default=None, description="Total MPI ranks; when set, mpi_pe is auto-derived."
    )
    parallel_environments: list[dict[str, Any]] | None = Field(
        default=None, description="The cluster's parallel_environments (for recommend-pe)."
    )


class WalkSubmitAmbiguitiesResult(BaseModel):
    """The ``needs_resolution``-shaped data block: ``{resolved, ambiguities}``.

    ``resolved`` carries every field the walk could fill (caller-supplied
    or auto-resolved). ``ambiguities`` is the list of unresolved fields in
    the SKILL's hand-built shape (``{field, candidates, depends_on,
    safe_default, context?}``); a REQUIRED_CALLER_FIELDS member never
    carries a ``safe_default`` (the Ambiguity guard enforces this). The
    matching envelope ``error_code`` is ``needs_resolution`` when
    ``ambiguities`` is non-empty.
    """

    model_config = ConfigDict(extra="forbid", title="walk-submit-ambiguities output")

    resolved: dict[str, Any] = Field(
        description="Every field the walk resolved (caller-supplied or auto-resolved).",
    )
    ambiguities: list[dict[str, Any]] = Field(
        description="Unresolved fields in the needs_resolution ambiguity shape.",
    )
    provenance: dict[str, Any] = Field(
        default_factory=dict,
        description="How each resolved field was reached (caller / auto-rule / resolve-resources).",
    )
