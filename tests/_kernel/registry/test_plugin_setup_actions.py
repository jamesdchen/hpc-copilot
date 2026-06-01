"""Tests for the generic plugin setup-action seam.

``run_plugin_setup_actions`` is the host's plugin-agnostic replacement
for the previously hard-coded ``install-cron`` wiring in the ``setup``
primitive. The host invokes each loaded plugin's optional
``run_setup_actions(context)`` hook blindly and collects the results
under the ``setup`` envelope's ``plugin_actions`` field, keyed by
plugin name. It names no specific plugin and knows nothing about what
an action does.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from hpc_agent._kernel.registry import plugins


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    plugins.load_plugins.cache_clear()
    yield
    plugins.load_plugins.cache_clear()


def test_no_plugins_yields_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "load_plugins", lambda: ())
    assert plugins.run_plugin_setup_actions({"install": False}) == {}


def test_plugin_without_hook_contributes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = SimpleNamespace(__name__="someplugin.plugin")  # no run_setup_actions
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))
    assert plugins.run_plugin_setup_actions({"install": True}) == {}


def test_hook_result_keyed_by_manifest_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin with a MANIFEST.name is keyed by that name."""
    manifest = SimpleNamespace(name="my-plugin")
    plugin = SimpleNamespace(
        __name__="my_plugin.plugin",
        MANIFEST=manifest,
        run_setup_actions=lambda ctx: {"status": "available"},
    )
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    actions = plugins.run_plugin_setup_actions({"install": False})

    assert actions == {"my-plugin": {"status": "available"}}


def test_hook_falls_back_to_module_key_without_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = SimpleNamespace(
        __name__="myplugin.plugin",
        run_setup_actions=lambda ctx: {"status": "installed"},
    )
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    actions = plugins.run_plugin_setup_actions({"install": True})

    assert actions == {"myplugin": {"status": "installed"}}


def test_context_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook receives a copy of the context dict the host built."""
    seen: dict = {}

    def _hook(ctx: dict) -> dict:
        seen.update(ctx)
        return {"ok": True}

    plugin = SimpleNamespace(__name__="p.plugin", run_setup_actions=_hook)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    ctx = {"cluster": "hoffman2", "experiment_dir": "/e", "install": True, "dry_run": False}
    plugins.run_plugin_setup_actions(ctx)

    assert seen == ctx


def test_context_is_copied_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin mutating its context copy can't corrupt the host's dict or peers."""

    def _mutating_hook(ctx: dict) -> dict:
        ctx["install"] = "TAMPERED"
        return {"ok": True}

    p1 = SimpleNamespace(__name__="p1.plugin", run_setup_actions=_mutating_hook)
    captured: dict = {}

    def _observer(ctx: dict) -> dict:
        captured.update(ctx)
        return {"ok": True}

    p2 = SimpleNamespace(__name__="p2.plugin", run_setup_actions=_observer)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (p1, p2))

    host_ctx = {"install": True}
    plugins.run_plugin_setup_actions(host_ctx)

    assert host_ctx == {"install": True}  # host dict untouched
    assert captured == {"install": True}  # p2 saw the original, not p1's tamper


def test_falsy_or_none_result_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    p_none = SimpleNamespace(__name__="a.plugin", run_setup_actions=lambda ctx: None)
    p_empty = SimpleNamespace(__name__="b.plugin", run_setup_actions=lambda ctx: {})
    monkeypatch.setattr(plugins, "load_plugins", lambda: (p_none, p_empty))

    assert plugins.run_plugin_setup_actions({"install": False}) == {}


def test_raising_hook_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """One plugin's hook raising doesn't break setup or the other plugins."""

    def _boom(ctx: dict) -> dict:
        raise RuntimeError("plugin exploded")

    p_bad = SimpleNamespace(__name__="bad.plugin", run_setup_actions=_boom)
    p_good = SimpleNamespace(__name__="good.plugin", run_setup_actions=lambda ctx: {"ok": True})
    monkeypatch.setattr(plugins, "load_plugins", lambda: (p_bad, p_good))

    with pytest.warns(RuntimeWarning, match="plugin setup hook failed"):
        actions = plugins.run_plugin_setup_actions({"install": True})

    assert actions == {"good": {"ok": True}}
