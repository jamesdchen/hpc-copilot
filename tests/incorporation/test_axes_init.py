"""Tests for the axes-init primitive (atoms/axes_init.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.incorporation.axes_init import axes_init
from hpc_agent.state.axes import axes_path, read_axes

if TYPE_CHECKING:
    from pathlib import Path


def test_writes_when_absent(tmp_path: Path) -> None:
    out = axes_init(experiment_dir=tmp_path, homogeneous_axes=["window", "fold"])
    assert out["wrote"] is True
    assert out["homogeneous_axes"] == ["window", "fold"]
    assert out["axes_path"] == str(axes_path(tmp_path))
    persisted = read_axes(tmp_path)
    assert persisted is not None
    assert persisted["homogeneous_axes"] == ["window", "fold"]


def test_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    axes_init(experiment_dir=tmp_path, homogeneous_axes=["window"])
    out = axes_init(experiment_dir=tmp_path, homogeneous_axes=["fold"])
    assert out["wrote"] is False
    assert "already exists" in out["reason"]
    # Existing file unchanged.
    persisted = read_axes(tmp_path)
    assert persisted is not None
    assert persisted["homogeneous_axes"] == ["window"]


def test_force_overwrites(tmp_path: Path) -> None:
    axes_init(experiment_dir=tmp_path, homogeneous_axes=["window"])
    out = axes_init(experiment_dir=tmp_path, homogeneous_axes=["fold"], force=True)
    assert out["wrote"] is True
    persisted = read_axes(tmp_path)
    assert persisted is not None
    assert persisted["homogeneous_axes"] == ["fold"]


def test_minimal_axes_yaml(tmp_path: Path) -> None:
    out = axes_init(experiment_dir=tmp_path)
    assert out["wrote"] is True
    persisted = read_axes(tmp_path)
    assert persisted is not None
    assert "homogeneous_axes" not in persisted


def test_empty_list_writes_no_homogeneous(tmp_path: Path) -> None:
    out = axes_init(experiment_dir=tmp_path, homogeneous_axes=[])
    assert out["wrote"] is True
    persisted = read_axes(tmp_path)
    assert persisted is not None
    # Empty list is not the same as omitted; both behave the same downstream
    # (cold-start picker returns None, "homogeneous_axes is empty"), but we
    # round-trip the explicit empty so the file shows the agent's intent.
    assert persisted.get("homogeneous_axes", []) == []
