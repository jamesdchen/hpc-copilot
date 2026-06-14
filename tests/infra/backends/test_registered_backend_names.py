"""Tests for :func:`hpc_agent.infra.backends.registered_backend_names`.

The helper backs the clusters.yaml ``scheduler`` validator's
plugin-backend check (``docs/proposals/crowd-compute-backend.md``): it
must report the built-in backends, import plugin ``primitive_modules``
for their ``@register`` side effect, and skip a broken plugin module
silently (the primitive registry owns the loud warning for that
failure).
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from hpc_agent._kernel.registry import plugins
from hpc_agent.infra import backends


def test_includes_builtin_backends():
    names = backends.registered_backend_names()
    assert {"slurm", "sge", "pbspro", "torque"} <= names


def test_imports_plugin_modules_for_register_side_effect(tmp_path, monkeypatch):
    # A plugin's backend registers at import time; the helper must
    # trigger that import itself rather than depend on primitive
    # registration having already run in this process.
    mod = tmp_path / "fake_crowd_plugin_backend.py"
    mod.write_text(
        textwrap.dedent(
            """\
            from hpc_agent.infra.backends import HPCBackend, register


            @register("fakecrowdhelper")
            class FakeCrowdBackend(HPCBackend):
                scheduler_name = "fakecrowdhelper"

                def _build_command(self, *a, **k):
                    raise NotImplementedError
            """
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(plugins, "plugin_primitive_modules", lambda: ("fake_crowd_plugin_backend",))
    try:
        assert "fakecrowdhelper" in backends.registered_backend_names()
    finally:
        backends._REGISTRY.pop("fakecrowdhelper", None)
        sys.modules.pop("fake_crowd_plugin_backend", None)


def test_broken_plugin_module_is_skipped(monkeypatch):
    monkeypatch.setattr(
        plugins,
        "plugin_primitive_modules",
        lambda: ("hpc_agent_test_no_such_module_xyz",),
    )
    # The helper survives (still reports built-ins) AND surfaces the failure
    # via a warning — config validation can run outside CLI dispatch, where
    # the primitive registry's own warning never fires, so a silently-dropped
    # plugin would otherwise be invisible.
    with pytest.warns(UserWarning, match="hpc_agent_test_no_such_module_xyz"):
        names = backends.registered_backend_names()
    assert "slurm" in names
