"""``migrate-remainder`` — move a run's UNDONE tasks to another cluster, gated.

USER DIRECTIVE (2026-07-16): *"migrate-remainder must be possible"*. The live case
is xgb ``causal_tune_tree_xgb-0b5ef197`` with 216/900 done on hoffman2 — the
question is to move the 684 remaining to carc without re-running the 216 already
finished, and without losing the source's queue position if the migration fails.
Done by hand tonight that is "an hour of careful surgery"; this verb composes the
already-built pieces into ONE journaled decision.

**The composition (SPEC §3, this is the M3 unit M-BRIEF).** Given ``{source_run_id,
target_cluster}`` the verb:

1. **censuses** the source's per-task done-set from its cluster-side announce
   markers (``ops/monitor/announce.read_announced_task_ids`` — the id-carrying
   sibling of the counts reader) and computes ``undone = range(total) − done``. A
   missing per-task census REFUSES ("no per-task census present") — absence is never
   read as "all undone" (that would re-run every finished task);
2. **mints** a derived enumerated run over exactly the undone cells
   (``ops/migrate/derive.derive_enumerated_run``): a per-run-scoped ``tasks.py``
   (NEVER the shared singleton the source's reporter reads — the LIVE-4 hazard),
   ``parents=[source]`` so its ``node_sha`` records the lineage, and a cell-ownership
   map for the eventual two-parent harvest;
3. **estimates** the migration's footprint over the undone count from the
   source-observed canary runtime (``ops/migrate/cost.estimate_migration_cost``);
4. **returns** a persisted migration brief (``needs_decision=True``,
   ``next_block=submit-s2``, ``resolved["next_block"]="submit-s2"`` stamped so
   ``assert_greenlit_target`` reads it) the human ``y``s through the existing
   ``append-decision`` path.

**It actuates nothing itself and returns in SECONDS** (the ``retarget_run.py``
MCP-safe contract): the census read is best-effort, no canary runs inline, and the
source array is NOT killed here. The ``y`` greenlights **submit-s2** to stage +
canary the DERIVED run on the target; only when that canary is verified GREEN does
the migration proceed to kill the source remainder (M-KILL, range-aware) and launch
the derived main array. **This inverts ``retarget-run``'s supersede-first order**
[SPEC §3 Step E, LIVE-3]: retarget re-runs the WHOLE grid so supersede-first is
safe, but a remainder-migration must not sacrifice partial progress — a failed
migration leaves the source's queue position intact on both clusters.

**Load-bearing guards** (each is a guard that CAN fire, the engineering-principles
rule):

* a missing source sidecar REFUSES (there is no run to migrate);
* a same-cluster / clusterless ``target_cluster`` REFUSES — nothing to migrate;
  a same-cluster resource change is ``revise-resolved``, a cluster MOVE of a whole
  fresh grid is ``retarget-run``;
* a source with no per-task census REFUSES (reconcile the source first);
* a source with an EMPTY undone set REFUSES — nothing to migrate, route to
  ``aggregate`` / harvest the done run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.workflows.migrate_remainder import (
    MigrateRemainderInput,
    MigrateRemainderResult,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.ops.migrate.census import MigrationCensus, census_remainder
from hpc_agent.ops.migrate.cost import estimate_migration_cost
from hpc_agent.ops.migrate.derive import derive_enumerated_run

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["migrate_remainder"]

# The block id the persisted brief + the greenlight are scoped under. NOT a gated
# block itself (submit-s2 is the reused gate); this is the decision-point label the
# brief journal keys on so the rule-9 provenance gate can diff the y.
_MIGRATE_BLOCK = "migrate-remainder"


def _target_uses_global_array_index(target_cluster: str) -> bool:
    """The TARGET backend's ``uses_global_array_index`` capability (backends:187).

    Resolves the target cluster's scheduler from ``clusters.yaml`` → the backend →
    its ``uses_global_array_index`` flag, which ``census_remainder`` uses to REFUSE
    an index-bounded target that cannot express a non-contiguous arbitrary
    remainder. A cluster with no resolvable scheduler / backend degrades to ``True``
    (global-index, the SLURM default) — fail-OPEN to the downstream submit-s2 stage
    rather than spuriously refusing a migration the target could in fact express.
    """
    from hpc_agent.infra.backends import get_backend
    from hpc_agent.infra.clusters import load_clusters_config

    try:
        scheduler = str((load_clusters_config().get(target_cluster) or {}).get("scheduler") or "")
        if not scheduler:
            return True
        return bool(get_backend(scheduler).uses_global_array_index)
    except Exception:  # noqa: BLE001 — capability probe; a resolution fault fails open
        return True


def _census(
    experiment_dir: Path,
    *,
    source_run_id: str,
    total_tasks: int,
    target_cluster: str,
    wave_map: Mapping[str, Any] | None,
) -> MigrationCensus:
    """Census the source done-set + partition the remainder [SPEC §3 Step A].

    Resolves the source's live ssh target + remote path from its run record, the
    target backend's global-index capability, and delegates to the canonical
    ``census_remainder`` (M-CENSUS) — which reads the announce markers, computes
    ``undone = range(total) − done``, aligns to whole waves [LIVE-1], and REFUSES an
    absent census / an empty remainder / an index-bounded target that cannot express
    the range. A module-level seam so tests drive the composition without SSH.

    A missing source run record REFUSES (cannot resolve the ssh target).
    """
    from hpc_agent.infra.clusters import resolve_ssh_target
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, source_run_id)
    if record is None:
        raise errors.SpecInvalid(
            f"migrate-remainder: no run record for source_run_id={source_run_id!r} — "
            "cannot resolve the cluster / ssh target to census the done-set. Submit "
            "or reconcile the source run first."
        )
    return census_remainder(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        source_run_id=source_run_id,
        total_tasks=total_tasks,
        target_uses_global_array_index=_target_uses_global_array_index(target_cluster),
        wave_map=wave_map,
    )


def _derived_run_id(source_run_id: str, target_cluster: str) -> str:
    """Code-derive the derived run id — the LLM never authors run identity.

    ``<source_run_name>-migrate-<target_cluster>`` where the source run_name is the
    id minus its cmd_sha suffix (the ``retarget-run`` ``<old_run_name>-<cluster>``
    precedent). Deterministic per (source, target), and distinct from the source id
    so the migrate-scoped artifacts never collide with the source's own state.
    """
    source_run_name = source_run_id.rsplit("-", 1)[0] or source_run_id
    return f"{source_run_name}-migrate-{target_cluster}"


@primitive(
    name="migrate-remainder",
    verb="workflow",
    composes=["submit-s2"],
    side_effects=[
        SideEffect(
            "writes-derived-run",
            "<experiment>/.hpc/migrate/<derived_run_id>/ (the derived tasks.py + "
            "ownership.json artifact); backs up the source's shared .hpc/tasks.py",
        ),
        SideEffect("ssh", "<source-cluster> (best-effort per-task census read; non-blocking)"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.PreconditionFailed,
        errors.ClusterUnknown,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    idempotency_key="source_run_id",
    cli=CliShape(
        help=(
            "Move a run's UNDONE tasks to another cluster as ONE gated verb "
            "(USER DIRECTIVE 2026-07-16). Censuses the source's per-task done-set, "
            "mints a DERIVED enumerated run over exactly the undone cells "
            "(per-run-scoped tasks.py, parents=[source], cell-ownership map), "
            "estimates the footprint over the undone count from the source-observed "
            "canary runtime, and returns a persisted migration brief with "
            "next_block=submit-s2. Returns in SECONDS: actuates NOTHING — the human's "
            "y greenlights submit-s2 to stage & canary the DERIVED run, and the "
            "source remainder is killed ONLY after that canary is verified GREEN "
            "(inverts retarget-run's supersede-first order — a remainder migration "
            "must not sacrifice partial progress). A same-cluster target / missing "
            "census / empty undone set is REFUSED."
        ),
        spec_arg=True,
        spec_model=MigrateRemainderInput,
        experiment_dir_arg=True,
        requires_ssh=True,
        schema_ref=SchemaRef(input="migrate_remainder"),
    ),
    agent_facing=True,
)
def migrate_remainder(
    experiment_dir: Path, *, spec: MigrateRemainderInput
) -> MigrateRemainderResult:
    """Census → derive → cost → persisted gated brief (next_block=submit-s2).

    See the module docstring for the full flow + the guard inventory. Raises
    :class:`errors.SpecInvalid` on a missing source sidecar, a same-cluster /
    clusterless target, a source with no per-task census, or an empty undone set;
    :class:`errors.RemoteCommandFailed` on a transport failure during the census.
    """
    from hpc_agent.state.decision_briefs import append_brief
    from hpc_agent.state.runs import read_run_sidecar

    source_run_id = spec.source_run_id
    target_cluster = spec.target_cluster.strip()

    # ── the source sidecar (task_count / cluster / resources / wave_map) ──────
    try:
        sidecar = read_run_sidecar(experiment_dir, source_run_id)
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(
            f"migrate-remainder: no sidecar for source_run_id={source_run_id!r} — "
            "this verb migrates a RESOLVED, in-flight run (its per-run sidecar "
            "carries the task_count / cluster / resources / wave_map). Submit it "
            "first (submit-s1), then migrate its remainder."
        ) from exc

    source_cluster = str(sidecar.get("cluster") or "").strip()
    total_tasks = int(sidecar.get("task_count") or 0)

    # ── same-cluster / clusterless guard (a guard that CAN fire) ──────────────
    if not target_cluster:
        raise errors.SpecInvalid(
            "migrate-remainder: target_cluster is empty — name the cluster the "
            "undone tasks migrate to."
        )
    if target_cluster == source_cluster:
        raise errors.SpecInvalid(
            f"migrate-remainder: target_cluster ({target_cluster!r}) is the SAME as "
            f"the source's cluster — nothing to MIGRATE. To change resources on the "
            "same cluster use revise-resolved; to re-run the WHOLE grid on a new "
            "cluster use retarget-run. migrate-remainder moves only the UNDONE tasks "
            "to a DIFFERENT cluster."
        )
    if total_tasks < 1:
        raise errors.SpecInvalid(
            f"migrate-remainder: source {source_run_id!r} has task_count={total_tasks} "
            "(< 1) — no task grid to census. Reconcile the source run first."
        )

    # ── census the done-set (M-CENSUS: REFUSES on absent census / empty remainder /
    #    an index-bounded target that can't express the range — never 'all undone') ─
    census = _census(
        experiment_dir,
        source_run_id=source_run_id,
        total_tasks=total_tasks,
        target_cluster=target_cluster,
        wave_map=dict(sidecar.get("wave_map") or {}),
    )
    undone_ids = list(census.undone_ids)
    done_ids = list(census.done_ids)
    undone_range = census.task_range
    whole_waves = [str(w) for w in census.whole_waves] if census.wave_aligned else None

    # ── mint the derived enumerated run (per-run-scoped; actuates nothing) ────
    derived_run_id = _derived_run_id(source_run_id, target_cluster)
    derive = derive_enumerated_run(
        experiment_dir,
        source_run_id=source_run_id,
        derived_run_id=derived_run_id,
        target_cluster=target_cluster,
        undone_ids=undone_ids,
        done_ids=done_ids,
        produced_by=spec.produced_by,
    )

    # ── cost over the UNDONE count from the source-observed runtime ───────────
    cost = estimate_migration_cost(
        experiment_dir,
        source_run_id=source_run_id,
        undone_count=len(undone_ids),
        source_resources=dict(sidecar.get("resources") or {}),
        target_cluster=target_cluster,
    )

    # ── what-DIES: the source remainder (killed ONLY after the canary is green) ─
    from hpc_agent.state.journal import load_run

    source_record = load_run(experiment_dir, source_run_id)
    source_job_ids = list(source_record.job_ids) if source_record is not None else []
    what_dies = {
        "source_run_id": source_run_id,
        "source_cluster": source_cluster,
        "job_ids": source_job_ids,
        "task_range": undone_range,
        "whole_waves": whole_waves,
        # THE ordering invariant [LIVE-3]: this verb kills nothing; the range-kill
        # fires only after the derived canary (submit-s2) is verified GREEN.
        "killed_only_after_derived_canary_green": True,
        "note": (
            "the source remainder is NOT killed by this verb — the range-kill "
            "(M-KILL) fires ONLY after the derived-run canary is verified GREEN "
            "(submit-s2). A failed migration leaves the source's queue position "
            "intact on both clusters (inverts retarget-run's supersede-first order)."
        ),
    }

    # ── census disagreement (surfaced by M-CENSUS's status-reporter cross-check,
    #    never auto-masked) ──────────────────────────────────────────────────────
    census_disagreement = census.disagreement

    # ── the migration brief (mirrors submit-s2 + retarget shape) ──────────────
    resolved: dict[str, Any] = {"next_block": "submit-s2"}
    brief: dict[str, Any] = {
        # run_id + cluster ride the brief so a relay renders from the brief's OWN
        # data: the canonical line is "canary PENDING on <target_cluster>".
        "run_id": derived_run_id,
        "cluster": target_cluster,
        "migrated_from": {"run_id": source_run_id, "cluster": source_cluster},
        "what_moves": {
            "undone_count": len(undone_ids),
            "total_tasks": total_tasks,
            "done_count": len(done_ids),
            "wave_aligned": census.wave_aligned,
            "whole_waves": whole_waves,
            "range_shape": census.range_shape,
            "task_range": undone_range,
        },
        "what_dies": what_dies,
        "est_core_hours": cost.est_core_hours,
        # Unknown-footprint honesty (run #6): the relay reads off the brief dict, so
        # the signal rides here too — never render a defensive 0.0 as "0 core-hours".
        "footprint_unknown": cost.footprint_unknown,
        "cost_estimate": cost.to_brief(),
        "ownership_map": derive.ownership_digest,
        "flip_back": {
            "required": derive.flip_back.required,
            "reason": derive.flip_back.reason,
            "sequence": derive.flip_back.sequence,
            "singleton_backup": (
                str(derive.flip_back.singleton_backup)
                if derive.flip_back.singleton_backup is not None
                else None
            ),
            "gated_clean_fix": derive.flip_back.gated_clean_fix,
        },
        "derived": {
            "run_id": derive.derived_run_id,
            "parents": derive.parents,
            "cmd_sha": derive.cmd_sha,
            "node_sha": derive.node_sha,
            "task_count": derive.task_count,
            "tasks_py_path": str(derive.tasks_py_path),
            "ownership_path": str(derive.ownership_path),
            "preview": derive.preview,
        },
        "census_disagreement": census_disagreement,
        "resolved": resolved,
    }

    # Persist the brief so the rule-9 provenance gate can diff the y
    # (brief_provenance.py:67). CODE persists it here, the moment this verb returns a
    # decision-point Result, exactly as the submit blocks do (_persist_brief seam).
    append_brief(experiment_dir, run_id=derived_run_id, block=_MIGRATE_BLOCK, brief=brief)

    est_phrase = (
        "unknown core-hours (no canary measurement / requested walltime)"
        if cost.footprint_unknown
        else f"{cost.est_core_hours:g} core-hours"
    )
    reason = (
        f"migrate {len(undone_ids)} undone of {total_tasks} tasks from "
        f"{source_run_id!r} ({source_cluster}) to {target_cluster} (est. {est_phrase}); "
        f"derived run {derived_run_id!r} minted (parents=[source]) — canary PENDING. "
        "Greenlight submit-s2 to stage & canary the derived run; the source remainder "
        "is killed ONLY after that canary is GREEN."
    )
    return MigrateRemainderResult(
        stage_reached="migration_pending_canary",
        needs_decision=True,
        reason=reason,
        source_run_id=source_run_id,
        derived_run_id=derived_run_id,
        brief=brief,
        next_block={
            "verb": "submit-s2",
            "why": "migration derived; stage & canary the derived remainder run for review.",
            "spec_hint": {"run_id": derived_run_id},
        },
    )
