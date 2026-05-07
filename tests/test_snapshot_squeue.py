"""Tests for ``scripts.snapshot_squeue.write_snapshot``.

The SSH-bound paths aren't tested here (covered indirectly by
``infra.remote.ssh_run`` tests). We exercise the persistence layer
with a real ``tmp_path`` directory.
"""

from __future__ import annotations

import gzip
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

# Load the script as a module — scripts/ isn't on the package path.
_SPEC = importlib.util.spec_from_file_location(
    "_snapshot_squeue",
    Path(__file__).resolve().parent.parent / "scripts" / "snapshot_squeue.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_write_snapshot_creates_gzipped_file_with_timestamp_name(tmp_path: Path) -> None:
    text = (
        "JOBID|PRIORITY|PARTITION|USER|STATE|TIME_LEFT|TIME_LIMIT\n"
        "1|100|gpu|alice|PENDING|N/A|3600\n"
    )
    at = datetime(2026, 9, 22, 10, 30, 0, tzinfo=timezone.utc)
    path = _MOD.write_snapshot(text=text, experiment_dir=tmp_path, at=at)
    assert path.name == "20260922T103000.tsv.gz"
    assert path.parent == tmp_path / ".hpc" / "squeue_snapshots"


def test_snapshot_contents_round_trip_through_gzip(tmp_path: Path) -> None:
    text = "header|line\nrow|data\n"
    at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    path = _MOD.write_snapshot(text=text, experiment_dir=tmp_path, at=at)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        round_tripped = f.read()
    assert round_tripped == text


def test_default_at_uses_now_utc(tmp_path: Path) -> None:
    """When *at* is omitted, the writer uses the current UTC time."""
    text = "x\n"
    path = _MOD.write_snapshot(text=text, experiment_dir=tmp_path)
    assert path.suffix == ".gz"
    assert path.parent.name == "squeue_snapshots"


def test_build_squeue_command_includes_required_columns() -> None:
    """Pin the column set — reordering would silently break the
    parser's name-keyed lookup, but adding columns is fine."""
    cmd = _MOD._build_squeue_command()
    for col in ("JOBID", "PRIORITY", "PARTITION", "USERNAME", "STATE", "TIME_LEFT", "TIME_LIMIT"):
        assert col in cmd
