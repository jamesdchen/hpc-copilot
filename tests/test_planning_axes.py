"""Tests for claude_hpc.planning.axes — cold-start axis picker."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import jsonschema
import pytest

from claude_hpc.planning.axes import (
    AXES_FILENAME,
    AXES_SCHEMA_VERSION,
    axes_path,
    compute_wave_map,
    pick_array_axis,
    pick_array_axis_warm,
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
        validate_axes({"axes_schema_version": 1, "homogeneous_axes": ["window", "window"]})


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


# ─── axes enumeration with cardinalities ────────────────────────────────────


def test_write_with_axes_enumeration(tmp_path: Path) -> None:
    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 4}, {"name": "window", "size": 20}],
        homogeneous_axes=["window"],
    )
    out = read_axes(tmp_path)
    assert out is not None
    assert out["axes"] == [
        {"name": "model", "size": 4},
        {"name": "window", "size": 20},
    ]
    assert out["homogeneous_axes"] == ["window"]


def test_homogeneous_must_be_subset_of_axes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="homogeneous_axes references"):
        write_axes(
            tmp_path,
            axes=[{"name": "model", "size": 4}],
            homogeneous_axes=["window"],  # not in axes
        )


def test_axes_schema_rejects_zero_size() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_axes(
            {
                "axes_schema_version": 1,
                "axes": [{"name": "model", "size": 0}],
            }
        )


# ─── compute_wave_map ───────────────────────────────────────────────────────


def test_wave_map_array_axis_is_last(tmp_path: Path) -> None:
    # axes order: [model(2), window(3)]; pick window (last) → 2 waves of 3.
    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}, {"name": "window", "size": 3}],
    )
    wave_map = compute_wave_map(tmp_path, picked_axis="window")
    assert wave_map == {0: [0, 1, 2], 1: [3, 4, 5]}


def test_wave_map_array_axis_is_first(tmp_path: Path) -> None:
    # axes order: [model(2), window(3)]; pick model (first) → 3 waves of 2,
    # task_ids stride by 3 (size of window).
    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}, {"name": "window", "size": 3}],
    )
    wave_map = compute_wave_map(tmp_path, picked_axis="model")
    # Wave 0 = (window=0): task_ids = [0, 3]
    # Wave 1 = (window=1): task_ids = [1, 4]
    # Wave 2 = (window=2): task_ids = [2, 5]
    assert wave_map == {0: [0, 3], 1: [1, 4], 2: [2, 5]}


def test_wave_map_three_axes_picks_middle(tmp_path: Path) -> None:
    # 4 * 3 * 20 = 240 total; pick data (middle, size 3); 4*20=80 waves of 3.
    write_axes(
        tmp_path,
        axes=[
            {"name": "model", "size": 4},
            {"name": "data", "size": 3},
            {"name": "window", "size": 20},
        ],
    )
    wave_map = compute_wave_map(tmp_path, picked_axis="data")
    assert len(wave_map) == 4 * 20
    for tids in wave_map.values():
        assert len(tids) == 3
    # Round-trip: every task_id 0..239 appears exactly once.
    seen: set[int] = set()
    for tids in wave_map.values():
        for tid in tids:
            assert tid not in seen, f"duplicate task_id {tid}"
            seen.add(tid)
    assert seen == set(range(240))


def test_wave_map_rejects_unknown_axis(tmp_path: Path) -> None:
    write_axes(
        tmp_path,
        axes=[{"name": "model", "size": 2}],
    )
    with pytest.raises(ValueError, match="not in axes"):
        compute_wave_map(tmp_path, picked_axis="nonexistent")


def test_wave_map_rejects_missing_yaml(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        compute_wave_map(tmp_path, picked_axis="anything")


def test_wave_map_rejects_no_axes_enumeration(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window"])
    with pytest.raises(ValueError, match="no 'axes' enumeration"):
        compute_wave_map(tmp_path, picked_axis="window")


# ─── pick_array_axis_warm (skeleton — inert until samples grow axis_bindings) ─


def test_warm_returns_none_without_axes_yaml(tmp_path: Path) -> None:
    name, reason = pick_array_axis_warm(tmp_path)
    assert name is None
    assert "no axes.yaml" in reason


def test_warm_returns_none_when_axes_yaml_lacks_enumeration(tmp_path: Path) -> None:
    write_axes(tmp_path, homogeneous_axes=["window"])
    name, reason = pick_array_axis_warm(tmp_path)
    assert name is None
    assert "no axes" in reason


def test_warm_returns_none_when_no_qualifying_samples(tmp_path: Path) -> None:
    write_axes(
        tmp_path,
        axes=[{"name": "window", "size": 5}],
    )
    with patch(
        "claude_hpc.state.runtime_prior.read_samples",
        return_value=[],
    ):
        name, reason = pick_array_axis_warm(tmp_path, min_samples=5)
    assert name is None
    assert "qualifying samples" in reason


def test_warm_picks_lowest_cv_when_samples_carry_axis_bindings(tmp_path: Path) -> None:
    # window is homogeneous (constant runtime); model is heterogeneous.
    write_axes(
        tmp_path,
        axes=[
            {"name": "model", "size": 3},
            {"name": "window", "size": 5},
        ],
    )
    samples = []
    # 5 windows for each model. Model A: 100s, Model B: 200s, Model C: 300s.
    # Within each model, all windows take the same time → CV=0 along window.
    # Holding window fixed, model varies wildly → CV high along model.
    for model, base in (("A", 100.0), ("B", 200.0), ("C", 300.0)):
        for w in range(5):
            samples.append(
                {
                    "elapsed_sec": base,
                    "exit_code": 0,
                    "axis_bindings": {"model": model, "window": w},
                }
            )
    with patch(
        "claude_hpc.state.runtime_prior.read_samples",
        return_value=samples,
    ):
        name, reason = pick_array_axis_warm(tmp_path, min_samples=5)
    assert name == "window"
    assert "lowest CV" in reason
