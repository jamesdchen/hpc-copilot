"""Two-parent, ownership-aware harvest for a remainder migration [SPEC §3 Step F, §5, LIVE-2].

The final third of ``migrate-remainder``. After the source run keeps its *done*
cells (on the origin cluster) and a derived run computes the *undone* cells (on the
target), the aggregate must pull **both** parents' per-task results into one mirror
and reduce them into a single summary — the union of ``900`` cells for the live
``causal_tune_tree_xgb`` case (``216`` done + ``684`` migrated).

Why this is a NEW module and not an edit to the single-run harvest
(``ops/aggregate_flow.py``, a parked/forbidden seam this wave, SPEC §9): the
single-run path reduces ONE mirror and, for the ``_combiner/`` partials, filters
foreign waves by an exact ``run_id`` match (``execution/mapreduce/reduce/
metrics.py:265``, the F05 filter). That filter is **wrong for two parents** — it
would drop one whole parent. So this module composes the SAME building blocks the
single-run path uses — a per-parent filtered pull (``infra.transport.rsync_pull``)
and the value-keyed weighted-mean reducer (``reduce_metrics``, ``metrics.py:134``)
— but replaces the ``run_id`` selection with the **cell-ownership map**
(``ops/migrate/ownership.py``, M-DERIVE's data contract): for each cell key,
include **exactly** the owner's result dir.

The correctness story (SPEC §5) rests entirely on that selection:

- ``reduce_metrics`` is **value-keyed**: it concatenates result dirs, appends each
  ``metrics.json`` as one entry, and weighted-means by ``n_samples``
  (``metrics.py:97,101-102``). It has **no task-id keying and no cardinality gate**
  — it would happily average an over-count.
- **The break condition is the qdel race window** (SPEC §5.2, LIVE-2): the source
  may finish a cell *after* the census but *before* the range-kill, while the
  derived run also runs it. The SAME logical cell then exists under BOTH run_ids.
  Blind concatenation counts its ``n`` twice.
- **The ownership map is the fix.** Before the reduce, each cell resolves to its
  single owner: a raced cell present under both run_ids is dropped to the owner and
  counted **once** (``metrics.py:101-102`` never double-sums ``n``). The union
  ``total`` and the exactly-once map make the cardinality gate here — and the
  ``range(total)`` invariants downstream (``ops/aggregate/invariants.py:215``,
  ``aggregate_flow.py:731``) — the **safety net**, firing only on genuine overlap
  the ownership map did not resolve, never the primary guard.

Cell-id spaces (the join key subtlety this module encodes):

- The **source** mirror keys each result dir by its **source-global** task id — the
  source run never re-indexed, so ``task_<gid>`` IS the cell key.
- The **derived** mirror re-materializes its ``items`` from ``0`` (M-DERIVE) and
  renders result dirs under its own run_id at that **local** index, so ``task_<j>``
  is the derived-local index. ``ownership.derived_local_index`` maps every
  source-global cell → its derived-local index; this module inverts it to attribute
  each derived result dir back to its source-global cell key.

Canary-family anti-contamination (SPEC §5, ``aggregate_flow.py:722-725`` precedent)
is applied **per parent**: a parent's ``<run_id>-canary`` sibling writes under the
same ``results/`` subtree, so its dir is dropped using that parent's own
``sibling_run_ids`` family before ownership selection — a source canary never
contaminates and neither does a derived one.

This module **actuates nothing destructive** and reduces in **seconds** over
already-mirrored files: :func:`multi_parent_reduce` is a pure function over two
local mirrors, and :func:`multi_parent_harvest` only pulls (read-only) then reduces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_metrics
from hpc_agent.ops.monitor.reconcile import sibling_run_ids

if TYPE_CHECKING:
    from collections.abc import Callable

    from hpc_agent.ops.migrate.ownership import OwnershipMap

__all__ = [
    "MultiParentHarvestResult",
    "ParentPull",
    "multi_parent_harvest",
    "multi_parent_reduce",
    "select_owned_dirs",
]

#: The default per-task summary filename (the ``reduce_metrics`` historical
#: default). Callers thread the run's declared ``summary_artifact`` (F-J) so a
#: non-default emitter is read; both parents MUST share it (they are the same
#: experiment) — a mismatch is a caller bug, not a shape this module guesses at.
DEFAULT_SUMMARY_NAME = "metrics.json"

#: The LAST run of digits in a per-task result-dir NAME — the leaf ``task_<id>``
#: / ``task-<id>`` the ``result_dir_template`` renders. Like the single-run
#: harvest's ``_TASK_DIR_RE`` (``aggregate_flow.py:796``): keyed on the leaf name
#: only, so a run_id with digits elsewhere in the path never leaks into the id.
_TASK_DIR_RE = re.compile(r"\d+(?!.*\d)")


def _task_id_from_dir_name(result_dir: Path) -> int | None:
    """Integer task id from a per-task result dir leaf (``task-<n>``), or None.

    Mirrors ``aggregate_flow._task_id_from_dir``: the trailing integer run in the
    dir NAME is the cell's local index. ``None`` when the leaf carries no integer.
    """
    m = _TASK_DIR_RE.search(result_dir.name)
    return int(m.group(0)) if m else None


def _mirror_task_dirs(mirror: Path, summary_name: str) -> list[Path]:
    """Every result dir under *mirror* that carries the declared summary file.

    A summary artifact may be PATH-shaped (``sub/metrics.json``): the task dir is
    the match minus ALL of the artifact's components, mirroring
    ``aggregate_flow._task_dir`` — ``p.parent`` alone would keep the artifact's own
    subdir and ``reduce_metrics`` re-joining ``dir / summary_name`` would then
    double it. De-duplicated (two summary matches can never share a task dir, but a
    PATH-shaped artifact with siblings could).
    """
    if not mirror.is_dir():
        return []
    depth = len(PurePosixPath(summary_name).parts)
    seen: dict[str, Path] = {}
    for match in mirror.rglob(summary_name):
        if not match.is_file():
            continue
        tdir = match
        for _ in range(depth):
            tdir = tdir.parent
        seen.setdefault(str(tdir), tdir)
    return [seen[k] for k in sorted(seen)]


def _drop_canary_dirs(dirs: list[Path], run_id: str) -> tuple[list[Path], list[Path]]:
    """Split *dirs* into (kept, excluded) by this parent's canary family.

    The ``<run_id>-canary`` / ``-canary2`` siblings render their result dirs under
    the SAME ``results/`` subtree (their run_id has the main id as a prefix), so the
    recursive scan sweeps them in and the mean would double-count the canary's task
    (``aggregate_flow.py:733-745``, run #6). Exclude the whole ``-canary`` FAMILY via
    the single ``sibling_run_ids`` suffix definition — never a hardcoded ``-canary``.
    """
    canary_ids = set(sibling_run_ids(run_id))
    kept: list[Path] = []
    excluded: list[Path] = []
    for d in dirs:
        if canary_ids.isdisjoint(d.parts):
            kept.append(d)
        else:
            excluded.append(d)
    return kept, excluded


@dataclass(frozen=True)
class _Selection:
    """Internal: the owner-resolved dirs plus the accounting the reduce discloses."""

    selected: dict[int, str]  # source-global cell id → the owner's result-dir path
    dropped_raced: list[int]  # cells dropped to their owner (present under BOTH run_ids)
    excluded_canary: int  # per-parent canary dirs excluded before selection


def select_owned_dirs(
    *,
    source_mirror: Path,
    derived_mirror: Path,
    ownership: OwnershipMap,
    summary_name: str = DEFAULT_SUMMARY_NAME,
) -> _Selection:
    """Resolve each cell to EXACTLY its owner's result dir [SPEC §5.3, LIVE-2].

    For every result dir in each parent's mirror, key it to its **source-global**
    cell id (the source mirror keys directly; the derived mirror inverts
    ``ownership.derived_local_index``), then include the dir **iff the ownership map
    names this parent's run as that cell's owner**. A raced cell present under BOTH
    run_ids passes the filter in the OWNER's mirror only, so it is counted once.

    Guards that CAN fire (the reducer has no cardinality gate of its own, SPEC §5):

    - a source dir whose id is outside ``range(total)`` → ``owner_of`` REFUSES
      (``ownership.py`` — the census named a cell the source never had);
    - a derived dir whose local index has NO ``derived_local_index`` entry →
      REFUSE (a foreign/extra dir in the derived mirror — an over-count the
      value-keyed reducer would silently average);
    - the SAME cell selected as owned from BOTH mirrors → REFUSE (the ownership
      map is not exactly-once — the double-count safety net).
    """
    inv_local: dict[int, int] = {
        local_idx: gid for gid, local_idx in ownership.derived_local_index.items()
    }

    selected: dict[int, str] = {}
    dropped_raced: list[int] = []
    excluded_canary = 0

    # (mirror, this parent's run_id, leaf-id → source-global mapper)
    parents: list[tuple[Path, str, Callable[[int], int | None]]] = [
        (source_mirror, ownership.source_run_id, lambda leaf: leaf),
        (derived_mirror, ownership.derived_run_id, inv_local.get),
    ]

    for mirror, run_id, to_global in parents:
        dirs = _mirror_task_dirs(mirror, summary_name)
        kept, excluded = _drop_canary_dirs(dirs, run_id)
        excluded_canary += len(excluded)
        for tdir in kept:
            leaf = _task_id_from_dir_name(tdir)
            if leaf is None:
                # An unkeyable result dir cannot be attributed to an owner, and a
                # silent drop under-counts the union — refuse loudly.
                raise errors.SpecInvalid(
                    f"result dir {tdir} carries no task id in its name — cannot "
                    "attribute it to an owning parent; refusing to reduce over an "
                    "unkeyable cell (it would silently under-count the union)."
                )
            gid = to_global(leaf)
            if gid is None:
                raise errors.SpecInvalid(
                    f"derived result dir {tdir} (local index {leaf}) has no "
                    "derived_local_index entry — a foreign/extra dir in the derived "
                    "mirror the ownership map never enumerated; refusing to average "
                    "an over-count the value-keyed reducer cannot detect."
                )
            owner = ownership.owner_of(gid)  # REFUSES for an out-of-range cell id
            if owner != run_id:
                # Owned by the OTHER parent — a raced duplicate (or contamination).
                # Drop it here; the owner's mirror contributes it exactly once.
                dropped_raced.append(gid)
                continue
            if gid in selected:
                raise errors.SpecInvalid(
                    f"cell {gid} is owned+present in BOTH mirrors — the ownership "
                    "map is not exactly-once, so the reduce would double-count its "
                    "n. Refusing (the exactly-once selection is the whole safety "
                    "story, SPEC §5)."
                )
            selected[gid] = str(tdir)

    return _Selection(
        selected=selected,
        dropped_raced=sorted(set(dropped_raced)),
        excluded_canary=excluded_canary,
    )


@dataclass(frozen=True)
class MultiParentHarvestResult:
    """The two-parent reduce plus the accounting the migration brief discloses."""

    aggregated: dict  # reduce_metrics weighted-mean over the union of owned cells
    total: int  # the union cardinality (900 for the live case)
    cells_counted: int  # distinct owner cells that contributed a result dir
    source_cells_counted: int
    derived_cells_counted: int
    dropped_raced: list[int]  # cells present under both run_ids, dropped to the owner
    excluded_canary_dirs: int  # per-parent canary dirs excluded before selection
    selected_dirs: list[str] = field(default_factory=list)


def multi_parent_reduce(
    *,
    source_mirror: Path,
    derived_mirror: Path,
    ownership: OwnershipMap,
    summary_name: str = DEFAULT_SUMMARY_NAME,
) -> MultiParentHarvestResult:
    """Ownership-aware weighted-mean over both parents' mirrors [SPEC §3 Step F].

    Composes :func:`select_owned_dirs` (the ownership selection that replaces the
    single-run ``run_id`` filter) with the value-keyed ``reduce_metrics``
    weighted-mean (``metrics.py:134``) — the SAME reducer the single-run harvest
    uses, so the two-parent aggregate is byte-identical to a single-run reduce over
    the same cells.

    Cardinality gate (the safety net, SPEC §5.4): the selected cells must be a
    subset of ``range(total)`` sized ``<= total``. ``select_owned_dirs`` already
    refuses out-of-range ids and same-cell double-selection, so a surviving
    ``> total`` here is provable un-resolved overlap — refuse rather than average it.
    """
    total = int(ownership.total)
    sel = select_owned_dirs(
        source_mirror=source_mirror,
        derived_mirror=derived_mirror,
        ownership=ownership,
        summary_name=summary_name,
    )

    if len(sel.selected) > total:
        raise errors.SpecInvalid(
            f"two-parent harvest selected {len(sel.selected)} owned cells but the "
            f"union total is {total}; the surplus is un-resolved overlap the "
            "ownership map did not partition. Refusing to average an over-count "
            "(SPEC §5.4 cardinality gate)."
        )

    source_n = sum(1 for gid in sel.selected if ownership.owner_of(gid) == ownership.source_run_id)
    derived_n = len(sel.selected) - source_n

    # Deterministic order so the reduce and the disclosed dir list are stable.
    selected_dirs = [sel.selected[gid] for gid in sorted(sel.selected)]
    aggregated = reduce_metrics(selected_dirs, filename=summary_name)

    return MultiParentHarvestResult(
        aggregated=aggregated,
        total=total,
        cells_counted=len(sel.selected),
        source_cells_counted=source_n,
        derived_cells_counted=derived_n,
        dropped_raced=sel.dropped_raced,
        excluded_canary_dirs=sel.excluded_canary,
        selected_dirs=selected_dirs,
    )


@dataclass(frozen=True)
class ParentPull:
    """A read-only pull plan for ONE parent's per-task results [SPEC §3 Step F].

    Names the remote (``ssh_target`` + ``remote_path`` + ``remote_subdir``) and the
    LOCAL mirror the parent's ``summary_name`` sidecars land in. The two parents
    pull into DISJOINT local mirrors so the ownership selection sees each parent's
    cell-id space unambiguously.
    """

    ssh_target: str
    remote_path: str
    remote_subdir: str
    local_mirror: Path


def multi_parent_harvest(
    *,
    source_pull: ParentPull,
    derived_pull: ParentPull,
    ownership: OwnershipMap,
    summary_name: str = DEFAULT_SUMMARY_NAME,
    pull_fn: Callable[..., object] | None = None,
) -> MultiParentHarvestResult:
    """Pull BOTH parents' per-task results, then reduce with the ownership map.

    The read-only orchestrator: it filters each parent's ``results/`` subtree to the
    declared ``summary_name`` (the 1000x transfer lever — KB of sidecars, not the GB
    of artifacts beside them), lands them in DISJOINT local mirrors, and hands both
    to :func:`multi_parent_reduce`. It **actuates nothing** on either cluster (pull
    is read-only) and composes the EXISTING transport pull rather than re-implement
    it — the single-run harvest's pull building block, one per parent.

    *pull_fn* is injectable for testing/alternate engines; it defaults to
    ``infra.transport.rsync_pull`` and is called with the same keyword contract.
    A non-zero pull return code REFUSES — there is no deterministic numeric input to
    reduce, and fabricating an aggregate is exactly the failure this framework
    exists to prevent.
    """
    if pull_fn is None:
        from hpc_agent.infra.transport import rsync_pull as _rsync_pull

        pull_fn = _rsync_pull

    for plan in (source_pull, derived_pull):
        result = pull_fn(
            ssh_target=plan.ssh_target,
            remote_path=plan.remote_path,
            remote_subdir=plan.remote_subdir,
            local_dir=str(plan.local_mirror),
            include=[summary_name],
        )
        rc = getattr(result, "returncode", 0)
        if rc != 0:
            stderr_tail = (getattr(result, "stderr", "") or "").strip()
            raise errors.RemoteCommandFailed(
                f"two-parent harvest pull from {plan.ssh_target}:{plan.remote_path}/"
                f"{plan.remote_subdir} failed (exit {rc}); there is no deterministic "
                "numeric input to reduce for this parent — refusing to fabricate an "
                f"aggregate over a partial mirror. pull stderr: {stderr_tail[:300]}"
            )

    return multi_parent_reduce(
        source_mirror=source_pull.local_mirror,
        derived_mirror=derived_pull.local_mirror,
        ownership=ownership,
        summary_name=summary_name,
    )
