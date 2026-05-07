"""Parser + position estimator for the full SLURM priority queue (lesson 7).

The framework's existing :mod:`queue_simulator` models only the
user's own queue position. Lesson 7 from the backfill session: real
backfill modelling needs other users' priorities too — competitor
jobs draining out of higher-priority pools is what actually opens
shadows. This module is the foundation for that broader simulator.

Two pure helpers:

1. :func:`parse_squeue_priority_field` — ingests
   ``squeue -O 'jobid,priority,partition,user,state,timeleft'``
   pipe-separated output into typed records.
2. :func:`estimate_rank` — given a parsed list + a hypothetical new
   priority, returns where the new job would land (rank, queue depth
   in front, partition-specific depth).

The actual drain simulator (priority queue + partition dispatch)
isn't here yet — that's a separate piece, harder to ship without
real cluster data to validate against. But the parser + rank
estimator are useful on their own and unlock the simulator when it
arrives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class QueuedJob:
    """One row of squeue output.

    * ``time_left_sec`` — remaining walltime for RUNNING jobs;
      ``None`` for PENDING (the scheduler hasn't started timing them).
    * ``time_limit_sec`` — what the user *requested*. Set on every
      job, RUNNING or PENDING. The drain simulator uses this for
      pending jobs to decide whether they fit in backfill shadows.
    """

    job_id: str
    priority: int
    partition: str
    user: str
    state: str  # PENDING / RUNNING / etc.
    time_left_sec: int | None
    time_limit_sec: int | None = None


def _parse_time_left(token: str) -> int | None:
    """SLURM's compact time format: ``[D-]HH:MM:SS`` or ``MM:SS`` or
    ``SS`` or ``UNLIMITED`` / ``N/A``. Returns seconds or None."""
    if not token or token in {"UNLIMITED", "N/A", "INVALID", ""}:
        return None
    # D-HH:MM:SS
    m = re.match(r"^(?:(\d+)-)?(\d{1,3}):(\d{2}):(\d{2})(?:\.\d+)?$", token)
    if m:
        d = int(m.group(1) or 0)
        return d * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    # MM:SS
    m = re.match(r"^(\d+):(\d{2})$", token)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    # bare integer seconds
    if token.isascii() and token.isdigit():
        return int(token)
    return None


def parse_squeue_priority_field(text: str) -> list[QueuedJob]:
    """Parse pipe-separated ``squeue -O`` output.

    Expected header line (first non-blank line): ``JOBID|PRIORITY|
    PARTITION|USER|STATE|TIME_LEFT`` (case-insensitive). The parser
    keys columns by name so a different ordering doesn't break.
    Permissive: rows with unparseable priority are skipped; missing
    columns surface as None / empty.
    """
    if not text:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = [col.strip().upper() for col in lines[0].split("|")]
    name_to_idx = {col: i for i, col in enumerate(header)}
    required = {"JOBID", "PRIORITY", "PARTITION", "USER", "STATE"}
    if not required <= set(name_to_idx):
        return []

    def _cell(cells: list[str], col: str) -> str:
        i = name_to_idx.get(col)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    out: list[QueuedJob] = []
    for raw in lines[1:]:
        cells = raw.split("|")
        try:
            priority = int(_cell(cells, "PRIORITY"))
        except ValueError:
            continue
        out.append(
            QueuedJob(
                job_id=_cell(cells, "JOBID"),
                priority=priority,
                partition=_cell(cells, "PARTITION"),
                user=_cell(cells, "USER"),
                state=_cell(cells, "STATE"),
                time_left_sec=_parse_time_left(_cell(cells, "TIME_LEFT")),
                # TIME_LIMIT is the user-requested walltime; required
                # for backfill simulation. Same compact format as
                # TIME_LEFT (D-HH:MM:SS / HH:MM:SS / MM:SS / N).
                time_limit_sec=_parse_time_left(_cell(cells, "TIME_LIMIT")),
            )
        )
    return out


@dataclass(frozen=True)
class RankEstimate:
    """Result of :func:`estimate_rank` — where a hypothetical new job lands."""

    rank_overall: int  # 1-based; 1 = front of queue
    rank_in_partition: int
    pending_ahead_overall: int
    pending_ahead_in_partition: int


def estimate_rank(
    queue: list[QueuedJob],
    *,
    new_priority: int,
    partition: str | None = None,
) -> RankEstimate:
    """Given a parsed queue and a hypothetical new priority, return the
    rank the new job would take.

    Considers PENDING jobs only (running jobs aren't competitors for
    the front of the queue). Higher priority → earlier rank.
    Partition-scoped rank counts only competitors in *partition* when
    given.
    """
    pending = [j for j in queue if j.state == "PENDING"]
    pending_overall = sum(1 for j in pending if j.priority > new_priority)
    pending_in_partition = (
        sum(1 for j in pending if j.priority > new_priority and j.partition == partition)
        if partition is not None
        else pending_overall
    )
    return RankEstimate(
        rank_overall=pending_overall + 1,
        rank_in_partition=pending_in_partition + 1,
        pending_ahead_overall=pending_overall,
        pending_ahead_in_partition=pending_in_partition,
    )


__all__ = [
    "QueuedJob",
    "RankEstimate",
    "parse_squeue_priority_field",
    "estimate_rank",
]
