"""Tests for ``hpc_agent.incorporation.template.axis_config`` and the v2 axes schema."""

from __future__ import annotations

import jsonschema
import pytest

from hpc_agent._schema_models.fixtures.axes import _DataAxisConfig
from hpc_agent.incorporation.template.axis import (
    MOMENTS,
    SUM,
    Associative,
    BoundedHalo,
    Independent,
    Sequential,
)
from hpc_agent.incorporation.template.axis_config import (
    HaloExprError,
    config_from_data_axis,
    data_axis_from_config,
    eval_halo_expr,
)
from hpc_agent.state.axes import validate_axes

# ─── round-trip ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cfg",
    [
        {"kind": "independent"},
        {"kind": "sequential"},
        {"kind": "associative", "monoid": "sum"},
        {"kind": "associative", "monoid": "moments"},
        {"kind": "bounded_halo", "halo": {"expr": "train_window * 48"}},
    ],
)
def test_config_axis_config_round_trip(cfg: dict) -> None:
    """config -> live DataAxis -> config is the identity for all four kinds."""
    axis = data_axis_from_config(cfg)
    assert config_from_data_axis(axis) == cfg


def test_data_axis_from_config_builds_right_types() -> None:
    assert isinstance(data_axis_from_config({"kind": "independent"}), Independent)
    assert isinstance(data_axis_from_config({"kind": "sequential"}), Sequential)
    assoc = data_axis_from_config({"kind": "associative", "monoid": "sum"})
    assert isinstance(assoc, Associative) and assoc.monoid is SUM
    assert data_axis_from_config({"kind": "associative"}).monoid is MOMENTS
    bh = data_axis_from_config({"kind": "bounded_halo", "halo": {"expr": "w * 2"}})
    assert isinstance(bh, BoundedHalo)
    assert bh.halo_fn({"w": 10}) == 20


def test_config_from_data_axis_rejects_opaque_bounded_halo() -> None:
    """A hand-built BoundedHalo carries no source expr — cannot serialize."""
    with pytest.raises(ValueError, match="halo_expr"):
        config_from_data_axis(BoundedHalo(lambda p: 5))


# ─── safe halo evaluator ─────────────────────────────────────────────────


def test_eval_halo_expr_arithmetic() -> None:
    assert eval_halo_expr("train_window * 48", {"train_window": 30}) == 1440
    assert eval_halo_expr("a + b - 1", {"a": 10, "b": 5}) == 14
    assert eval_halo_expr("max(a, 96)", {"a": 10}) == 96
    assert eval_halo_expr("min(a, 96)", {"a": 10}) == 10
    assert eval_halo_expr("n // 2", {"n": 7}) == 3


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('x')",  # call to non-min/max
        "obj.attr",  # attribute access
        "open('/etc/passwd')",  # arbitrary call
        "sum([1, 2])",  # call other than min/max
        "a ** 2",  # disallowed operator (Pow)
        "a / b",  # true division, not FloorDiv
        "[x for x in range(3)]",  # comprehension
        "(lambda: 1)()",  # lambda
        "a if b else 0",  # conditional expression
    ],
)
def test_eval_halo_expr_rejects_unsafe(expr: str) -> None:
    with pytest.raises(HaloExprError):
        eval_halo_expr(expr, {"a": 4, "b": 2})


def test_eval_halo_expr_rejects_unknown_name() -> None:
    with pytest.raises(HaloExprError, match="not a run"):
        eval_halo_expr("missing_param * 2", {"train_window": 30})


def test_eval_halo_expr_rejects_divide_by_zero() -> None:
    with pytest.raises(HaloExprError, match="zero"):
        eval_halo_expr("n // 0", {"n": 10})


# ─── schema v1/v2 compatibility ──────────────────────────────────────────


def test_v1_axes_yaml_validates_under_v2_schema() -> None:
    """Every existing v1 axes.yaml must still validate (additive bump)."""
    validate_axes({"axes_schema_version": 1})
    validate_axes({"axes_schema_version": 1, "homogeneous_axes": ["window"]})
    validate_axes(
        {
            "axes_schema_version": 1,
            "axes": [{"name": "window", "size": 20}],
            "homogeneous_axes": ["window"],
        }
    )


def test_v2_executors_block_validates() -> None:
    validate_axes(
        {
            "axes_schema_version": 2,
            "executors": {
                "run": {
                    "run_signature_sha": "abc123",
                    "data_axis": {"kind": "bounded_halo", "halo": {"expr": "w * 2"}},
                    "classified_by": "interview",
                    "classified_at": "2026-05-21T00:00:00+00:00",
                }
            },
        }
    )


def test_v2_schema_rejects_unknown_version() -> None:
    with pytest.raises(jsonschema.ValidationError):
        validate_axes({"axes_schema_version": 3})


def test_data_axis_config_model_rejects_bounded_halo_without_halo() -> None:
    """The cross-field rule is enforced at the Pydantic layer."""
    with pytest.raises(ValueError, match="bounded_halo"):
        _DataAxisConfig.model_validate({"kind": "bounded_halo"})


def test_data_axis_config_model_rejects_halo_on_non_bounded() -> None:
    with pytest.raises(ValueError, match="halo"):
        _DataAxisConfig.model_validate({"kind": "independent", "halo": {"expr": "w"}})
