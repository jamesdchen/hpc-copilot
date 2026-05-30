"""Tests for ``hpc_agent.incorporation.build.compute_run_id`` — pure
run_id derivation from ``.hpc/tasks.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.incorporation.build.compute_run_id import compute_run_id

if TYPE_CHECKING:
    from pathlib import Path


_MINIMAL_TASKS_PY = """\
def total():
    return 2


def resolve(i):
    return {"seed": i}
"""


def _write_tasks_py(experiment_dir: Path, body: str = _MINIMAL_TASKS_PY) -> None:
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text(body, encoding="utf-8")


def test_happy_path_returns_deterministic_run_id(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path)

    out = compute_run_id(tmp_path, run_name="myrun")

    assert set(out.keys()) == {"run_id", "cmd_sha"}
    assert len(out["cmd_sha"]) == 64
    assert all(c in "0123456789abcdef" for c in out["cmd_sha"])
    assert out["run_id"] == f"myrun-{out['cmd_sha'][:8]}"
    assert out["run_id"].startswith("myrun-")


def test_determinism_same_tasks_same_output(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write_tasks_py(repo_a)
    _write_tasks_py(repo_b)

    out_a = compute_run_id(repo_a, run_name="exp")
    out_b = compute_run_id(repo_b, run_name="exp")

    assert out_a == out_b


def test_missing_tasks_py_raises_spec_invalid(tmp_path: Path) -> None:
    # No .hpc/tasks.py created.
    with pytest.raises(errors.SpecInvalid) as excinfo:
        compute_run_id(tmp_path, run_name="myrun")
    msg = str(excinfo.value)
    assert ".hpc/tasks.py not found" in msg
    assert "/wrap-entry-point" in msg


def test_bad_run_name_with_space_raises_spec_invalid(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path)
    with pytest.raises(errors.SpecInvalid) as excinfo:
        compute_run_id(tmp_path, run_name="foo bar")
    assert "invalid --run-name" in str(excinfo.value)


def test_bad_run_name_with_slash_raises_spec_invalid(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path)
    with pytest.raises(errors.SpecInvalid):
        compute_run_id(tmp_path, run_name="foo/bar")
