"""Tests for ``claude_hpc.atoms.build_tasks_py``.

The primitive scaffolds ``.hpc/tasks.py`` from a cartesian-product
axes spec. We test:

  * the rendered file is syntactically valid Python
  * the rendered file imports correctly and exposes total() / resolve()
  * total() == cartesian product cardinality
  * resolve(i) returns the right kwargs
  * single-axis and multi-axis paths both work
  * refuse-without-force survives, force overwrites
  * malformed input surfaces SpecInvalid
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING, Any

from claude_hpc._schema_models.actions.build_tasks_py import BuildTasksPyInput
from claude_hpc.atoms.build_tasks_py import build_tasks_py

if TYPE_CHECKING:
    from pathlib import Path


def _load(path: Path, name: str = "_test_tasks") -> Any:
    """Load the rendered tasks.py as a module so we can call total()/resolve()."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_single_axis_renders_simple_comprehension(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "horizon", "values": [1, 5, 10]}],
            flags_by_executor={
                "src.ml_ridge": [
                    {"name": "horizon", "type": "int", "default": 1},
                ]
            },
        ),
    )
    assert out["wrote"] is True
    assert out["n_tasks"] == 3
    mod = _load(tmp_path / ".hpc" / "tasks.py", name="_t_single")
    assert mod.total() == 3
    assert mod.resolve(0) == {"horizon": 1}
    assert mod.resolve(1) == {"horizon": 5}
    assert mod.resolve(2) == {"horizon": 10}


def test_multi_axis_uses_itertools_product(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[
                {"name": "horizon", "values": [1, 5]},
                {"name": "seed", "values": [42, 1337]},
            ],
            flags_by_executor={
                "src.ml_ridge": [
                    {"name": "horizon", "type": "int", "default": 1},
                    {"name": "seed", "type": "int", "default": 42},
                ]
            },
        ),
    )
    assert out["n_tasks"] == 4
    mod = _load(tmp_path / ".hpc" / "tasks.py", name="_t_multi")
    assert mod.total() == 4
    # itertools.product order: leftmost varies slowest.
    assert mod.resolve(0) == {"horizon": 1, "seed": 42}
    assert mod.resolve(1) == {"horizon": 1, "seed": 1337}
    assert mod.resolve(2) == {"horizon": 5, "seed": 42}
    assert mod.resolve(3) == {"horizon": 5, "seed": 1337}


def test_three_axis_cardinality_round_trips(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[
                {"name": "model", "values": ["lgbm", "xgb"]},
                {"name": "horizon", "values": [1, 5, 25]},
                {"name": "seed", "values": [42, 1337, 31337, 2718]},
            ],
            flags_by_executor={"src.ml_ridge": [{"name": "model", "type": "str"}]},
        ),
    )
    assert out["n_tasks"] == 24
    mod = _load(tmp_path / ".hpc" / "tasks.py", name="_t_three")
    assert mod.total() == 24
    seen = {tuple(sorted(mod.resolve(i).items())) for i in range(24)}
    assert len(seen) == 24  # every combination unique


def test_string_values_render_as_quoted(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "model", "values": ["lgbm", "xgb_dart", "catboost"]}],
            flags_by_executor={"src.ml": [{"name": "model", "type": "str"}]},
        ),
    )
    assert out["n_tasks"] == 3
    mod = _load(tmp_path / ".hpc" / "tasks.py", name="_t_str")
    assert mod.resolve(0) == {"model": "lgbm"}
    assert mod.resolve(2) == {"model": "catboost"}


def test_flags_block_includes_default_when_present(tmp_path: Path) -> None:
    build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "x", "values": [1]}],
            flags_by_executor={
                "src.ml": [
                    {"name": "alpha", "type": "float", "default": 0.5},
                    {"name": "verbose", "type": "bool"},
                ]
            },
        ),
    )
    src = (tmp_path / ".hpc" / "tasks.py").read_text()
    assert "flag('alpha', float, default=0.5)" in src
    assert "flag('verbose', bool)" in src
    # No default rendered for verbose.
    assert "flag('verbose', bool, default" not in src


def test_refuses_overwrite_without_force(tmp_path: Path) -> None:
    build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "x", "values": [1]}],
            flags_by_executor={"src.ml": [{"name": "x", "type": "int"}]},
        ),
    )
    # Hand-edit the file to simulate the user's Pattern 2/3 conversion.
    target = tmp_path / ".hpc" / "tasks.py"
    target.write_text("# user's hand-edited version\n_TASKS = []\n")
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "x", "values": [1, 2, 3]}],  # different cardinality
            flags_by_executor={"src.ml": [{"name": "x", "type": "int"}]},
        ),
    )
    assert out["wrote"] is False
    assert "force=true" in out["reason"]
    # File still has the user's edit.
    assert "user's hand-edited version" in target.read_text()


def test_force_overwrites(tmp_path: Path) -> None:
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# stale\n")
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "x", "values": [1, 2]}],
            flags_by_executor={"src.ml": [{"name": "x", "type": "int"}]},
            force=True,
        ),
    )
    assert out["wrote"] is True
    assert "stale" not in target.read_text()


def test_multi_executor_flags_block_includes_each(tmp_path: Path) -> None:
    build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "x", "values": [1]}],
            flags_by_executor={
                "src.ml_ridge": [{"name": "alpha", "type": "float", "default": 1.0}],
                "src.dl_patchts": [{"name": "horizon", "type": "int", "default": 1}],
            },
        ),
    )
    src = (tmp_path / ".hpc" / "tasks.py").read_text()
    assert "'src.ml_ridge'" in src
    assert "'src.dl_patchts'" in src
    assert "flag('alpha', float, default=1.0)" in src
    assert "flag('horizon', int, default=1)" in src
