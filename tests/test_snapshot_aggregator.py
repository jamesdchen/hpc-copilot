"""Tests for ``forecast.snapshot_aggregator``.

Pattern: write synthetic squeue snapshots to ``tmp_path/.hpc/squeue_snapshots/``
with timestamps in the filenames; verify the aggregator picks them up
correctly.
"""

from __future__ import annotations

import gzip
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from claude_hpc.forecast.snapshot_aggregator import (
    compute_arrival_rate_per_hour,
    load_recent_snapshots,
)

if TYPE_CHECKING:
    from pathlib import Path


_NOW = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)


def _write_snapshot(
    experiment_dir: Path,
    *,
    at: datetime,
    rows: list[tuple[str, int, str, str, str]],
    gzipped: bool = True,
) -> None:
    """Each row is (job_id, priority, partition, user, state)."""
    out_dir = experiment_dir / ".hpc" / "squeue_snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = at.strftime("%Y%m%dT%H%M%S") + (".tsv.gz" if gzipped else ".tsv")
    target = out_dir / fname
    body = "JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT|TIME_LIMIT\n"
    for jid, prio, part, user, state in rows:
        body += f"{jid}|{prio}|{part}|{user}|{state}|N/A|3600\n"
    if gzipped:
        with gzip.open(target, "wt", encoding="utf-8") as f:
            f.write(body)
    else:
        target.write_text(body)


# ─── load_recent_snapshots ────────────────────────────────────────────


def test_no_snapshot_dir_yields_nothing(tmp_path: Path) -> None:
    out = list(load_recent_snapshots(tmp_path, now=_NOW, window_hours=6))
    assert out == []


def test_loads_only_snapshots_in_window(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=2), rows=[("a", 100, "gpu", "u", "PENDING")]
    )
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=10), rows=[("b", 100, "gpu", "u", "PENDING")]
    )
    out = list(load_recent_snapshots(tmp_path, now=_NOW, window_hours=6))
    assert len(out) == 1
    assert out[0][1][0].job_id == "a"


def test_loads_newest_first(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=5), rows=[("old", 100, "gpu", "u", "PENDING")]
    )
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=1), rows=[("new", 100, "gpu", "u", "PENDING")]
    )
    out = list(load_recent_snapshots(tmp_path, now=_NOW, window_hours=6))
    assert [snap[1][0].job_id for snap in out] == ["new", "old"]


def test_unparseable_filenames_skipped(tmp_path: Path) -> None:
    """Files that don't match the timestamp format are silently skipped."""
    snap_dir = tmp_path / ".hpc" / "squeue_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "garbage.txt").write_text("not a snapshot")
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=1), rows=[("a", 100, "gpu", "u", "PENDING")]
    )
    out = list(load_recent_snapshots(tmp_path, now=_NOW, window_hours=6))
    assert len(out) == 1


# ─── compute_arrival_rate_per_hour ────────────────────────────────────


def test_arrival_rate_returns_none_with_fewer_than_2_snapshots(tmp_path: Path) -> None:
    """Single snapshot can't measure rate; returns None so caller can
    fall back."""
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(minutes=30), rows=[("a", 100, "gpu", "u", "PENDING")]
    )
    out = compute_arrival_rate_per_hour(tmp_path, now=_NOW, partition="gpu")
    assert out is None


def test_arrival_rate_excludes_pre_existing_pendings(tmp_path: Path) -> None:
    """Jobs present in the OLDEST snapshot are pre-existing; only new
    arrivals count toward the rate."""
    # t-2h: a is pending (pre-existing)
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=2), rows=[("a", 100, "gpu", "u", "PENDING")]
    )
    # t-1h: a still pending, b NEW
    _write_snapshot(
        tmp_path,
        at=_NOW - timedelta(hours=1),
        rows=[("a", 100, "gpu", "u", "PENDING"), ("b", 100, "gpu", "u", "PENDING")],
    )
    rate = compute_arrival_rate_per_hour(tmp_path, now=_NOW, partition="gpu")
    # 1 new arrival (b) over a 1h window → 1.0/hr.
    assert rate is not None
    assert abs(rate - 1.0) < 0.1


def test_arrival_rate_only_counts_target_partition(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=2), rows=[("seed", 100, "gpu", "u", "PENDING")]
    )
    _write_snapshot(
        tmp_path,
        at=_NOW - timedelta(hours=1),
        rows=[
            ("seed", 100, "gpu", "u", "PENDING"),
            ("a_gpu", 100, "gpu", "u", "PENDING"),
            ("a_cpu", 100, "cpu", "u", "PENDING"),  # different partition — ignored
        ],
    )
    rate = compute_arrival_rate_per_hour(tmp_path, now=_NOW, partition="gpu")
    assert rate is not None
    assert abs(rate - 1.0) < 0.1  # only a_gpu counts


def test_running_jobs_dont_count_as_arrivals(tmp_path: Path) -> None:
    """A job that was PENDING then went RUNNING isn't a new arrival —
    it was already in the queue."""
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=2), rows=[("a", 100, "gpu", "u", "PENDING")]
    )
    _write_snapshot(
        tmp_path, at=_NOW - timedelta(hours=1), rows=[("a", 100, "gpu", "u", "RUNNING")]
    )
    rate = compute_arrival_rate_per_hour(tmp_path, now=_NOW, partition="gpu")
    # No new arrivals; rate = 0.
    assert rate == 0.0
