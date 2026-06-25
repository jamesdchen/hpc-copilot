"""Tests for the generator-version stamp + staleness check (#364).

Covers the core :mod:`hpc_agent.incorporation.build.scaffold_meta` logic:
the stamp round-trip, the no-op fast path (stamp matches → no scan), the
import-resolution scan (vanished module / removed symbol), the
stale-by-construction legacy ``_build_tasks.py`` artifact, and the
"unknown generator → verify, don't refuse" rule for an unstamped scaffold
whose imports all resolve.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import hpc_agent
from hpc_agent.incorporation.build.scaffold_meta import (
    check_scaffold_staleness,
    read_scaffold_meta,
    scaffold_meta_path,
    stamp_scaffold_meta,
)

if TYPE_CHECKING:
    from pathlib import Path

_GOOD_IMPORT = "from hpc_agent.executor_cli import flag, generic_args\n"
_VANISHED_MODULE = "from hpc_agent.template import register_run, save_artifact\n"
_REMOVED_SYMBOL = "from hpc_agent.executor_cli import flag, definitely_not_a_real_symbol\n"


def _hpc(tmp_path: Path) -> Path:
    d = tmp_path / ".hpc"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── stamp round-trip ──────────────────────────────────────────────────


def test_stamp_round_trip(tmp_path: Path) -> None:
    _hpc(tmp_path)
    path = stamp_scaffold_meta(tmp_path, scaffold_files=["tasks.py", "cli.py"])
    assert path == scaffold_meta_path(tmp_path)
    meta = read_scaffold_meta(tmp_path)
    assert meta is not None
    assert meta["generator_version"] == hpc_agent.__version__
    assert meta["scaffold_files"] == ["cli.py", "tasks.py"]  # sorted
    assert meta["schema_version"] >= 1


def test_read_scaffold_meta_absent_is_none(tmp_path: Path) -> None:
    _hpc(tmp_path)
    assert read_scaffold_meta(tmp_path) is None


def test_read_scaffold_meta_corrupt_is_none(tmp_path: Path) -> None:
    _hpc(tmp_path)
    scaffold_meta_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert read_scaffold_meta(tmp_path) is None


# ─── no-op fast path ───────────────────────────────────────────────────


def test_matching_stamp_is_fresh_noop(tmp_path: Path) -> None:
    """Stamp == installed version → fresh with NO import scan (byte-identical
    to pre-#364: scanned_files is empty even though tasks.py is on disk)."""
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_GOOD_IMPORT)
    stamp_scaffold_meta(tmp_path, scaffold_files=["tasks.py"])
    result = check_scaffold_staleness(tmp_path)
    assert result.status == "fresh"
    assert result.stale is False
    assert result.scanned_files == []  # the fast path skipped the scan entirely


def test_no_hpc_dir_is_fresh(tmp_path: Path) -> None:
    result = check_scaffold_staleness(tmp_path)
    assert result.status == "fresh"
    assert result.stale is False


# ─── import-resolution scan ────────────────────────────────────────────


def test_vanished_module_is_stale(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_VANISHED_MODULE)  # unstamped → scanned
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is True
    assert result.status == "stale"
    assert any(p["missing"] == "module" for p in result.unresolved_imports)
    assert result.unresolved_imports[0]["file"] == ".hpc/tasks.py"


def test_removed_symbol_is_stale(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_REMOVED_SYMBOL)
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is True
    assert any(p.get("missing") == "symbol" for p in result.unresolved_imports)


def test_unstamped_but_resolvable_is_not_stale(tmp_path: Path) -> None:
    """Unstamped (legacy) scaffold whose imports all resolve → 'unknown
    generator → verify, don't refuse'. NOT flagged stale."""
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_GOOD_IMPORT)
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is False
    assert result.status == "fresh"
    assert result.stamp_version is None
    assert "tasks.py" in result.scanned_files  # it WAS scanned (no stamp to trust)


def test_version_mismatch_alone_is_not_stale(tmp_path: Path) -> None:
    """A stamp from a different version whose imports still resolve is NOT
    stale — mismatch alone never refuses; only a broken import does."""
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_GOOD_IMPORT)
    scaffold_meta_path(tmp_path).write_text(
        json.dumps(
            {"schema_version": 1, "generator_version": "0.0.1-ancient", "scaffold_files": []}
        ),
        encoding="utf-8",
    )
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is False
    assert result.stamp_version == "0.0.1-ancient"


# ─── legacy artifact: stale by construction ────────────────────────────


def test_legacy_build_tasks_is_stale_by_construction(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "_build_tasks.py").write_text(_VANISHED_MODULE)
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is True
    assert "_build_tasks.py" in result.legacy_artifacts


def test_legacy_artifact_stale_even_with_matching_stamp(tmp_path: Path) -> None:
    """A pre-reorg ``_build_tasks.py`` is stale even when a fresh stamp is
    present — the legacy file is dead-by-construction, not trusted away."""
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text(_GOOD_IMPORT)
    (hpc / "_build_tasks.py").write_text(_GOOD_IMPORT)  # legacy name, resolvable import
    stamp_scaffold_meta(tmp_path, scaffold_files=["tasks.py"])
    result = check_scaffold_staleness(tmp_path)
    assert result.stale is True
    assert "_build_tasks.py" in result.legacy_artifacts
