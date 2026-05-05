"""Tests for claude_hpc.planning.axes — cold-start axis picker."""

from __future__ import annotations

from typing import TYPE_CHECKING

import jsonschema
import pytest

from claude_hpc.planning.axes import (
    AXES_FILENAME,
    AXES_SCHEMA_VERSION,
    axes_path,
    pick_array_axis,
    read_axes,
    validate_axes,
    write_axes,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_axes_path_lives_under_dot_hpc(tmp_path: Path) -> None:
    assert axes_path(tmp_path) == tmp_path / ".hpc" / AXES_FILENAME


def test_write_round_trips(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window", "fold"])
    out = read_axes(tmp_path)
    assert out is not None
    assert out["axes_schema_version"] == AXES_SCHEMA_VERSION
    assert out["homogeneous_axes"] == ["window", "fold"]


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_axes(tmp_path) is None


def test_minimal_axes_validates(tmp_path: Path) -> None:
    write_axes(tmp_path)
    out = read_axes(tmp_path)
    assert out is not None
    assert "homogeneous_axes" not in out


def test_unknown_field_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_axes({"axes_schema_version": 1, "weird_field": "x"})


def test_duplicate_homogeneous_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_axes(
            {"axes_schema_version": 1, "homogeneous_axes": ["window", "window"]}
        )


def test_empty_string_axis_rejected() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_axes({"axes_schema_version": 1, "homogeneous_axes": [""]})


# ─── pick_array_axis ──────────────────────────────────────────────────────


def test_pick_returns_none_when_no_yaml(tmp_path: Path) -> None:
    name, reason = pick_array_axis(tmp_path)
    assert name is None
    assert "no axes.yaml" in reason


def test_pick_returns_none_when_homogeneous_empty(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=[])
    name, reason = pick_array_axis(tmp_path)
    assert name is None
    assert "empty" in reason


def test_pick_returns_first_homogeneous_without_available_filter(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window", "fold"])
    name, _ = pick_array_axis(tmp_path)
    assert name == "window"


def test_pick_filters_to_available_axes(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window", "fold"])
    name, reason = pick_array_axis(tmp_path, available_axes=["fold", "model"])
    assert name == "fold"
    assert "fold" in reason


def test_pick_returns_none_when_no_overlap(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window"])
    name, reason = pick_array_axis(tmp_path, available_axes=["fold", "model"])
    assert name is None
    assert "no homogeneous_axes" in reason


def test_pick_handles_missing_homogeneous_axes_key(tmp_path: Path) -> None:
    # File exists but has no homogeneous_axes key — equivalent to empty.
    write_axes(tmp_path)
    name, reason = pick_array_axis(tmp_path, available_axes=["fold"])
    assert name is None
    assert "empty" in reason
