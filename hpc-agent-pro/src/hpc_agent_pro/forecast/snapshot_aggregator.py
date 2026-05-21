"""Aggregate features from saved squeue snapshots.

Builds time-series features for the LGBM predictor by walking
``<experiment_dir>/.hpc/squeue_snapshots/`` (written by
``scripts/snapshot_squeue.py``).

Two helpers:

* :func:`compute_arrival_rate_per_hour` — count distinct PENDING
  job IDs that appeared across the recent window divided by the
  window length. Direct measurement of "how fast new pendings are
  arriving on this partition right now."
* :func:`load_recent_snapshots` — yield (timestamp, parsed_queue)
  tuples for snapshots inside a time window, newest first.

Both are pure I/O helpers; no network. The training script and the
inference path both call them.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hpc_agent_pro.forecast.squeue_priority_field import (
    QueuedJob,
    parse_squeue_priority_field,
)


def _parse_snapshot_filename(path: Path) -> datetime | None:
    """``20260415T030000.tsv.gz`` → naive UTC datetime."""
    stem = path.name.split(".", 1)[0]
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _read_snapshot(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return f.read()
    return path.read_text(encoding="utf-8")


def load_recent_snapshots(
    experiment_dir: Path,
    *,
    now: datetime,
    window_hours: float,
) -> Iterator[tuple[datetime, list[QueuedJob]]]:
    """Yield ``(timestamp, parsed_queue)`` for every snapshot in
    ``[now - window_hours, now]``, newest first.

    Permissive — unparseable snapshot filenames are skipped, garbled
    contents yield empty queues. Caller iterates; never raises.

    *now* is normalized to UTC-aware if naive — snapshot timestamps are
    always tz-aware (UTC), so a naive *now* would otherwise raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    inside the cutoff comparison. v3 BUG-5V3-3.
    """
    snap_dir = experiment_dir / ".hpc" / "squeue_snapshots"
    if not snap_dir.is_dir():
        return
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    triples: list[tuple[datetime, Path]] = []
    for path in snap_dir.iterdir():
        ts = _parse_snapshot_filename(path)
        if ts is None or ts < cutoff or ts > now:
            continue
        triples.append((ts, path))
    triples.sort(key=lambda t: t[0], reverse=True)
    for ts, path in triples:
        try:
            text = _read_snapshot(path)
        except OSError:
            continue
        yield ts, parse_squeue_priority_field(text)


def compute_arrival_rate_per_hour(
    experiment_dir: Path,
    *,
    now: datetime,
    partition: str,
    window_hours: float = 6.0,
) -> float | None:
    """Count distinct PENDING job IDs (in *partition*) seen across
    the recent window, divide by window length.

    Returns ``None`` when fewer than 2 snapshots fall inside the
    window — a single point can't measure arrival rate.

    Each pending job is counted at most once even if it appears in
    multiple snapshots: we want "how many NEW pending jobs entered
    the queue in the window," not "how many pending observations."
    First-seen detection: a job_id present in the OLDEST snapshot is
    pre-existing (doesn't count); job_ids appearing in newer
    snapshots count as arrivals.
    """
    snapshots = list(load_recent_snapshots(experiment_dir, now=now, window_hours=window_hours))
    if len(snapshots) < 2:
        return None
    # snapshots is newest-first; reverse for chronological walk.
    chronological = list(reversed(snapshots))
    pre_existing_pending_ids = {
        j.job_id for j in chronological[0][1] if j.partition == partition and j.state == "PENDING"
    }
    seen_arrivals: set[str] = set()
    for _, queue in chronological[1:]:
        for j in queue:
            if (
                j.partition == partition
                and j.state == "PENDING"
                and j.job_id not in pre_existing_pending_ids
            ):
                seen_arrivals.add(j.job_id)
    # Denominator is ``t_last - t_baseline`` because the counting
    # window is half-open ``(t_baseline, t_last]``: arrivals that
    # occurred between the baseline and the first follow-up snapshot
    # show up at ``chronological[1]`` as PENDING-not-in-baseline, so
    # the baseline IS the lower bound on observable arrival time —
    # not chronological[1]. Using ``[-1] - [1]`` (which an earlier
    # audit pass proposed) would bias the rate HIGH by dropping the
    # ``(t0, t1]`` interval from the denominator while keeping the
    # arrivals it observed in the numerator.
    actual_window_hours = (chronological[-1][0] - chronological[0][0]).total_seconds() / 3600
    if actual_window_hours <= 0:
        return None
    return len(seen_arrivals) / actual_window_hours


__all__ = [
    "compute_arrival_rate_per_hour",
    "load_recent_snapshots",
]
