"""Capability-scoped plugin gate on the CLI single-verb fast path.

The fast path (``hpc_agent.cli.dispatch``) imports only the one module a mapped
core verb needs, skipping the ~100-module ``register_primitives`` walk. The
walk is the only place plugin ``register_cli`` hooks reshape the parser, so the
fast path must defer to it *when a plugin can reshape a core verb's CLI* — but
NOT merely because some plugin is installed.

These tests pin the narrowing decided in the packages-swarm architect memo §5:
the gate closes only for a plugin that implements the CLI-shaping hook
(``register_cli`` — the sole seam handed the argparse subparsers, per
``hpc_agent._kernel.registry.plugins.register_plugin_cli``). A plugin that only
registers new primitives leaves core verbs fast, and its own new verbs fall
through naturally because they are absent from ``VERB_MODULE_MAP``.

We drive the gate by monkeypatching ``load_plugins`` (which
``_fast_dispatch_enabled`` re-imports at call time) with synthetic plugin
objects — no real plugin distribution is installed in the test venv.
"""

from __future__ import annotations

import types

import pytest

import hpc_agent._kernel.registry.plugins as plugins_mod
from hpc_agent.cli import dispatch
from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

# A verb guaranteed present in the generated map; its single-verb parser accepts
# ``--spec`` without touching a cluster, so the fast path can be driven to the
# ``_invoke_parsed`` seam without side effects.
_CORE_VERB = "monitor-flow"
# A name no plugin-added verb could ever collide with — and, crucially, absent
# from VERB_MODULE_MAP, standing in for a plugin-contributed verb.
_PLUGIN_VERB = "totally-not-in-the-core-map"


def _primitives_only_plugin() -> object:
    """A plugin that registers new primitives but never shapes the CLI."""
    return types.SimpleNamespace(primitive_modules=("some_plugin.primitives",))


def _register_cli_plugin() -> object:
    """A plugin that implements the CLI-shaping hook (can reshape a core verb)."""
    return types.SimpleNamespace(register_cli=lambda subparsers: None)


def _patch_plugins(monkeypatch: pytest.MonkeyPatch, loaded: tuple[object, ...]) -> None:
    """Make ``load_plugins`` return *loaded* wherever the gate re-imports it."""
    monkeypatch.setattr(plugins_mod, "load_plugins", lambda: loaded)


# ── _fast_dispatch_enabled: the narrowed gate ──────────────────────────────


def test_no_plugins_enables_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: with nothing installed the fast path is on."""
    _patch_plugins(monkeypatch, ())
    assert dispatch._fast_dispatch_enabled() is True


def test_primitives_only_plugin_keeps_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin with only ``primitive_modules`` cannot reshape a core verb's CLI,
    so the gate stays open — the crux of the memo §5 narrowing."""
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))
    assert dispatch._fast_dispatch_enabled() is True


def test_register_cli_plugin_closes_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin implementing the CLI-shaping hook forces the full walk."""
    _patch_plugins(monkeypatch, (_register_cli_plugin(),))
    assert dispatch._fast_dispatch_enabled() is False


def test_mixed_plugins_close_when_any_shapes_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """One CLI-shaping plugin among primitives-only peers still closes the gate."""
    _patch_plugins(
        monkeypatch,
        (_primitives_only_plugin(), _register_cli_plugin()),
    )
    assert dispatch._fast_dispatch_enabled() is False


def test_non_callable_register_cli_does_not_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """``register_cli`` must be *callable* to count — a stray non-callable
    attribute mirrors ``register_plugin_cli``'s own ``callable`` guard and does
    not force the full path."""
    bogus = types.SimpleNamespace(register_cli="not a function")
    _patch_plugins(monkeypatch, (bogus,))
    assert dispatch._fast_dispatch_enabled() is True


def test_kill_switch_wins_over_plugin_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HPC_AGENT_NO_FAST_CLI=1`` disables the fast path regardless of plugins."""
    monkeypatch.setenv("HPC_AGENT_NO_FAST_CLI", "1")
    _patch_plugins(monkeypatch, ())  # even with nothing installed
    assert dispatch._fast_dispatch_enabled() is False


def test_disable_plugins_short_circuits_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HPC_AGENT_DISABLE_PLUGINS=1`` allows the fast path without consulting
    ``load_plugins`` — no plugin can act, so none can reshape the CLI."""
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.setenv("HPC_AGENT_DISABLE_PLUGINS", "1")

    def _boom() -> tuple[object, ...]:
        raise AssertionError("load_plugins must not be consulted when disabled")

    monkeypatch.setattr(plugins_mod, "load_plugins", _boom)
    assert dispatch._fast_dispatch_enabled() is True


def test_metadata_error_falls_back_to_full_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``load_plugins`` blow-up degrades to the full path (fail-safe), never
    crashes the CLI — the byte-identical fallback is preserved by construction."""
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)

    def _raise() -> tuple[object, ...]:
        raise RuntimeError("entry-point metadata is corrupt")

    monkeypatch.setattr(plugins_mod, "load_plugins", _raise)
    assert dispatch._fast_dispatch_enabled() is False


# ── _try_fast_dispatch: end-to-end routing under the narrowed gate ──────────


def test_core_verb_fast_dispatches_with_primitives_only_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a primitives-only plugin installed, a mapped core verb is served on
    the fast path: it reaches the shared ``_invoke_parsed`` seam (sentinel),
    proving it did NOT fall through to the full walk."""
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))

    sentinel = 4242
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: sentinel)

    rc = dispatch._try_fast_dispatch([_CORE_VERB, "--spec", "/no/such/spec.json"])
    assert rc == sentinel


def test_plugin_verb_falls_through_even_when_gate_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin-contributed verb is absent from VERB_MODULE_MAP, so even with the
    gate open (primitives-only plugin) it defers to the full walk that knows how
    to dispatch it."""
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))

    assert _PLUGIN_VERB not in VERB_MODULE_MAP
    assert dispatch._try_fast_dispatch([_PLUGIN_VERB]) is None


def test_core_verb_falls_through_with_register_cli_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI-shaping plugin forces even a mapped core verb onto the full walk."""
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)
    _patch_plugins(monkeypatch, (_register_cli_plugin(),))

    # If the gate wrongly stayed open this would return the sentinel instead.
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: 4242)
    assert dispatch._try_fast_dispatch([_CORE_VERB, "--spec", "/x"]) is None


def test_stale_map_miss_falls_through_with_gate_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A map entry whose module import fails (rename/delete staleness) defers to
    the full walk even with the plugin gate open — unchanged #59 behaviour."""
    import hpc_agent._kernel.registry.primitive as primitive_mod

    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))

    def _raise_import_error(module_name: str) -> None:
        raise ModuleNotFoundError(f"No module named {module_name!r}")

    monkeypatch.setattr(primitive_mod, "register_single_module", _raise_import_error)
    verb = next(iter(VERB_MODULE_MAP))
    assert dispatch._try_fast_dispatch([verb]) is None
