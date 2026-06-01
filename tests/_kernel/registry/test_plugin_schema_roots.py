"""Tests for the generic plugin schema-root resolution.

``plugin_schema_roots`` is what lets the CLI input boundary validate a
``--spec`` against a *plugin-owned* primitive's wire schema without the
host knowing any plugin's name. It resolves each loaded plugin's schema
directory two ways:

* an explicit ``schema_assets`` traversable on the plugin object, or
* by convention: the ``<plugin-module-root>.schemas`` package, derived
  from the plugin object's ``__name__``.

These tests fake plugin objects and monkeypatch ``load_plugins`` (the
single discovery chokepoint) so they exercise the resolution logic
without installing a real plugin distribution.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from hpc_agent._kernel.registry import plugins


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """``load_plugins`` is ``@cache``-d; reset around each test."""
    plugins.load_plugins.cache_clear()
    yield
    plugins.load_plugins.cache_clear()


def test_no_plugins_yields_no_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugins, "load_plugins", lambda: ())
    assert plugins.plugin_schema_roots() == ()


def test_explicit_schema_assets_is_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin exposing ``schema_assets`` contributes that object as-is."""
    sentinel = object()
    plugin = SimpleNamespace(__name__="anything.plugin", schema_assets=sentinel)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    roots = plugins.plugin_schema_roots()

    assert roots == (sentinel,)


def test_convention_resolves_from_module_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``schema_assets``, the ``<root>.schemas`` package is resolved.

    Uses the host's own ``hpc_agent.schemas`` as the stand-in target: a
    plugin object whose ``__name__`` is ``hpc_agent.<...>`` resolves to
    the real, importable ``hpc_agent.schemas`` package — proving the
    convention derivation + ``importlib.resources`` lookup works end to
    end against a package that actually exists on disk.
    """
    plugin = SimpleNamespace(__name__="hpc_agent.fake_plugin_module")
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    roots = plugins.plugin_schema_roots()

    assert len(roots) == 1
    # The resolved traversable points at the host schemas package and a
    # known schema file is reachable through it.
    assert (roots[0] / "submit.input.json").is_file()


def test_unimportable_convention_package_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin whose ``<root>.schemas`` doesn't import is skipped, not fatal."""
    plugin = SimpleNamespace(__name__="nonexistent_plugin_pkg_xyz.plugin")
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    assert plugins.plugin_schema_roots() == ()


def test_plugin_without_name_or_assets_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An object with no usable ``__name__`` and no ``schema_assets`` is skipped."""

    class _Anon:
        # an instance: no ``__name__`` attribute, no ``schema_assets``
        pass

    monkeypatch.setattr(plugins, "load_plugins", lambda: (_Anon(),))

    assert plugins.plugin_schema_roots() == ()


def test_explicit_assets_take_precedence_over_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both are present, ``schema_assets`` wins; convention isn't consulted."""
    sentinel = object()
    plugin = SimpleNamespace(__name__="hpc_agent.whatever", schema_assets=sentinel)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (plugin,))

    assert plugins.plugin_schema_roots() == (sentinel,)


def test_multiple_plugins_preserve_order(monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = object(), object()
    p1 = SimpleNamespace(__name__="p1.plugin", schema_assets=a)
    p2 = SimpleNamespace(__name__="p2.plugin", schema_assets=b)
    monkeypatch.setattr(plugins, "load_plugins", lambda: (p1, p2))

    assert plugins.plugin_schema_roots() == (a, b)
