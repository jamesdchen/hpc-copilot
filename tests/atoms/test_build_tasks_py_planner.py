"""Planner-mode tests for ``build-tasks-py`` (the ``data_axis`` branch).

When the spec carries a ``data_axis``, ``build-tasks-py`` emits a
``hpc_agent.template.plan_tasks``-driven ``tasks.py`` — the deterministic
materialisation of the /submit-hpc Step 3 ``DataAxis`` inference.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._schema_models.actions.build_tasks_py import BuildTasksPyInput
from hpc_agent.atoms.build_tasks_py import build_tasks_py

if TYPE_CHECKING:
    from pathlib import Path


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_planner_independent_splits_into_chunks(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "alpha", "values": [0.1, 1.0]}],
            flags_by_executor={"src.exp": [{"name": "alpha", "type": "float"}]},
            data_axis={"kind": "independent", "chunks": 4, "series_length": 100},
        ),
    )
    assert out["n_tasks"] == 8  # 2 sweep points * 4 chunks
    mod = _load(tmp_path / ".hpc" / "tasks.py", "_plan_indep")
    assert mod.total() == 8
    assert mod.resolve(0) == {"alpha": 0.1, "start": 0, "end": 25, "halo": 0}
    # FLAGS gained the planner's --halo flag.
    assert any(f.name == "halo" for f in mod.FLAGS["src.exp"])


def test_planner_bounded_halo_renders_halo_fn(tmp_path: Path) -> None:
    build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "w", "values": [10]}],
            flags_by_executor={"src.exp": [{"name": "w", "type": "int"}]},
            data_axis={
                "kind": "bounded_halo",
                "chunks": 4,
                "series_length": 100,
                "halo_expr": "params['w'] * 2",
            },
        ),
    )
    mod = _load(tmp_path / ".hpc" / "tasks.py", "_plan_halo")
    assert mod.total() == 4
    # chunk 0 clamped to 0; w*2 == 20 on the rest.
    assert [mod.resolve(i)["halo"] for i in range(4)] == [0, 20, 20, 20]


def test_planner_sequential_ignores_chunks(tmp_path: Path) -> None:
    out = build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "seed", "values": [1, 2, 3]}],
            flags_by_executor={"src.exp": [{"name": "seed", "type": "int"}]},
            data_axis={"kind": "sequential", "chunks": 16, "series_length": 100},
        ),
    )
    assert out["n_tasks"] == 3  # one task per sweep point, no series split
    mod = _load(tmp_path / ".hpc" / "tasks.py", "_plan_seq")
    assert mod.resolve(0) == {"seed": 1, "start": 0, "end": 100, "halo": 0}


def test_planner_associative_carries_no_halo(tmp_path: Path) -> None:
    build_tasks_py(
        tmp_path,
        spec=BuildTasksPyInput(
            axes=[{"name": "alpha", "values": [1.0]}],
            flags_by_executor={"src.exp": [{"name": "alpha", "type": "float"}]},
            data_axis={
                "kind": "associative",
                "chunks": 3,
                "series_length": 60,
                "monoid": "moments",
            },
        ),
    )
    mod = _load(tmp_path / ".hpc" / "tasks.py", "_plan_assoc")
    assert mod.total() == 3
    assert all(mod.resolve(i)["halo"] == 0 for i in range(3))


def test_planner_bounded_halo_requires_halo_expr(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="halo_expr"):
        build_tasks_py(
            tmp_path,
            spec=BuildTasksPyInput(
                axes=[{"name": "a", "values": [1]}],
                flags_by_executor={"src.exp": [{"name": "a", "type": "int"}]},
                data_axis={"kind": "bounded_halo", "chunks": 2, "series_length": 10},
            ),
        )


def test_planner_rejects_non_arithmetic_halo_expr(tmp_path: Path) -> None:
    # A halo_expr is rendered verbatim into the generated tasks.py; a
    # call / import must be rejected at the spec boundary.
    with pytest.raises(errors.SpecInvalid, match="arithmetic"):
        build_tasks_py(
            tmp_path,
            spec=BuildTasksPyInput(
                axes=[{"name": "a", "values": [1]}],
                flags_by_executor={"src.exp": [{"name": "a", "type": "int"}]},
                data_axis={
                    "kind": "bounded_halo",
                    "chunks": 2,
                    "series_length": 10,
                    "halo_expr": "__import__('os').system('echo pwned')",
                },
            ),
        )
