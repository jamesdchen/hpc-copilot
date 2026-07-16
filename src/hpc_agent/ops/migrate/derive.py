"""Mint a derived enumerated run over a source run's UNDONE cells [SPEC §3 Step B].

The middle third of ``migrate-remainder``: given the census's *undone* task-id set
for a still-live source run, build a **derived** run whose ``task_generator`` is the
``enumerated`` recipe over exactly those undone task-kwargs cells, materialize its
``tasks.py`` **per-run-scoped** (never over the shared singleton), record its
lineage as ``parents=[source_run_id]`` so its ``node_sha`` is DERIVED from the
source sidecar, and compute + persist the cell-ownership map for the eventual
two-parent harvest. This module **actuates nothing** — no SSH, no submit, no
deploy; it produces the derived run's *spec + files + ownership artifact* in
seconds, and the human ``y`` (M-BRIEF) gates everything downstream.

Four hazards this module is built to (SPEC §3.B, §8):

- **The off-by-one guard.** ``items = [source.resolve(i) for i in undone]`` and the
  minted ``InterviewSpec`` carries ``task_count = len(undone)``; the enumerated
  materializer stores ``items`` VERBATIM and this module asserts
  ``total() == task_count`` after loading (the interview.py:370-376 discipline,
  replicated so a mismatch refuses before any downstream deploy).

- **The singleton hazard [LIVE-4].** ``.hpc/tasks.py`` is ONE file per experiment
  (``layout.py:85``) and the source run's cluster-side status reporter reads it over
  SSH (``cluster_status.py:158``). Minting the obvious way OVERWRITES it with the
  684-item list and silently corrupts the still-live source's monitoring. So the
  derived ``tasks.py`` is materialized to a **per-run path**
  (``.hpc/migrate/<derived_run_id>/tasks.py``), the ``.hpc/wrappers/<run_name>.py``
  per-run precedent. Deploy + reporter are NOT yet plumbed for a per-run tasks path,
  so the result also carries an **explicit, disclosed flip-back sequence** and backs
  up the shared singleton — never a silent overwrite. The clean resolution (the
  "run-14 per-run-materialization fix") is carried **GATED / PLAUSIBLE-UNVERIFIED**:
  no such planned unit exists in ``docs/plans/`` at baseline.

- **The lineage identity.** ``parents=[source_run_id]`` → ``resolve_node_sha``
  composes the derived ``node_sha`` from the source sidecar (``runs.py:672``); a
  **missing source sidecar REFUSES** (``runs.py:709-716``). The derived ``cmd_sha``
  differs from the source's (684 items ≠ 900), so this is a distinct identity whose
  lineage is provable — NOT a resume-reattach.

- **The two-parent double-count [LIVE-2].** The ownership map (``ops/migrate/
  ownership.py``) is computed here mechanically from the census and persisted as a
  migrate-scoped artifact, so the harvest counts every raced cell exactly once.

This unit **reads** the census (``ops/migrate/census.py``, M-CENSUS) as a data
contract — it takes the resolved ``undone_ids``/``done_ids`` sets as arguments and
does not import the census module, keeping the two M2 units decoupled.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hpc_agent import errors, load_tasks_module
from hpc_agent._kernel.contract.layout import RepoLayout
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent.ops.memory.interview import _expected_count, _materialize_tasks_py
from hpc_agent.ops.migrate.ownership import (
    OwnershipMap,
    compute_ownership_map,
    persist_ownership_map,
)
from hpc_agent.state.run_sha import compute_cmd_sha
from hpc_agent.state.runs import resolve_node_sha

# Default provenance for a code-driven derivation: the human operator that
# greenlights the migration owns it. M-BRIEF passes the real authorship; this
# keeps the minted InterviewSpec valid when a caller does not.
_DEFAULT_PRODUCED_BY: dict[str, Any] = {"kind": "human", "operator": "migrate-remainder"}


@dataclass(frozen=True)
class FlipBack:
    """The disclosed singleton flip-back sequence [SPEC §3.B.2, §8, LIVE-4].

    ``required`` is True whenever the per-run tasks path is not yet plumbed through
    deploy + reporter (the baseline reality): reaching the target still transits the
    shared ``.hpc/tasks.py`` singleton, so the source's copy must be restored before
    its next reporter read. ``singleton_backup`` is the on-disk backup this module
    wrote so the restore step is executable rather than aspirational.
    """

    required: bool
    reason: str
    sequence: list[str]
    singleton_backup: Path | None
    gated_clean_fix: str
    singleton_untouched_by_derive: bool = True


@dataclass(frozen=True)
class DeriveResult:
    """The derived run's spec + materialized files + ownership artifact.

    Everything a downstream gate (M-BRIEF) needs to render the migration brief and,
    behind the ``y``, drive the reused S2/S3 path over the DERIVED run.
    """

    derived_run_id: str
    source_run_id: str
    target_cluster: str
    parents: list[str]
    cmd_sha: str
    node_sha: str | None
    task_count: int
    tasks_py_path: Path
    shared_tasks_py_path: Path
    ownership_path: Path
    ownership_digest: dict[str, Any]
    interview_spec: dict[str, Any]
    flip_back: FlipBack
    # First/last materialized cells, for the brief's what-moves preview.
    preview: dict[str, Any] = field(default_factory=dict)
    ownership: OwnershipMap | None = None


def _migrate_dir(experiment_dir: Path, derived_run_id: str) -> Path:
    """``.hpc/migrate/<derived_run_id>/`` — the per-run migrate scope; created."""
    d = Path(experiment_dir).resolve() / ".hpc" / "migrate" / derived_run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def derive_enumerated_run(
    experiment_dir: Path,
    *,
    source_run_id: str,
    derived_run_id: str,
    target_cluster: str,
    undone_ids: list[int],
    done_ids: list[int],
    source_tasks_py: Path | None = None,
    produced_by: dict[str, Any] | None = None,
) -> DeriveResult:
    """Mint the derived enumerated run over *undone_ids*; actuate nothing.

    Parameters
    ----------
    undone_ids, done_ids
        The census partition of the source's ``range(total_tasks)`` — *undone*
        moves to the derived run, *done* stays under the source. Passed as a data
        contract (M-CENSUS produces them); this module does not re-census.
    source_tasks_py
        Override for the source's materialized ``tasks.py``; defaults to the shared
        ``.hpc/tasks.py`` singleton the source run itself uses.

    Refusals (guards that CAN fire):

    - empty *undone_ids* → nothing to migrate (route to plain aggregate, §3.A.4);
    - source ``tasks.py`` missing / unloadable → surfaced, not treated as all-undone;
    - the recipe count ≠ ``task_count``, or ``total() != task_count`` after
      materialization → the off-by-one guard (interview.py:370-376);
    - a missing source sidecar → ``resolve_node_sha`` REFUSES (runs.py:709-716);
    - a non-partition census → ``compute_ownership_map`` REFUSES (ownership.py).
    """
    layout = RepoLayout(Path(experiment_dir))
    shared_tasks = layout.tasks  # .hpc/tasks.py — the singleton, never overwritten
    src_tasks_path = Path(source_tasks_py) if source_tasks_py is not None else shared_tasks

    # ── source task set ──────────────────────────────────────────────────────
    if not src_tasks_path.is_file():
        raise errors.SpecInvalid(
            f"source tasks.py not found at {src_tasks_path}; cannot resolve the "
            "undone cells' kwargs — reconcile the source run first (never treat an "
            "absent tasks.py as 'all undone')."
        )
    source_mod = load_tasks_module(src_tasks_path)
    total_tasks = int(source_mod.total())

    undone_sorted = sorted(set(undone_ids))
    if len(undone_sorted) != len(undone_ids):
        raise errors.SpecInvalid("undone_ids contains duplicates — census is not a set")
    if not undone_sorted:
        raise errors.SpecInvalid(
            f"source run {source_run_id!r} has no undone cells — nothing to migrate; "
            "route to plain aggregate instead of minting an empty derived run."
        )

    # Compute the ownership map FIRST — it is the partition validator (disjoint,
    # exactly-once over range(total_tasks)); a corrupt census refuses here before
    # any file is written.
    ownership = compute_ownership_map(
        total=total_tasks,
        undone_ids=undone_sorted,
        done_ids=sorted(set(done_ids)),
        source_run_id=source_run_id,
        derived_run_id=derived_run_id,
    )

    # ── mint the enumerated InterviewSpec [_wire/actions/interview.py:76] ─────
    # items VERBATIM — source.resolve(i) already merges the source's _INJECT
    # constants into each cell, so the derived resolve(j) reproduces the exact
    # kwargs. No further inject on the derived side.
    items = [source_mod.resolve(i) for i in undone_sorted]
    for pos, kwargs in enumerate(items):
        if not isinstance(kwargs, dict):
            raise errors.SpecInvalid(
                f"source tasks.resolve({undone_sorted[pos]}) returned "
                f"{type(kwargs).__name__}, not a dict — cannot enumerate a non-dict "
                "cell into the derived run."
            )
    task_count = len(items)
    generator = {"kind": "enumerated", "params": {"items": items}}

    spec = InterviewSpec(
        goal=f"remainder migration of {source_run_id} to {target_cluster}",
        task_count=task_count,
        produced_by=produced_by or _DEFAULT_PRODUCED_BY,  # type: ignore[arg-type]
        task_generator=generator,  # type: ignore[arg-type]
        notes=(
            f"derived from {source_run_id}: the {task_count} undone cells "
            f"(of {total_tasks}) migrated to {target_cluster}"
        ),
    )
    # The pre-materialization off-by-one cross-check (interview.py:366-374).
    expected = _expected_count(generator)
    if expected != spec.task_count:
        raise errors.SpecInvalid(
            f"enumerated recipe would produce {expected} tasks but task_count = "
            f"{spec.task_count}; recipe and stated count disagree (refusing to "
            "materialize the derived tasks.py)."
        )

    # ── per-run-scoped materialization [LIVE-4] — NEVER the singleton ─────────
    migrate_dir = _migrate_dir(experiment_dir, derived_run_id)
    derived_tasks = migrate_dir / "tasks.py"
    _materialize_tasks_py(generator, derived_tasks, inject_kwargs=None)

    derived_mod = load_tasks_module(derived_tasks)
    derived_total = int(derived_mod.total())
    if derived_total != spec.task_count:
        # The post-materialization guard (interview.py:370-376) — must never fire
        # for the deterministic enumerated recipe, but a corrupt write is caught
        # here rather than as a wrong task set on the cluster.
        raise errors.SpecInvalid(
            f"derived tasks.total() = {derived_total} but task_count = "
            f"{spec.task_count}; the materialized derived tasks.py disagrees with "
            "the recipe count."
        )
    cmd_sha = compute_cmd_sha(derived_mod)

    # ── lineage identity [SPEC §3.B.3] — REFUSES on a missing source sidecar ──
    node_sha = resolve_node_sha(
        Path(experiment_dir),
        cmd_sha=cmd_sha,
        parent_run_ids=[source_run_id],
    )

    # ── ownership artifact (migrate-scoped, NOT a sidecar write, §9) ──────────
    ownership_path = persist_ownership_map(experiment_dir, ownership)

    # ── the disclosed flip-back: back up the shared singleton, executably ─────
    singleton_backup: Path | None = None
    if shared_tasks.is_file():
        singleton_backup = migrate_dir / "source_tasks.py.backup"
        shutil.copyfile(shared_tasks, singleton_backup)
    flip_back = FlipBack(
        required=True,
        reason=(
            "deploy and the cluster status reporter are hard-wired to the shared "
            ".hpc/tasks.py singleton (resolve_submit_inputs.py:351, "
            "cluster_status.py:158); the derived per-run tasks.py cannot yet reach "
            "the target without transiting the singleton, which the still-live "
            "source run's reporter reads."
        ),
        sequence=[
            f"materialized derived tasks.py at {derived_tasks} (done — singleton untouched)",
            (
                f"backed up the source's shared .hpc/tasks.py to {singleton_backup} (done)"
                if singleton_backup is not None
                else "no shared .hpc/tasks.py present to back up"
            ),
            "before deploy: copy the derived tasks.py OVER .hpc/tasks.py",
            "deploy the derived run to the target",
            "RESTORE the source's .hpc/tasks.py from the backup BEFORE the source's "
            "next reporter read",
        ],
        singleton_backup=singleton_backup,
        gated_clean_fix=(
            "run-14 per-run-materialization: thread .hpc/migrate/<rid>/tasks.py "
            "through deploy + reporter so no singleton transit is needed — GATED / "
            "PLAUSIBLE-UNVERIFIED, no planned unit found in docs/plans/ at baseline."
        ),
    )

    preview = {
        "first_undone_cell_id": undone_sorted[0],
        "first": derived_mod.resolve(0),
        "last_undone_cell_id": undone_sorted[-1],
        "last": derived_mod.resolve(task_count - 1),
    }

    return DeriveResult(
        derived_run_id=derived_run_id,
        source_run_id=source_run_id,
        target_cluster=target_cluster,
        parents=[source_run_id],
        cmd_sha=cmd_sha,
        node_sha=node_sha,
        task_count=task_count,
        tasks_py_path=derived_tasks,
        shared_tasks_py_path=shared_tasks,
        ownership_path=ownership_path,
        ownership_digest=ownership.digest(),
        interview_spec=spec.model_dump(mode="json"),
        flip_back=flip_back,
        preview=preview,
        ownership=ownership,
    )
