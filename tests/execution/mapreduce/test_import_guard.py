"""Tests for the cluster-side import guard (issue #159)."""

from __future__ import annotations

import os

import pytest

from hpc_agent.execution.mapreduce._guard import (
    ShadowedImportError,
    assert_canonical_import,
)


def test_happy_path_real_install_does_not_raise():
    # The suite runs from a normal editable/site install, so the guard must
    # be a no-op: real __file__, Python 3, not under ~/.local.
    assert_canonical_import()


def test_namespace_shadow_without_file_raises(monkeypatch):
    import hpc_agent

    # A namespace-package shadow resolves the package with no __file__.
    monkeypatch.setattr(hpc_agent, "__file__", None, raising=False)
    with pytest.raises(ShadowedImportError, match="namespace package"):
        assert_canonical_import()


def test_user_site_shadow_raises(monkeypatch):
    import hpc_agent

    fake = os.path.join(
        os.path.expanduser("~/.local"),
        "lib",
        "python3.10",
        "site-packages",
        "hpc_agent",
        "__init__.py",
    )
    monkeypatch.setattr(hpc_agent, "__file__", fake, raising=False)
    with pytest.raises(ShadowedImportError, match="user-site"):
        assert_canonical_import()


def test_disable_env_var_skips_guard(monkeypatch):
    import hpc_agent

    # Even with a would-fail layout, the escape hatch makes it a no-op.
    monkeypatch.setattr(hpc_agent, "__file__", None, raising=False)
    monkeypatch.setenv("HPC_DISABLE_IMPORT_GUARD", "1")
    assert_canonical_import()
