"""``aggregate-stream`` — a partial-but-honest aggregate over the complete arms [SPEC §1, S-STREAM].

The streaming counterpart to the all-or-nothing final harvest. Given ONE run or a
set of parent run_ids, it:

* **censuses per-arm completeness** (:mod:`hpc_agent.ops.aggregate.arm_census`) —
  which arms have every task announced ``.complete`` right now;
* **reduces ONLY the complete arms** through the deterministic reducer
  (``reduce_metrics`` per complete arm, or ``migrate.harvest.multi_parent_reduce``
  when a persisted ownership map names a source+derived pair) — every emitted
  number is reducer-computed, never the LLM;
* **emits a partial ``metrics_aggregate.json``** carrying ``arms_complete`` plus
  an ``arms_pending:[{arm, tasks_done, tasks_expected, owner_run_id}]`` disclosure
  block — the never-silent-cap rule (SPEC §4);
* **refines monotonically** — each call bumps ``snapshot_seq`` and reports
  ``newly_complete`` (this call's complete set minus the prior snapshot's); an arm
  that was complete before and is not now is disclosed as ``arms_regressed``,
  never masked.

The verb **actuates nothing** — no submit, no kill, no journal terminal, no
greenlight. It does bounded reads (the ``ls`` census per parent + a summary-only
mirror pull, KB not GB) and overwrites one local snapshot file. Re-callable with
no state mutation beyond that file: this mechanizes the run-14 manual 40→44-arm
progressive table with ``xgb/vol_demand`` disclosed PENDING (SPEC motivating
artifacts), instead of the operator concatenating ``_aggregated`` envelopes by
hand (the run-13 finding-14 operator-bypass this closes).

v1 streams WAVE-ALIGNED runs only (an arm = a whole wave); a non-wave-aligned run
REFUSES in the census (SPEC §8). The task→arm join equals the reducer's row
grouping by construction (wave = bucket = arm in the bucket-major [LIVE-1] tiling
the live reducers use), so a mis-join refuses rather than emitting a wrong-``n``
arm.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.stream_aggregate import (
    StreamAggregateInput,
    StreamAggregateResult,
    StreamArmPending,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_metrics
from hpc_agent.infra.time import utcnow_iso
from hpc_agent.ops.aggregate.arm_census import ArmCensus, census_arms

if TYPE_CHECKING:
    from hpc_agent.ops.aggregate.arm_census import ArmCompleteness
    from hpc_agent.state.run_record import RunRecord

__all__ = ["stream_aggregate"]

#: The canonical partial-aggregate filename — the SAME basename the final harvest
#: writes (``aggregate_flow.py`` convention) so a consumer reads either identically.
_SNAPSHOT_FILENAME = "metrics_aggregate.json"


def _results_subdir_for(record: RunRecord, sidecar: dict[str, Any]) -> str:
    """The run's own results subtree — the static prefix of ``result_dir_template``.

    Mirrors ``aggregate_flow._scoped_results_subdir``: scoping the pull to this
    run's prefix keeps the summary-only mirror KB-scale (finding 19). Falls back
    to ``results`` when no template is declared.
    """
    template = getattr(record, "result_dir_template", None) or sidecar.get("result_dir_template")
    if not (isinstance(template, str) and template):
        return "results"
    head = template.split("{", 1)[0]
    scoped = head.rsplit("/", 1)[0] if "{" in template else head.rstrip("/")
    return scoped or "results"


def _scan_mirror(mirror: Path, summary_name: str) -> dict[int, Path]:
    """Map each task id → the result dir under *mirror* carrying *summary_name*.

    The trailing integer run in a per-task dir NAME is the task id (the
    ``result_dir_template`` leaf ``task_<id>``), mirroring
    ``migrate.harvest._task_id_from_dir_name`` — kept a local copy rather than a
    cross-package private import (the W2 boundary lint). A PATH-shaped summary
    artifact (``sub/metrics.json``) resolves the task dir by stripping ALL of the
    artifact's components (the ``harvest._mirror_task_dirs`` precedent).
    """
    import re

    task_re = re.compile(r"\d+(?!.*\d)")
    depth = len(PurePosixPath(summary_name).parts)
    out: dict[int, Path] = {}
    if not mirror.is_dir():
        return out
    for match in mirror.rglob(summary_name):
        if not match.is_file():
            continue
        tdir = match
        for _ in range(depth):
            tdir = tdir.parent
        m = task_re.search(tdir.name)
        if m is None:
            continue
        out.setdefault(int(m.group(0)), tdir)
    return out


def _snapshot_key(parents: list[str]) -> str:
    """A stable, filesystem-safe snapshot dir key for *parents*.

    A single parent keys on the run_id itself (so the snapshot lands at the
    canonical ``_aggregated/<run_id>/`` location a later final harvest / verify
    reads). A multi-leg set keys on a short sha of the sorted parents, so every
    re-call of the same set refines the SAME snapshot (monotonic seq).
    """
    if len(parents) == 1:
        return parents[0]
    digest = hashlib.sha1("\x00".join(sorted(parents)).encode("utf-8")).hexdigest()[:12]
    return f"stream-{digest}"


def _read_prior_snapshot(path: Path) -> tuple[int, list[str]]:
    """Return ``(prior_seq, prior_complete_arms)`` from an existing snapshot, or ``(0, [])``.

    Best-effort: an absent / unreadable / shape-drifted snapshot reads as "no
    prior" (seq 0) — the monotonic sequence just restarts, never crashes a
    re-call.
    """
    import json

    if not path.is_file():
        return 0, []
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        prov = obj.get("provenance") or {}
        seq = int(prov.get("snapshot_seq") or 0)
        complete = [str(a) for a in (prov.get("arms_complete") or [])]
        return seq, complete
    except (OSError, ValueError, TypeError):
        return 0, []


def _census_parent(
    experiment_dir: Path,
    run_id: str,
    *,
    census_fn: Callable[..., ArmCensus],
) -> tuple[RunRecord, dict[str, Any], ArmCensus]:
    """Load a parent's record + sidecar and census its arms (owner = the parent)."""
    from hpc_agent.infra.clusters import resolve_ssh_target
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for run_id={run_id!r}")
    try:
        sidecar = read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError):
        sidecar = {}
    census = census_fn(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        run_id=run_id,
        wave_map=sidecar.get("wave_map") or None,
        total_tasks=int(sidecar.get("task_count") or getattr(record, "total_tasks", 0) or 0),
        owner_run_id=run_id,
    )
    return record, sidecar, census


