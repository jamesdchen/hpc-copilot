"""Census the source run's done-set and partition the remainder [SPEC §3 Step A].

The first third of ``migrate-remainder``: given a still-live source run, compute
which of its ``range(total_tasks)`` cells are DONE (from the cluster's per-task
terminal announcements) and which are UNDONE (the migration payload), preferring a
**wave/axis-aligned** remainder when the sidecar ``wave_map`` allows it [LIVE-1]
and falling back to an explicit arbitrary-id ``task_range`` otherwise. This module
**actuates nothing** and does at most ONE bounded ssh read (the announce id
listing); the status-reporter cross-check consumes a report the caller already
fetched (read-only). It returns in seconds.

Four guards that CAN fire (the engineering-principles rule):

- **Δ1 — no per-task census REFUSES.** ``read_announced_task_ids`` reads the SET of
  done ids under the SAME ack discipline as the counts read; an absent announce dir
  / dropped ack is "no per-task census present" (pre-announce run, or the dispatcher
  never started) and REFUSES — never "all undone", which would re-run every
  already-finished task.
- **Δ2 — wave-alignment.** The undone set is intersected with the sidecar
  ``wave_map`` (built by ``build_wave_map``; ids→waves mapped as in
  ``recover_flow.py:299-314``). When every undone id falls in a set of WHOLE waves
  (those waves are entirely undone), the migration unit is those waves (one array
  each); only a wave-splitting remainder falls to an arbitrary-id range.
- **Status-reporter cross-check.** When the caller supplies a status report, the
  announce done-set is cross-checked against the reporter's ``complete`` ids
  (``rows_observed_from_report``, ``cluster_status.py``); a DISAGREEMENT is
  SURFACED in the result (``disagreement``), never auto-masked (the aggregate-check
  integrity precedent, ``aggregate_blocks.py:451``).
- **Index-bounded target REFUSES a non-contiguous range.** An index-bounded target
  backend (``uses_global_array_index=False``, ``backends/__init__.py:187``) submits
  a LOCAL ``1-N`` array + one ``TASK_OFFSET`` per wave, so it can express a whole
  wave (contiguous) or a single contiguous window, but NOT a non-contiguous
  arbitrary remainder. Such a case REFUSES, surfacing the range shape.

This unit does not import ``derive``/``ownership`` (M-DERIVE consumes the census's
``undone_ids``/``done_ids`` as a data contract); it reads ``announce`` for the id
listing and ``cluster_status`` for the read-only report parse.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.cluster_status import rows_observed_from_report
from hpc_agent.ops.monitor.announce import AnnouncedTaskIds, read_announced_task_ids

__all__ = ["MigrationCensus", "census_remainder"]

# The remainder's range SHAPE, ordered least→most constrained for the target.
_SHAPE_WAVE_ALIGNED = "wave_aligned"  # whole undone waves — one array each
_SHAPE_CONTIGUOUS = "contiguous"  # a single [a..b] window — one array + offset
_SHAPE_ARBITRARY = "arbitrary"  # non-contiguous ids — needs a global index space


@dataclass(frozen=True)
class MigrationCensus:
    """The authoritative done/undone partition + the target-expressible range shape.

    Everything M-DERIVE needs as a data contract (``undone_ids``/``done_ids``) plus
    the brief-facing metadata (the range shape, the whole-wave list, and the
    never-masked status-reporter ``disagreement``).
    """

    source_run_id: str
    total_tasks: int
    done_ids: tuple[int, ...]
    undone_ids: tuple[int, ...]
    undone_count: int
    present: bool
    wave_aligned: bool
    whole_waves: tuple[int, ...]
    task_range: str
    range_shape: str
    n_ranges: int
    target_uses_global_array_index: bool
    disagreement: dict[str, list[int]] | None

    def digest(self) -> dict[str, Any]:
        """A compact, JSON-safe summary for the migration brief (M-BRIEF)."""
        return {
            "source_run_id": self.source_run_id,
            "total_tasks": self.total_tasks,
            "done_count": len(self.done_ids),
            "undone_count": self.undone_count,
            "wave_aligned": self.wave_aligned,
            "whole_waves": list(self.whole_waves),
            "task_range": self.task_range,
            "range_shape": self.range_shape,
            "n_ranges": self.n_ranges,
            "disagreement": self.disagreement,
        }


def _to_ranges(ids: Sequence[int]) -> list[tuple[int, int]]:
    """Compress a sorted id list into ``[(lo, hi), …]`` maximal runs."""
    ranges: list[tuple[int, int]] = []
    for i in sorted(ids):
        if ranges and i == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], i)
        elif not ranges or i != ranges[-1][1]:
            ranges.append((i, i))
    return ranges


def _format_range(ids: Sequence[int]) -> str:
    """Render ids as a scheduler ``task_range`` string, e.g. ``0-99,200,305-307``."""
    return ",".join(f"{lo}" if lo == hi else f"{lo}-{hi}" for lo, hi in _to_ranges(ids))


def _wave_alignment(
    undone: set[int], wave_map: Mapping[str, Sequence[int]] | None
) -> tuple[bool, list[int]]:
    """Return ``(aligned, whole_wave_numbers)`` for the undone set [Δ2, LIVE-1].

    Aligned iff every undone id is covered by the ``wave_map`` AND the waves it
    touches are ENTIRELY undone — i.e. the union of those waves' ids equals the
    undone set exactly (a wave split between done and undone breaks alignment).
    Ids→waves are mapped as ``recover_flow.py:299-314`` does.
    """
    if not wave_map:
        return False, []
    id_to_wave: dict[int, int] = {}
    wave_ids: dict[int, set[int]] = {}
    for wave_key, tids in wave_map.items():
        try:
            wave_num = int(wave_key)
        except (TypeError, ValueError):
            continue
        try:
            members = {int(x) for x in (tids or [])}
        except (TypeError, ValueError):
            continue
        wave_ids[wave_num] = members
        for tid in members:
            id_to_wave[tid] = wave_num
    if not undone or not (undone <= set(id_to_wave)):
        return False, []
    touched = {id_to_wave[i] for i in undone}
    union: set[int] = set()
    for wave_num in touched:
        union |= wave_ids[wave_num]
    if union == undone:
        return True, sorted(touched)
    return False, []


def _cross_check(
    done: set[int], total_tasks: int, status_report: Mapping[str, Any]
) -> dict[str, list[int]] | None:
    """Cross-check the announce done-set vs the reporter's complete ids.

    Returns the never-masked disagreement (``announce_only`` / ``reporter_only``)
    or ``None`` on agreement. Reporter ids outside ``range(total_tasks)`` are
    dropped (a skew guard) before the diff.
    """
    complete_ids, _rows_by_task, _emits = rows_observed_from_report(dict(status_report))
    reporter = {i for i in complete_ids if 0 <= i < total_tasks}
    announce_only = sorted(done - reporter)
    reporter_only = sorted(reporter - done)
    if not announce_only and not reporter_only:
        return None
    return {"announce_only": announce_only, "reporter_only": reporter_only}


def census_remainder(
    *,
    ssh_target: str,
    remote_path: str,
    source_run_id: str,
    total_tasks: int,
    target_uses_global_array_index: bool,
    wave_map: Mapping[str, Sequence[int]] | None = None,
    status_report: Mapping[str, Any] | None = None,
    _read_ids: Callable[..., AnnouncedTaskIds] | None = None,
) -> MigrationCensus:
    """Census the source's done-set and partition the undone remainder.

    Parameters
    ----------
    total_tasks
        The source run's task count (from its sidecar); the census partitions
        ``range(total_tasks)``.
    target_uses_global_array_index
        The TARGET backend's ``uses_global_array_index`` capability
        (``backends/__init__.py:187``): ``True`` ⇒ any range is expressible;
        ``False`` (index-bounded) ⇒ a non-contiguous arbitrary remainder REFUSES.
    wave_map
        The source sidecar's ``{str(wave): [global ids]}`` map (or ``None``); when
        the remainder falls in whole waves the migration unit is those waves [Δ2].
    status_report
        An optional pre-fetched status report (``ssh_status_report`` shape) for the
        read-only cross-check; a disagreement is surfaced, never auto-masked.

    Refusals (guards that CAN fire):

    - no per-task census (absent announce dir / dropped ack) → :class:`errors.PreconditionFailed`
      (never "all undone"); [Δ1]
    - the source has no undone tasks (nothing to migrate) → :class:`errors.PreconditionFailed`
      (route to plain aggregate, §3.A.4);
    - an index-bounded target that cannot express a non-contiguous remainder →
      :class:`errors.SpecInvalid`, surfacing the range shape.
    """
    if total_tasks <= 0:
        raise errors.SpecInvalid(
            f"total_tasks must be positive to census a remainder (got {total_tasks!r}); "
            "read the source sidecar's task_count first."
        )
    reader = _read_ids if _read_ids is not None else read_announced_task_ids
    census = reader(ssh_target=ssh_target, remote_path=remote_path, run_id=source_run_id)

    # Δ1 — absent census REFUSES; never treat "no ack" as "all undone".
    if not census.present:
        raise errors.PreconditionFailed(
            f"no per-task census present for {source_run_id!r} (pre-announce run, or "
            "the dispatcher never started): the .hpc/announce/<run_id> dir is absent "
            "or the read carried no positive-evidence ack. Reconcile the source run "
            "first — refusing to treat an absent census as 'all tasks undone'."
        )

    # Bound the done ids to the run's task space (a stray marker outside range is
    # ignored) and derive the undone remainder.
    done = {i for i in census.done_ids if 0 <= i < total_tasks}
    undone_set = set(range(total_tasks)) - done
    if not undone_set:
        raise errors.PreconditionFailed(
            f"source run {source_run_id!r} has every task done (0 undone of "
            f"{total_tasks}) — nothing to migrate; route to plain aggregate."
        )
    undone = sorted(undone_set)

    # Status-reporter cross-check — surfaced, never auto-masked.
    disagreement: dict[str, list[int]] | None = None
    if status_report is not None:
        disagreement = _cross_check(done, total_tasks, status_report)

    # Δ2 — wave alignment, then range shape.
    aligned, whole_waves = _wave_alignment(undone_set, wave_map)
    ranges = _to_ranges(undone)
    n_ranges = len(ranges)
    if aligned:
        range_shape = _SHAPE_WAVE_ALIGNED
    elif n_ranges == 1:
        range_shape = _SHAPE_CONTIGUOUS
    else:
        range_shape = _SHAPE_ARBITRARY
    task_range = _format_range(undone)

    # Index-bounded target REFUSES a non-contiguous arbitrary remainder — it can
    # only submit a LOCAL 1-N array + one offset per wave (a contiguous window or a
    # whole wave), never a scattered id set. Surface the range shape.
    if not target_uses_global_array_index and range_shape == _SHAPE_ARBITRARY:
        raise errors.SpecInvalid(
            f"the undone remainder of {source_run_id!r} is a non-contiguous range "
            f"({task_range}, {n_ranges} disjoint windows) but the target backend is "
            "index-bounded (uses_global_array_index=False): it can express a whole "
            "wave or a single contiguous window, not a scattered id set. Migrate to a "
            "global-array-index backend, or reconcile the source so the remainder "
            "aligns to whole waves."
        )

    return MigrationCensus(
        source_run_id=source_run_id,
        total_tasks=total_tasks,
        done_ids=tuple(sorted(done)),
        undone_ids=tuple(undone),
        undone_count=len(undone),
        present=True,
        wave_aligned=aligned,
        whole_waves=tuple(whole_waves),
        task_range=task_range,
        range_shape=range_shape,
        n_ranges=n_ranges,
        target_uses_global_array_index=target_uses_global_array_index,
        disagreement=disagreement,
    )
