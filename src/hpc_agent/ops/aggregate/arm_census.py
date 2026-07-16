"""Per-arm completeness census for streaming aggregation [SPEC §3 Step A, S-CENSUS].

The first third of ``aggregate-stream``: given a still-draining run, decide which
of its **arms** are DONE *now* (every task in the arm reached a ``.complete``
terminal announcement) and which are still PENDING — so the streaming reduce can
emit a partial-but-honest table over the complete arms and disclose the rest **by
name** (never a silent cap).

This module does exactly three things to each arm, and never opens a
``metrics.json`` — the determinism-boundary the framework is built on
(``docs/internals/principles/determinism-boundary.md``): **IDENTITY** (which
task ids belong to the arm — read from the sidecar ``wave_map``), **COUNT** (how
many of them announced ``.complete`` — from the marker set), **COMPARE**
(``done == expected`` → COMPLETE). The arm is opaque to core; the run's own
deterministic reducer alone computes over its contents. No LLM in the numeric
loop, no metric named.

The single hardest problem (SPEC §8) is the **task→arm join**: the announce
census gives task *ids*, the reducer emits arm *rows*, and the map between them
lives in the reducer's own grouping, which core does not own. v1 ships the
**wave-aligned-only** resolution: an arm is a whole wave, provable from the
sidecar ``wave_map`` (the ``migrate/census._wave_alignment`` precedent,
``ops/migrate/census.py:113``; the bucket-major [LIVE-1] tiling the live
lgbm/xgb reducers use). A run whose ``wave_map`` is absent or does not cleanly
partition its task range **REFUSES** — "arm grouping not declared; final harvest
only" — rather than guess a grouping and risk emitting a half-drained arm's
wrong ``n``. A mis-join must refuse, never silently emit (the load-bearing
invariant, SPEC §8): so the guards below are the whole correctness story.

Four guards that CAN fire (the engineering-principles rule — verify each guard
can actually fire):

- **no per-task census REFUSES (Δ1).** ``read_announced_task_ids`` reads the SET
  of done ids under the ACK discipline (``ops/monitor/announce.py:183``); an
  absent announce dir / dropped ack is ``present=False`` and REFUSES — never read
  as "every arm undone" (which would emit an empty table as if nothing landed).
  An ssh transport failure (rc 255) raises inside the reader — a blip is never an
  empty done-set.
- **no wave_map REFUSES.** Without a declared arm grouping there is no safe
  task→arm join (SPEC §8 resolution 2). Refuse with "final harvest only".
- **a non-partitioning wave_map REFUSES.** Gaps, overlaps, out-of-range ids, or a
  non-integer wave key mean the wave_map is not a clean arm partition — a
  "complete arm" might not line up with a reducer row. Refuse rather than
  mis-fire the n-guard.
- **the whole-arm n-guard.** An arm is COMPLETE iff EVERY task in its set
  announced ``.complete``; a single missing task keeps it PENDING (carrying
  ``tasks_done``/``tasks_expected``). A half-drained bucket is never emitted — the
  reducer would otherwise mean a wrong ``n``/``qlike`` over the drained subset
  (the live ``xgb/vol_demand`` case).

The status-reporter cross-check (``infra.cluster_status.rows_observed_from_report``)
is surfaced, never auto-masked (the aggregate-check integrity precedent,
``aggregate_blocks.py``; the ``migrate/census._cross_check`` shape). This module
**actuates nothing** and does at most ONE bounded ssh read (the announce id
listing); it returns in seconds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.cluster_status import rows_observed_from_report
from hpc_agent.ops.monitor.announce import AnnouncedTaskIds, read_announced_task_ids

__all__ = ["ArmCompleteness", "ArmCensus", "census_arms"]


@dataclass(frozen=True)
class ArmCompleteness:
    """One arm's done/expected partition — IDENTITY/COUNT/COMPARE, no metric read.

    ``owner_run_id`` names the parent run whose mirror carries this arm's result
    dirs (the single-run census sets it to the run itself; the multi-leg census
    sets each parent's own id, so the union pending list attributes every arm to
    its leg — the operator's lgbm-complete / xgb-``vol_demand``-pending table).
    """

    arm: str
    owner_run_id: str
    task_ids: tuple[int, ...]
    tasks_done: int
    tasks_expected: int
    complete: bool

    def pending_digest(self) -> dict[str, Any]:
        """The by-name disclosure row for the ``arms_pending`` block (SPEC §3.D).

        Every pending arm rides this — its name, its progress, and where it is
        coming from — so a pending arm is NEVER silently dropped from the surface.
        """
        return {
            "arm": self.arm,
            "tasks_done": self.tasks_done,
            "tasks_expected": self.tasks_expected,
            "owner_run_id": self.owner_run_id,
        }


@dataclass(frozen=True)
class ArmCensus:
    """The per-arm completeness census for ONE parent run.

    ``present`` is always True on a returned census (an absent census REFUSES in
    :func:`census_arms` rather than returning ``present=False`` — a streaming
    reduce must never run off a fabricated empty done-set). ``disagreement`` is
    the never-masked status-reporter cross-check (or ``None`` on agreement).
    """

    run_id: str
    present: bool
    arms: tuple[ArmCompleteness, ...]
    disagreement: dict[str, list[int]] | None

    @property
    def complete_arms(self) -> tuple[ArmCompleteness, ...]:
        """The arms whose every task announced ``.complete`` — safe to reduce."""
        return tuple(a for a in self.arms if a.complete)

    @property
    def pending_arms(self) -> tuple[ArmCompleteness, ...]:
        """The still-draining arms — disclosed by name, never reduced (n-guard)."""
        return tuple(a for a in self.arms if not a.complete)

    def digest(self) -> dict[str, Any]:
        """A compact JSON-safe summary for the streaming brief."""
        return {
            "run_id": self.run_id,
            "arms_total": len(self.arms),
            "arms_complete": [a.arm for a in self.complete_arms],
            "arms_pending": [a.pending_digest() for a in self.pending_arms],
            "disagreement": self.disagreement,
        }


def _wave_partition(
    wave_map: Mapping[str, Sequence[int]] | None, total_tasks: int
) -> dict[int, frozenset[int]]:
    """Return ``{wave_num: frozenset(ids)}`` iff the wave_map cleanly partitions.

    A clean partition is the v1 wave-aligned-arm invariant (SPEC §8): every id in
    ``range(total_tasks)`` belongs to exactly one wave, no wave carries an
    out-of-range id, and every wave key is an integer. Any violation REFUSES with
    a message naming the defect and "final harvest only" — because a wave_map that
    is not a partition cannot be trusted as the task→arm join, and a mis-join
    would silently emit a half-drained arm or withhold a complete one.
    """
    if not wave_map:
        raise errors.SpecInvalid(
            "aggregate-stream needs a declared arm grouping to census per-arm "
            "completeness, but the run's sidecar carries no wave_map. v1 streams "
            "wave-aligned runs only (an arm = a whole wave, SPEC §8); a run whose "
            "arm grouping is not declared cannot be streamed safely — final "
            "harvest only. Reconcile / re-aggregate at terminal instead."
        )
    wave_members: dict[int, frozenset[int]] = {}
    seen: set[int] = set()
    for wave_key, ids in wave_map.items():
        try:
            wnum = int(wave_key)
        except (TypeError, ValueError):
            raise errors.SpecInvalid(
                f"wave_map carries a non-integer wave key {wave_key!r}; core cannot "
                "treat it as an arm partition. Refusing to stream a run whose arm "
                "grouping is malformed — final harvest only."
            ) from None
        try:
            members = frozenset(int(x) for x in (ids or []))
        except (TypeError, ValueError):
            raise errors.SpecInvalid(
                f"wave_map[{wave_key!r}] carries a non-integer task id; core cannot "
                "resolve the arm's task set. Refusing to stream — final harvest only."
            ) from None
        overlap = members & seen
        if overlap:
            raise errors.SpecInvalid(
                f"wave_map is not a partition: task ids {sorted(overlap)!r} appear in "
                f"more than one wave (wave {wnum} overlaps an earlier wave). An arm "
                "must be a disjoint task set — refusing to stream a non-wave-aligned "
                "run (final harvest only)."
            )
        seen |= members
        wave_members[wnum] = members
    full = set(range(total_tasks))
    if seen != full:
        missing = sorted(full - seen)
        extra = sorted(seen - full)
        raise errors.SpecInvalid(
            "wave_map does not cleanly partition the task range "
            f"0..{total_tasks - 1}: "
            + (f"ids {missing!r} belong to no wave" if missing else "")
            + ("; " if missing and extra else "")
            + (f"ids {extra!r} are outside the task range" if extra else "")
            + ". The arm grouping is not wave-aligned — refusing to stream (final "
            "harvest only)."
        )
    return wave_members


def _cross_check(
    done: frozenset[int], total_tasks: int, status_report: Mapping[str, Any]
) -> dict[str, list[int]] | None:
    """Cross-check the announce done-set vs the reporter's complete ids.

    Returns the never-masked disagreement (``announce_only`` / ``reporter_only``)
    or ``None`` on agreement. Mirrors ``migrate/census._cross_check`` (kept a
    module-private copy rather than a cross-package private import — the W2
    boundary lint). Reporter ids outside ``range(total_tasks)`` are dropped (a
    skew guard) before the diff.
    """
    complete_ids, _rows_by_task, _emits = rows_observed_from_report(dict(status_report))
    reporter = {i for i in complete_ids if 0 <= i < total_tasks}
    announce_only = sorted(done - reporter)
    reporter_only = sorted(reporter - done)
    if not announce_only and not reporter_only:
        return None
    return {"announce_only": announce_only, "reporter_only": reporter_only}


def census_arms(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    wave_map: Mapping[str, Sequence[int]] | None,
    total_tasks: int,
    owner_run_id: str | None = None,
    status_report: Mapping[str, Any] | None = None,
    _read_ids: Callable[..., AnnouncedTaskIds] | None = None,
) -> ArmCensus:
    """Census which of *run_id*'s arms are COMPLETE now [SPEC §3 Step A].

    Parameters
    ----------
    total_tasks
        The run's task count (from its sidecar ``task_count``); the arm partition
        must cover ``range(total_tasks)`` exactly.
    wave_map
        The run sidecar's ``{str(wave): [global ids]}`` map. v1 requires it — an
        absent / non-partitioning wave_map REFUSES (wave-aligned streaming only,
        SPEC §8).
    owner_run_id
        The run that owns these arms' result dirs (defaults to *run_id*); the
        multi-leg caller passes each parent's own id so pending arms are
        attributed to their leg.
    status_report
        An optional pre-fetched status report for the read-only cross-check; a
        disagreement is surfaced in ``ArmCensus.disagreement``, never auto-masked.

    Refusals (guards that CAN fire):

    - ``total_tasks <= 0`` → :class:`errors.SpecInvalid`.
    - absent / non-partitioning wave_map → :class:`errors.SpecInvalid` (final
      harvest only).
    - no per-task census (absent announce dir / dropped ack) →
      :class:`errors.PreconditionFailed` (never "all arms undone"; Δ1).
    """
    if total_tasks <= 0:
        raise errors.SpecInvalid(
            f"total_tasks must be positive to census arms (got {total_tasks!r}); "
            "read the run sidecar's task_count first."
        )
    owner = owner_run_id or run_id

    # Task→arm join: the wave-aligned partition (refuses a non-aligned run).
    wave_members = _wave_partition(wave_map, total_tasks)

    reader = _read_ids if _read_ids is not None else read_announced_task_ids
    census = reader(ssh_target=ssh_target, remote_path=remote_path, run_id=run_id)

    # Δ1 — absent census REFUSES; never treat "no ack" as "all arms undone".
    if not census.present:
        raise errors.PreconditionFailed(
            f"no per-task census present for {run_id!r} (pre-announce run, or the "
            "dispatcher never started): the .hpc/announce/<run_id> dir is absent or "
            "the read carried no positive-evidence ack. Reconcile the run first — "
            "refusing to stream over an absent census (it would read as an empty "
            "table, as if nothing had landed)."
        )

    # Bound done ids to the run's task space (a stray marker outside range is
    # ignored), then classify every arm whole (the n-guard).
    done = frozenset(i for i in census.done_ids if 0 <= i < total_tasks)

    disagreement: dict[str, list[int]] | None = None
    if status_report is not None:
        disagreement = _cross_check(done, total_tasks, status_report)

    arms: list[ArmCompleteness] = []
    for wnum in sorted(wave_members):
        members = wave_members[wnum]
        done_here = members & done
        arms.append(
            ArmCompleteness(
                arm=str(wnum),
                owner_run_id=owner,
                task_ids=tuple(sorted(members)),
                tasks_done=len(done_here),
                tasks_expected=len(members),
                # COMPARE: complete iff EVERY task in the arm announced .complete.
                complete=members <= done,
            )
        )

    return ArmCensus(
        run_id=run_id,
        present=True,
        arms=tuple(arms),
        disagreement=disagreement,
    )
