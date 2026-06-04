"""Tests for the ``inspect-parallel-axes`` composite primitive (WS5 #7).

Pins the pure-query collapse of the build-executor / axes-init
companion's multi-``Read``: one call surfaces ``.hpc/axes.yaml``'s parsed
axes / homogeneous_axes / executors AND ``.hpc/tasks.py``'s raw body so
the agent can classify parallel dimensions without a manual file walk.

Real filesystem (tmp_path) throughout — the verb only reads files and
executes nothing, so there's no subprocess / cluster surface to mock.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.ops.inspect_parallel_axes import inspect_parallel_axes
from hpc_agent.state.axes import write_axes


def _hpc(tmp_path: Path) -> Path:
    d = tmp_path / ".hpc"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestAbsent:
    """A fresh experiment with no .hpc artifacts degrades to empty, not error."""

    def test_no_hpc_dir_yields_empty_summary(self, tmp_path: Path) -> None:
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["axes_yaml_present"] is False
        assert result["axes_yaml_error"] is None
        assert result["axes"] == []
        assert result["homogeneous_axes"] == []
        assert result["executors"] == {}
        assert result["tasks_py_present"] is False
        assert result["tasks_py_body"] == ""

    def test_paths_are_reported_even_when_absent(self, tmp_path: Path) -> None:
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["axes_yaml_path"] == str(tmp_path / ".hpc" / "axes.yaml")
        assert result["tasks_py_path"] == str(tmp_path / ".hpc" / "tasks.py")
        assert result["experiment_dir"] == str(tmp_path)


class TestAxesYaml:
    """The axes.yaml half: parsed axes / homogeneous_axes / executors."""

    def test_reports_axes_and_homogeneous(self, tmp_path: Path) -> None:
        write_axes(
            tmp_path,
            axes=[{"name": "model", "size": 4}, {"name": "seed", "size": 3}],
            homogeneous_axes=["seed"],
        )
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["axes_yaml_present"] is True
        assert result["axes"] == [
            {"name": "model", "size": 4},
            {"name": "seed", "size": 3},
        ]
        assert result["homogeneous_axes"] == ["seed"]

    def test_reports_executors_block(self, tmp_path: Path) -> None:
        entry = {
            "run_signature_sha": "abc123",
            "data_axis": {"kind": "independent"},
            "classified_by": "agent",
            "classified_at": "2026-06-04T00:00:00Z",
        }
        write_axes(
            tmp_path,
            axes=[{"name": "seed", "size": 2}],
            homogeneous_axes=["seed"],
            executors={"train": entry},
        )
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["executors"] == {"train": entry}

    def test_corrupt_axes_yaml_surfaces_as_error_not_raise(self, tmp_path: Path) -> None:
        # A non-mapping top-level YAML violates the schema; read_axes raises,
        # and the verb must catch it so the tasks.py half still returns.
        (_hpc(tmp_path) / "axes.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        (tmp_path / ".hpc" / "tasks.py").write_text("def total(): return 1\n", encoding="utf-8")
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["axes_yaml_present"] is True
        assert result["axes_yaml_error"] is not None
        assert result["axes"] == []
        # tasks.py half survives the axes.yaml corruption.
        assert result["tasks_py_present"] is True
        assert "def total()" in result["tasks_py_body"]


class TestTasksPy:
    """The tasks.py half: raw body for axis classification."""

    def test_returns_raw_body(self, tmp_path: Path) -> None:
        body = 'FLAGS = {"src.ml": []}\ndef resolve(i):\n    return {"seed": i}\n'
        (_hpc(tmp_path) / "tasks.py").write_text(body, encoding="utf-8")
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert result["tasks_py_present"] is True
        assert result["tasks_py_body"] == body

    def test_body_is_tail_capped(self, tmp_path: Path) -> None:
        from hpc_agent.ops import inspect_parallel_axes as mod

        big = "# pad\n" + ("x = 1\n" * 5000)
        (_hpc(tmp_path) / "tasks.py").write_text(big, encoding="utf-8")
        result = inspect_parallel_axes(experiment_dir=tmp_path)
        assert len(result["tasks_py_body"]) == mod._TASKS_TEXT_CHARS

    def test_str_and_path_experiment_dir(self, tmp_path: Path) -> None:
        write_axes(tmp_path, axes=[{"name": "a", "size": 1}], homogeneous_axes=["a"])
        from_str = inspect_parallel_axes(experiment_dir=str(tmp_path))
        from_path = inspect_parallel_axes(experiment_dir=tmp_path)
        assert from_str["axes"] == from_path["axes"] == [{"name": "a", "size": 1}]
