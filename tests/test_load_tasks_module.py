"""Tests for ``hpc_agent.load_tasks_module`` — local import of a user's
``.hpc/tasks.py`` with the experiment repo root on ``sys.path``.

BUG 1 regression: ``load_tasks_module`` must put the experiment dir (and
``.hpc/``) on ``sys.path`` for the duration of ``exec_module`` so a
``tasks.py`` that does ``import my_root_module`` / ``from src.x import y``
(modules living at the experiment root) resolves during LOCAL enumeration
exactly as it does on the cluster — where the job script exports
``PYTHONPATH="$REPO_DIR:$REPO_DIR/.hpc"``. Before the fix this raised
``ModuleNotFoundError`` locally even though it imported fine on the cluster.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

import hpc_agent

if TYPE_CHECKING:
    from pathlib import Path


_SIBLING_MODULE = """\
ANSWER = 42


def helper():
    return ANSWER
"""

# A tasks.py that imports a module living at the EXPERIMENT ROOT (sibling of
# ``.hpc/``). This is the exact shape BUG 1 broke: the import is invisible to
# importlib unless the experiment root is on ``sys.path``.
_TASKS_PY_IMPORTING_ROOT = """\
import my_root_module


def total():
    return my_root_module.ANSWER


def resolve(i):
    return {"value": my_root_module.helper(), "task_id": i}
"""

# A tasks.py importing from a ``src/`` package at the experiment root —
# the ``from src.x import y`` variant from the bug report.
_SRC_PKG_TASKS_PY = """\
from src.knobs import KNOB


def total():
    return KNOB


def resolve(i):
    return {"knob": KNOB, "task_id": i}
"""


def _layout(experiment_dir: Path) -> Path:
    """Create ``<experiment_dir>/.hpc/`` and return the ``tasks.py`` path."""
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    return hpc / "tasks.py"


def test_imports_sibling_module_at_experiment_root(tmp_path: Path) -> None:
    """A tasks.py importing a repo-root sibling loads during local
    enumeration (the cluster already resolves this via PYTHONPATH)."""
    (tmp_path / "my_root_module.py").write_text(_SIBLING_MODULE, encoding="utf-8")
    tasks_py = _layout(tmp_path)
    tasks_py.write_text(_TASKS_PY_IMPORTING_ROOT, encoding="utf-8")

    module = hpc_agent.load_tasks_module(tasks_py)

    assert module.total() == 42
    assert module.resolve(0) == {"value": 42, "task_id": 0}


def test_imports_from_src_package_at_experiment_root(tmp_path: Path) -> None:
    """The ``from src.x import y`` variant: a package rooted at the
    experiment dir resolves too (root, not just ``.hpc/``, is on the path)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "knobs.py").write_text("KNOB = 7\n", encoding="utf-8")
    tasks_py = _layout(tmp_path)
    tasks_py.write_text(_SRC_PKG_TASKS_PY, encoding="utf-8")

    module = hpc_agent.load_tasks_module(tasks_py)

    assert module.total() == 7
    assert module.resolve(3) == {"knob": 7, "task_id": 3}


def test_sys_path_restored_after_successful_load(tmp_path: Path) -> None:
    """The experiment-root entries are transient: ``sys.path`` is identical
    before and after a successful load — no leaked entries in the host."""
    (tmp_path / "my_root_module.py").write_text(_SIBLING_MODULE, encoding="utf-8")
    tasks_py = _layout(tmp_path)
    tasks_py.write_text(_TASKS_PY_IMPORTING_ROOT, encoding="utf-8")

    before = list(sys.path)
    hpc_agent.load_tasks_module(tasks_py)

    assert sys.path == before
    assert str(tmp_path.resolve()) not in sys.path
    assert str((tmp_path / ".hpc").resolve()) not in sys.path


def test_sys_path_restored_after_failed_load(tmp_path: Path) -> None:
    """Restoration is via try/finally — a tasks.py that raises at import
    time still leaves ``sys.path`` clean."""
    tasks_py = _layout(tmp_path)
    tasks_py.write_text("import a_module_that_does_not_exist\n", encoding="utf-8")

    before = list(sys.path)
    with pytest.raises(ModuleNotFoundError):
        hpc_agent.load_tasks_module(tasks_py)

    assert sys.path == before


def test_no_duplicate_sys_path_entry_when_already_present(tmp_path: Path) -> None:
    """If the experiment root is already on ``sys.path`` (e.g. the caller's
    cwd), the load doesn't double-insert it, and the path is still restored
    to exactly its prior contents (including that pre-existing entry)."""
    (tmp_path / "my_root_module.py").write_text(_SIBLING_MODULE, encoding="utf-8")
    tasks_py = _layout(tmp_path)
    tasks_py.write_text(_TASKS_PY_IMPORTING_ROOT, encoding="utf-8")

    exp_root = str(tmp_path.resolve())
    sys.path.insert(0, exp_root)
    try:
        before = list(sys.path)
        hpc_agent.load_tasks_module(tasks_py)
        assert sys.path == before
        assert sys.path.count(exp_root) == 1
    finally:
        sys.path.remove(exp_root)