def _pull_parent_mirror(
    record: RunRecord,
    sidecar: dict[str, Any],
    *,
    mirror: Path,
    summary_name: str,
    pull_fn: Callable[..., Any],
) -> None:
    """Pull ONLY *summary_name* sidecars for a parent into *mirror* (the KB lever).

    A non-zero pull REFUSES — there is no deterministic numeric input to reduce,
    and fabricating an aggregate over a partial mirror is exactly the failure this
    framework exists to prevent (the ``migrate.harvest`` pull-refuse precedent).
    """
    from hpc_agent.infra.clusters import resolve_ssh_target

    mirror.mkdir(parents=True, exist_ok=True)
    result = pull_fn(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        remote_subdir=_results_subdir_for(record, sidecar),
        local_dir=str(mirror),
        include=[summary_name],
    )
    rc = getattr(result, "returncode", 0)
    if rc != 0:
        stderr_tail = (getattr(result, "stderr", "") or "").strip()
        raise errors.RemoteCommandFailed(
            f"aggregate-stream pull of {summary_name!r} for {record.run_id!r} failed "
            f"(exit {rc}); refusing to reduce over a partial mirror. stderr: {stderr_tail[:300]}"
        )


def _builtin_reduce(
    per_parent: list[tuple[RunRecord, dict[str, Any], ArmCensus]],
    *,
    experiment_dir: Path,
    snapshot_dir: Path,
    pull_fn: Callable[..., Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-complete-arm reduce over each parent's summary mirror (the default path).

    Returns ``(aggregated_metrics, per_arm_metrics)``: the overall weighted-mean
    over EVERY complete arm's task dirs, plus one reducer row per complete arm
    keyed ``owner:arm`` (the progressive table). Each parent's cells are distinct
    (two independent legs' ``task_0`` are DIFFERENT experiments — no cross-parent
    dedup), so the rows concatenate.
    """
    from hpc_agent.state.runs import resolved_summary_artifact

    all_selected: list[str] = []
    per_arm: dict[str, Any] = {}
    for record, sidecar, census in per_parent:
        summary_name = resolved_summary_artifact(sidecar)
        mirror = snapshot_dir / "_stream_mirror" / record.run_id
        _pull_parent_mirror(
            record, sidecar, mirror=mirror, summary_name=summary_name, pull_fn=pull_fn
        )
        dirs_by_task = _scan_mirror(mirror, summary_name)
        for arm in census.complete_arms:
            arm_dirs = [str(dirs_by_task[t]) for t in arm.task_ids if t in dirs_by_task]
            per_arm[f"{arm.owner_run_id}:{arm.arm}"] = reduce_metrics(
                arm_dirs, filename=summary_name
            )
            all_selected.extend(arm_dirs)
    # Read the FIRST parent's summary name for the overall reduce (all legs of one
    # experiment share it — the migrate DEFAULT_SUMMARY_NAME contract).
    overall_summary = resolved_summary_artifact(per_parent[0][1])
    aggregated = reduce_metrics(all_selected, filename=overall_summary)
    return aggregated, per_arm


def _ownership_reduce(
    per_parent: list[tuple[RunRecord, dict[str, Any], ArmCensus]],
    *,
    experiment_dir: Path,
    snapshot_dir: Path,
    pull_fn: Callable[..., Any],
    ownership: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Two-parent ownership-aware reduce (the migrate source+derived race, LIVE-2).

    Delegates to ``migrate.harvest.multi_parent_reduce`` so a cell present under
    BOTH run_ids (the qdel race window) is counted ONCE — the ownership map picks
    its single owner. Taken only when a persisted ownership map names this exact
    source+derived pair; the census still discloses any pending arm on the
    envelope. Returns ``(aggregated_metrics, per_arm_metrics, dedup_accounting)``.
    """
    from hpc_agent.ops.migrate.harvest import DEFAULT_SUMMARY_NAME, multi_parent_reduce
    from hpc_agent.state.runs import resolved_summary_artifact

    by_run = {rec.run_id: (rec, sc) for rec, sc, _ in per_parent}
    source_rec, source_sc = by_run[ownership.source_run_id]
    derived_rec, derived_sc = by_run[ownership.derived_run_id]
    summary_name = resolved_summary_artifact(source_sc) or DEFAULT_SUMMARY_NAME

    source_mirror = snapshot_dir / "_stream_mirror" / source_rec.run_id
    derived_mirror = snapshot_dir / "_stream_mirror" / derived_rec.run_id
    _pull_parent_mirror(
        source_rec, source_sc, mirror=source_mirror, summary_name=summary_name, pull_fn=pull_fn
    )
    _pull_parent_mirror(
        derived_rec, derived_sc, mirror=derived_mirror, summary_name=summary_name, pull_fn=pull_fn
    )
    res = multi_parent_reduce(
        source_mirror=source_mirror,
        derived_mirror=derived_mirror,
        ownership=ownership,
        summary_name=summary_name,
    )
    dedup = {
        "cells_counted": res.cells_counted,
        "source_cells_counted": res.source_cells_counted,
        "derived_cells_counted": res.derived_cells_counted,
        "dropped_raced": res.dropped_raced,
        "excluded_canary_dirs": res.excluded_canary_dirs,
    }
    return res.aggregated, {}, dedup


def _load_ownership_for_pair(experiment_dir: Path, parents: list[str]) -> Any | None:
    """Return the persisted ownership map iff *parents* are a source+derived pair.

    A migrated run persists ``.hpc/migrate/<derived_run_id>/ownership.json`` naming
    its ``source_run_id``. Streaming that pair must dedupe the raced cell, so try
    each parent as the derived run and accept the map only when BOTH its
    source/derived ids are exactly the given pair. Any other parent set (two
    independent legs) has no such map → ``None`` → the builtin path.
    """
    if len(parents) != 2:
        return None
    from hpc_agent.ops.migrate.ownership import load_ownership_map

    pair = set(parents)
    for candidate_derived in parents:
        try:
            om = load_ownership_map(experiment_dir, candidate_derived)
        except (FileNotFoundError, errors.SpecInvalid):
            continue
        if {om.source_run_id, om.derived_run_id} == pair:
            return om
    return None


def _write_snapshot(
    snapshot_path: Path,
    *,
    aggregated: dict[str, Any],
    per_arm: dict[str, Any],
    provenance: dict[str, Any],
) -> None:
    """Write the partial ``metrics_aggregate.json`` — the final-harvest shape plus
    the additive streaming provenance block (SPEC §3.D)."""
    from hpc_agent.infra.io import atomic_write_json

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        snapshot_path,
        {
            "aggregated_metrics": aggregated,
            "per_arm_metrics": per_arm,
            "provenance": provenance,
        },
    )


@primitive(
    name="aggregate-stream",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<parents> (per-arm announce census)"),
        SideEffect("sync-pull", "<remote_path>/results/**/<summary> → local mirror"),
    ],
    error_codes=[
        errors.SpecInvalid,
        errors.PreconditionFailed,
        errors.RemoteCommandFailed,
    ],
    idempotent=True,
    cli=CliShape(
        help=(
            "Emit a partial-but-honest aggregate over the arms complete NOW — one "
            "run or parents=[...]. Reduces only complete arms through the run's own "
            "deterministic reducer; discloses every pending arm by name; refines "
            "monotonically across calls. Actuates nothing (a re-callable query)."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        requires_ssh=True,
        spec_model=StreamAggregateInput,
        schema_ref=SchemaRef(input="stream_aggregate"),
    ),
    agent_facing=True,
)
def stream_aggregate(
    experiment_dir: Path,
    *,
    spec: StreamAggregateInput,
    _census_fn: Callable[..., ArmCensus] | None = None,
    _pull_fn: Callable[..., Any] | None = None,
) -> StreamAggregateResult:
    """Stream the current best table over the complete arms [SPEC §1].

    Parameters
    ----------
    spec
        Exactly one of ``run_id`` (single run) or ``parents`` (multi-leg).
    _census_fn / _pull_fn
        Injection seams for testing (default the real ``census_arms`` / the
        summary-only ``rsync_pull``). They keep the fire-path tests off ssh.

    Refusals (guards that CAN fire):

    - a non-wave-aligned parent → :class:`errors.SpecInvalid` (census; final
      harvest only, SPEC §8);
    - a parent with no per-task census → :class:`errors.PreconditionFailed`
      (census Δ1);
    - **zero arms complete across all parents** → :class:`errors.PreconditionFailed`
      — a clean "still draining, N arms pending by name" refusal, never a
      fabricated empty table (SPEC §3.A.5);
    - a mirror pull failure → :class:`errors.RemoteCommandFailed`.
    """
    experiment_dir = Path(experiment_dir)
    census_fn = _census_fn if _census_fn is not None else census_arms
    parents: list[str] = [spec.run_id] if spec.run_id else list(spec.parents or [])

    # ── census every parent (refuses a non-aligned / absent-census parent) ──────
    per_parent = [_census_parent(experiment_dir, rid, census_fn=census_fn) for rid in parents]

    complete_all: list[ArmCompleteness] = [arm for _, _, c in per_parent for arm in c.complete_arms]
    pending_all: list[ArmCompleteness] = [arm for _, _, c in per_parent for arm in c.pending_arms]

    # Zero complete → refuse with the pending arms named (never a fabricated table).
    if not complete_all:
        pending_names = ", ".join(f"{a.owner_run_id}:{a.arm}" for a in pending_all)
        raise errors.PreconditionFailed(
            f"aggregate-stream: zero arms complete across {parents!r} — nothing to "
            f"emit yet. {len(pending_all)} arm(s) still draining by name: "
            f"[{pending_names}]. Re-call as buckets land."
        )

    # ── snapshot bookkeeping (monotonic seq + delta since prior) ────────────────
    key = _snapshot_key(parents)
    snapshot_dir = (
        Path(spec.output_dir) if spec.output_dir else (experiment_dir / "_aggregated" / key)
    )
    snapshot_path = snapshot_dir / _SNAPSHOT_FILENAME
    prior_seq, prior_complete = _read_prior_snapshot(snapshot_path)

    complete_names = sorted(f"{a.owner_run_id}:{a.arm}" for a in complete_all)
    prior_set = set(prior_complete)
    newly_complete = [n for n in complete_names if n not in prior_set]
    arms_regressed = sorted(prior_set - set(complete_names))

    if _pull_fn is not None:
        pull_fn = _pull_fn
    else:
        from hpc_agent.infra.transport import rsync_pull as _rsync_pull

        pull_fn = _rsync_pull

    # ── reduce the complete arms through the deterministic reducer ──────────────
    ownership = _load_ownership_for_pair(experiment_dir, parents)
    ownership_dedup: dict[str, Any] | None = None
    if ownership is not None:
        aggregated, per_arm, ownership_dedup = _ownership_reduce(
            per_parent,
            experiment_dir=experiment_dir,
            snapshot_dir=snapshot_dir,
            pull_fn=pull_fn,
            ownership=ownership,
        )
        reduce_path = "ownership"
    else:
        aggregated, per_arm = _builtin_reduce(
            per_parent,
            experiment_dir=experiment_dir,
            snapshot_dir=snapshot_dir,
            pull_fn=pull_fn,
        )
        reduce_path = "builtin"

    # ── disclose census disagreements per parent (never masked) ─────────────────
    disagreement: dict[str, Any] | None = None
    per_parent_disagree = {
        c.run_id: c.disagreement for _, _, c in per_parent if c.disagreement is not None
    }
    if per_parent_disagree:
        disagreement = per_parent_disagree

    reduced_at = utcnow_iso()
    snapshot_seq = prior_seq + 1
    arms_pending_rows = [StreamArmPending(**a.pending_digest()) for a in pending_all]

    provenance = {
        "source": "stream",
        "reduced_at": reduced_at,
        "parents": parents,
        "arms_complete": complete_names,
        "arms_pending": [r.model_dump() for r in arms_pending_rows],
        "snapshot_seq": snapshot_seq,
        "superseded": prior_seq or None,
        "newly_complete": newly_complete,
        "arms_regressed": arms_regressed,
        "reduce_path": reduce_path,
        "ownership_dedup": ownership_dedup,
        "disagreement": disagreement,
    }
    _write_snapshot(snapshot_path, aggregated=aggregated, per_arm=per_arm, provenance=provenance)

    return StreamAggregateResult(
        ok=True,
        parents=parents,
        snapshot_seq=snapshot_seq,
        superseded=prior_seq or None,
        arms_complete=complete_names,
        arms_pending=arms_pending_rows,
        newly_complete=newly_complete,
        arms_regressed=arms_regressed,
        aggregated_metrics=aggregated,
        per_arm_metrics=per_arm,
        output_path_local=str(snapshot_path),
        reduce_path=reduce_path,
        ownership_dedup=ownership_dedup,
        disagreement=disagreement,
        reduced_at=reduced_at,
    )
