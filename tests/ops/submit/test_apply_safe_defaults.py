"""Tests for the ``apply-safe-defaults`` verb (Surface 2).

The load-bearing assertion: apply-safe-defaults REFUSES to fill
``task_generator`` — it leaves a required-caller field unresolved, and a
tampered envelope carrying a safe_default on one raises ``spec_invalid``
(defense-in-depth behind the Ambiguity guard).
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.apply_safe_defaults import ApplySafeDefaultsInput
from hpc_agent.ops.submit.apply_safe_defaults import apply_safe_defaults


def _apply(**overrides: Any) -> dict[str, Any]:
    spec = ApplySafeDefaultsInput.model_validate(overrides)
    return apply_safe_defaults(spec=spec).model_dump(mode="json")


def test_fills_auto_resolvable_defaults() -> None:
    out = _apply(
        resolved={"experiment_dir": "/x"},
        ambiguities=[
            {"field": "cluster", "candidates": ["a", "b"], "depends_on": [], "safe_default": "a"},
            {
                "field": "data_axis",
                "candidates": None,
                "depends_on": ["entry_point"],
                "safe_default": {"kind": "sequential"},
            },
        ],
    )
    assert out["resolved"]["cluster"] == "a"
    assert out["resolved"]["data_axis"] == {"kind": "sequential"}
    assert out["applied"] == {"cluster": "a", "data_axis": {"kind": "sequential"}}
    assert out["still_unresolved"] == []
    assert out["all_resolved"] is True


def test_task_generator_left_unresolved_no_default() -> None:
    """A required-caller field with no safe_default stays for the caller."""
    out = _apply(
        ambiguities=[
            {"field": "task_generator", "candidates": None, "depends_on": []},
            {"field": "goal", "candidates": None, "depends_on": []},
        ],
    )
    assert "task_generator" not in out["resolved"]
    assert "goal" not in out["resolved"]
    assert set(out["still_unresolved"]) == {"task_generator", "goal"}
    assert out["all_resolved"] is False


def test_refuses_tampered_task_generator_safe_default() -> None:
    """Defense-in-depth: a safe_default on task_generator raises spec_invalid."""
    with pytest.raises(errors.SpecInvalid, match="task_generator"):
        _apply(
            ambiguities=[
                {
                    "field": "task_generator",
                    "candidates": None,
                    "depends_on": [],
                    "safe_default": {"kind": "items_x_seeds", "params": {"seeds": [0, 1]}},
                },
            ],
        )


def test_refuses_tampered_goal_safe_default() -> None:
    with pytest.raises(errors.SpecInvalid, match="not auto-resolvable"):
        _apply(
            ambiguities=[
                {"field": "goal", "candidates": None, "depends_on": [], "safe_default": "fabbed"},
            ],
        )


def test_uncovered_param_dict_default_applied() -> None:
    """{param: None} is a present slot — applied (uncovered_param is auto-resolvable)."""
    out = _apply(
        ambiguities=[
            {
                "field": "uncovered_param",
                "candidates": ["samples"],
                "depends_on": ["entry_point"],
                "safe_default": {"samples": 10000},
            },
        ],
    )
    assert out["resolved"]["uncovered_param"] == {"samples": 10000}
    assert out["all_resolved"] is True


def test_falsy_default_applied_on_auto_resolvable() -> None:
    """homogeneous_axes safe_default [] is present (falsy) and must be applied."""
    out = _apply(
        ambiguities=[
            {"field": "homogeneous_axes", "candidates": None, "depends_on": [], "safe_default": []},
        ],
    )
    assert out["resolved"]["homogeneous_axes"] == []
    assert out["applied"]["homogeneous_axes"] == []


def test_auto_resolvable_with_no_default_stays_unresolved() -> None:
    """A cluster ambiguity with no safe_default (none configured) stays for the caller."""
    out = _apply(
        ambiguities=[
            {"field": "cluster", "candidates": None, "depends_on": [], "safe_default": None}
        ],
    )
    assert out["still_unresolved"] == ["cluster"]
    assert out["all_resolved"] is False


def test_missing_field_key_raises() -> None:
    with pytest.raises(errors.SpecInvalid, match="missing a string 'field'"):
        _apply(ambiguities=[{"candidates": None}])


def test_chains_from_walk_output() -> None:
    """End-to-end shape compatibility: walk output → apply input with no reshaping."""
    walk_out = {
        "resolved": {"experiment_dir": "/x", "cluster": "hoffman2", "goal": "g"},
        "ambiguities": [
            {"field": "task_generator", "candidates": None, "depends_on": []},
            {
                "field": "data_axis",
                "candidates": None,
                "depends_on": ["entry_point"],
                "safe_default": {"kind": "sequential"},
            },
        ],
    }
    out = _apply(resolved=walk_out["resolved"], ambiguities=walk_out["ambiguities"])
    assert out["resolved"]["data_axis"] == {"kind": "sequential"}
    assert out["still_unresolved"] == ["task_generator"]
