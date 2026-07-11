"""``walk-submit-ambiguities`` primitive — SKILL Steps 2-6 resolution in CODE.

Surface 2, incident 1b/2. The ``hpc-submit`` SKILL's resolution contract
(walk every step, accumulate ambiguities, never early-return on the first
miss) moves out of LLM prose and into deterministic code. The walk:

* resolves ``cluster`` (caller, else single configured, else ambiguity),
* surfaces ``goal`` / ``task_generator`` as REQUIRED_CALLER_FIELDS
  ambiguities — WITHOUT a ``safe_default`` (the partition guard refuses
  one), so the resolution path *structurally cannot* fabricate a sweep,
* surfaces ``entry_point`` / ``uncovered_param`` / ``data_axis`` /
  ``homogeneous_axes`` ambiguities when unresolved (each an
  AUTO_RESOLVABLE_FIELDS member with a real ``safe_default``),
* REUSES :func:`hpc_agent.ops.resolve_resources.resolve_resources` for
  ``walltime_sec`` / ``gpu_type`` / ``partition`` / ``mpi_pe`` — these
  always auto-resolve (a missing runtime prior is cold-start, not an
  ambiguity), so they go in ``resolved``, never ``ambiguities``.

Returns the ``needs_resolution`` envelope shape ``{resolved,
ambiguities}`` the SKILL consumed.

This is a NEW SIBLING of :mod:`hpc_agent.ops.resolve_submit_inputs`, which
is left byte-for-byte unchanged (the campaign delegates to it at
``meta/campaign/deterministic_resolver.py:313``). The two are different
rings: ``resolve-submit-inputs`` is the post-decision input-resolution
spine (scaffold tasks.py → run_id → spec); this verb is the pre-decision
ambiguity walk that decides which fields still need the caller.
"""

from __future__ import annotations

