"""Unit tests for the shared executor-module import helper (#178).

The helper mirrors the cluster's ``$REPO_DIR``-on-``PYTHONPATH`` context so an
executor module that imports on the cluster also imports during local intake,
without leaking the experiment dir onto the long-lived agent's ``sys.path``.
"""

from __future__ import annotations

import sys

import pytest

from hpc_agent.infra.executor_import import import_executor_module


@pytest.fixture(autouse=True)
def _restore_import_state():
    """Snapshot/restore sys.path + sys.modules so imports don't leak across tests."""
    path_snapshot = list(sys.path)
    module_snapshot = set(sys.modules)
    yield
    sys.path[:] = path_snapshot
    for mod in set(sys.modules) - module_snapshot:
        sys.modules.pop(mod, None)


def _write_ns_module(tmp_path, body: str = "VALUE = 7\n"):
    """Write a PEP 420 namespace-package module (no __init__.py) — the exact
    shape ``import executors.X`` resolves on the cluster."""
    pkg = tmp_path / "executors"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "ridge.py").write_text(body)


def test_imports_namespace_package_module_via_repo_dir(tmp_path) -> None:
    _write_ns_module(tmp_path)
    assert str(tmp_path.resolve()) not in sys.path  # not importable yet (the bug)
    mod = import_executor_module("executors.ridge", tmp_path)
    assert mod.VALUE == 7


def test_does_not_leak_repo_dir_on_sys_path(tmp_path) -> None:
    _write_ns_module(tmp_path)
    import_executor_module("executors.ridge", tmp_path)
    # The helper removes the entry it inserted; the agent process stays clean.
    assert str(tmp_path.resolve()) not in sys.path


def test_preexisting_path_entry_is_left_in_place(tmp_path) -> None:
    """If repo_dir is already on sys.path, the helper must NOT remove it."""
    _write_ns_module(tmp_path)
    sys.path.insert(0, str(tmp_path.resolve()))
    import_executor_module("executors.ridge", tmp_path)
    assert str(tmp_path.resolve()) in sys.path  # we didn't add it → don't drop it


def test_genuine_missing_module_still_raises(tmp_path) -> None:
    with pytest.raises(ModuleNotFoundError):
        import_executor_module("executors.nope", tmp_path)


def test_real_import_error_inside_module_propagates(tmp_path) -> None:
    _write_ns_module(tmp_path, body="import a_module_that_does_not_exist_xyz\n")
    with pytest.raises(ModuleNotFoundError):
        import_executor_module("executors.ridge", tmp_path)