from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.recommend_partition import PartitionInfo
from hpc_agent._wire.queries.walk_submit_ambiguities import (
    WalkSubmitAmbiguitiesInput,
    WalkSubmitAmbiguitiesResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.resolve_resources import resolve_resources
from hpc_agent.ops.submit.field_partition import Ambiguity

__all__ = ["walk_submit_ambiguities"]


def _materialized_data_axis(experiment_dir: str | None) -> dict[str, Any] | None:
    """The interview's persisted data-axis classification, or ``None``.

    Run-#12 finding 14: the interview writes the caller-declared hint to
    ``interview.json._materialized.entry_point.data_axis`` PRECISELY so no
    consumer re-asks — yet this walk recommended the ``sequential`` fail-safe
    over a recorded ``bounded_halo`` (a ``y`` would have shipped 2700
    BoundedHalo tasks as sequential). Tolerant read: any missing/malformed
    layer returns ``None`` and the fail-safe stands.
    """
    if not experiment_dir:
        return None
    import json
    from pathlib import Path

    try:
        doc = json.loads(
            (Path(experiment_dir) / "interview.json").read_text(encoding="utf-8")
        )
        entry = (doc.get("_materialized") or {}).get("entry_point") or {}
        axis = entry.get("data_axis")
    except Exception:  # noqa: BLE001 — the hint is an optimization, never a gate
        return None
    if isinstance(axis, dict) and isinstance(axis.get("kind"), str):
        return axis
    return None


def _walk_submit_ambiguities_result_post(
    result: WalkSubmitAmbiguitiesResult,
) -> dict[str, Any]:
    """Project the typed result into the envelope ``data`` dict."""
    return result.model_dump(mode="json")


@primitive(
    name="walk-submit-ambiguities",
    verb="query",
    composes=["resolve-resources"],
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Walk hpc-submit Steps 2-6 as deterministic branches: resolve "
            "cluster / entry_point / data_axis / homogeneous_axes / "
            "uncovered_param, reuse resolve-resources for walltime/gpu/"
            "partition/mpi_pe, and accumulate Ambiguity entries (safe_default "
            "ONLY for auto-resolvable fields; goal/task_generator get none). "
            "Returns the needs_resolution envelope {resolved, ambiguities}."
        ),
        spec_arg=True,
        schema_ref=SchemaRef(input="walk_submit_ambiguities"),
        spec_model=WalkSubmitAmbiguitiesInput,
        result_post=_walk_submit_ambiguities_result_post,
        requires_ssh=False,
    ),
    agent_facing=True,
)
def walk_submit_ambiguities(
    *,
    spec: WalkSubmitAmbiguitiesInput,
) -> WalkSubmitAmbiguitiesResult:
    """Run the deterministic submit-input walk; return ``{resolved, ambiguities}``.

    Walks every step regardless of earlier misses (the SKILL's
    "never early-return on the first miss" contract). REQUIRED_CALLER_FIELDS
    (``goal`` / ``task_generator``) are surfaced WITHOUT a ``safe_default``
    — constructing the :class:`Ambiguity` with one would raise, which is the
    point: the resolution path cannot express a fabricated sweep.
    AUTO_RESOLVABLE_FIELDS carry a real ``safe_default``. Resources are
    delegated to :func:`resolve_resources` and always land in ``resolved``.
    """
    resolved: dict[str, Any] = {"experiment_dir": spec.experiment_dir}
    provenance: dict[str, Any] = {}
    ambiguities: list[Ambiguity] = []

    # ── Step 2: cluster.
    if spec.cluster is not None:
        resolved["cluster"] = spec.cluster
        provenance["cluster"] = "caller"
    elif len(spec.configured_clusters) == 1:
        resolved["cluster"] = spec.configured_clusters[0]
        provenance["cluster"] = "single_configured"
    elif spec.configured_clusters:
        ambiguities.append(
            Ambiguity(
                field="cluster",
                candidates=list(spec.configured_clusters),
                depends_on=(),
                safe_default=sorted(spec.configured_clusters)[0],
            )
        )
    else:
        # No cluster and none configured — surface with no candidates and
        # no default (nothing to default to). Still auto-resolvable as a
        # field, so an empty/None default is permitted by the guard.
        ambiguities.append(
            Ambiguity(field="cluster", candidates=None, depends_on=(), safe_default=None)
        )

    # ── REQUIRED_CALLER_FIELDS: goal.
    if spec.goal is not None:
        resolved["goal"] = spec.goal
        provenance["goal"] = "caller"
    else:
        # No safe_default — the Ambiguity guard forbids it on goal.
        ambiguities.append(Ambiguity(field="goal", candidates=None, depends_on=()))

    # ── REQUIRED_CALLER_FIELDS: task_generator. Only an ambiguity when no
    #    hand-written tasks.py exists (the sanctioned hand-written path).
    if spec.task_generator is not None:
        resolved["task_generator"] = spec.task_generator.model_dump(exclude_none=True, mode="json")
        provenance["task_generator"] = "caller"
    elif not spec.tasks_py_present:
        # No safe_default — the framework cannot invent a sweep (incident 1b).
        ambiguities.append(Ambiguity(field="task_generator", candidates=None, depends_on=()))
    else:
        provenance["task_generator"] = "hand_written_tasks_py"

    # ── Step 3: entry point.
    if spec.entry_point_resolved:
        provenance["entry_point"] = "resolved_on_disk"
    else:
        candidates = spec.entry_point_candidates
        safe_default = candidates[0] if candidates else None
        ambiguities.append(
            Ambiguity(
                field="entry_point",
                candidates=candidates,
                depends_on=(),
                safe_default=safe_default,
            )
        )

    # ── Step 3b: uncovered required executor params (#195). dict-shaped
    #    safe_default — {param: <argparse default if any, else None>}. Note
    #    the {param: None} slot is PRESENT, so the guard's `is not None`
    #    correctly treats it as a default (and uncovered_param is
    #    auto-resolvable, so it's allowed).
    if spec.uncovered_required_params:
        default_map = {
            param: spec.uncovered_param_defaults.get(param)
            for param in spec.uncovered_required_params
        }
        context: dict[str, Any] = {"required_no_default": list(spec.uncovered_required_params)}
        if spec.executor_run_name is not None:
            context["executor"] = spec.executor_run_name
        ambiguities.append(
            Ambiguity(
                field="uncovered_param",
                candidates=list(spec.uncovered_required_params),
                depends_on=("entry_point",),
                safe_default=default_map,
                context=context,
            )
        )

    # ── Step 4: data axis. depends_on entry_point (the run being classified).
    if spec.data_axis_resolved:
        provenance["data_axis"] = "resolved_on_disk"
    else:
        # The interview's recorded hint outranks the fail-safe (finding 14):
        # the human already declared the classification; recommending
        # ``sequential`` over it invites re-derivation of a settled fact.
        hint = _materialized_data_axis(spec.experiment_dir)
        if hint is not None:
            provenance["data_axis"] = "interview_hint"
            ambiguities.append(
                Ambiguity(
                    field="data_axis",
                    candidates=None,
                    depends_on=("entry_point",),
                    safe_default=hint,
                    context={
                        "source": "interview.json _materialized.entry_point.data_axis"
                    },
                )
            )
        else:
            ambiguities.append(
                Ambiguity(
                    field="data_axis",
                    candidates=None,
                    depends_on=("entry_point",),
                    safe_default={"kind": "sequential"},
                )
            )

    # ── Step 5: homogeneous axes (cold-start only).
    if spec.homogeneous_axes_resolved:
        provenance["homogeneous_axes"] = "resolved_on_disk"
    else:
        ambiguities.append(
            Ambiguity(
                field="homogeneous_axes",
                candidates=None,
                depends_on=(),
                safe_default=[],
            )
        )

    # ── Step 6: resources — REUSE resolve-resources. Always auto-resolves
    #    (a missing runtime prior is cold-start, never an ambiguity), so the
    #    {walltime_sec, gpu_type, partition, mpi_pe} land in `resolved`.
    cluster_for_resources = resolved.get("cluster")
    if cluster_for_resources is not None:
        partitions_payload: list[dict[str, Any]] | None
        if spec.partitions is not None:
            partitions_payload = [
                p.model_dump(mode="json") if isinstance(p, PartitionInfo) else dict(p)
                for p in spec.partitions
            ]
        else:
            partitions_payload = None
        resources = resolve_resources(
            cluster=cluster_for_resources,
            experiment_dir=spec.experiment_dir,
            profile=spec.profile,
            cmd_sha=spec.cmd_sha,
            walltime_sec=spec.walltime_sec,
            gpu_type=spec.gpu_type,
            partition=spec.partition,
            user_preferred_partition=spec.user_preferred_partition,
            partitions=partitions_payload,
            mpi_pe=spec.mpi_pe,
            mpi_ranks=spec.mpi_ranks,
            parallel_environments=spec.parallel_environments,
        )
        for key in ("walltime_sec", "gpu_type", "partition", "mpi_pe"):
            resolved[key] = resources[key]
        provenance["resources"] = resources.get("provenance", {})
    else:
        # No cluster ⇒ resources can't resolve (cluster is the lookup key).
        # The cluster ambiguity above already escalates; record why resources
        # are absent so the caller knows to re-walk after picking a cluster.
        provenance["resources"] = "deferred_no_cluster"

    return WalkSubmitAmbiguitiesResult(
        resolved=resolved,
        ambiguities=[a.to_dict() for a in ambiguities],
        provenance=provenance,
    )
