"""Per-verb plugin gate on the CLI single-verb fast path (latency rank 13).

The fast path (``hpc_agent.cli.dispatch``) imports only the one module a mapped
core verb needs, skipping the ~100-module ``register_primitives`` walk. The walk
is the only place plugin ``register_cli`` hooks reshape the parser, so the fast
path must defer to it *for a verb a plugin can reshape* — but no longer for every
verb merely because some CLI-shaping plugin is installed.

Rank 13 narrows the gate from "any ``register_cli`` plugin → whole fast path
off" to a PER-VERB decision driven by the plugin manifest's
``reshapes_core_verbs`` declaration:

* an UNDECLARED ``register_cli`` plugin (no manifest / field unset) keeps the
  conservative pre-rank behaviour — every core verb falls back (back-compat);
* an ADD-ONLY plugin declaring ``reshapes_core_verbs=()`` leaves ALL core verbs
  fast (the ``hpc-agent-notebook-render`` case — the win);
* a plugin declaring ``reshapes_core_verbs=("foo",)`` forces ONLY ``foo`` to the
  full walk; every other core verb stays fast.

We drive the gate by monkeypatching ``load_plugins`` (which
``cli_reshaping_verdict`` re-imports at call time) with synthetic plugin objects
— no real plugin distribution is installed in the test venv. The disk verdict
cache is disabled per-test so the patched ``load_plugins`` is authoritative.
"""

from __future__ import annotations

import types

import pytest

import hpc_agent._kernel.registry.plugins as plugins_mod
from hpc_agent._wire.plugin_manifest import PluginManifest
from hpc_agent.cli import dispatch
from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

# A verb guaranteed present in the generated map; its single-verb parser accepts
# ``--spec`` without touching a cluster, so the fast path can be driven to the
# ``_invoke_parsed`` seam without side effects.
_CORE_VERB = "monitor-flow"
_OTHER_CORE_VERB = "read-decisions"
# A name no plugin-added verb could ever collide with — absent from the map.
_PLUGIN_VERB = "totally-not-in-the-core-map"


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the cross-process verdict cache so patched ``load_plugins`` wins.

    The rank-13 verdict is cached on disk keyed on the installed-distribution
    set; with a real signature that cache would shadow the synthetic
    ``load_plugins`` these tests install. The kill switch forces a fresh scan.
    """
    monkeypatch.setenv("HPC_AGENT_NO_FAST_PATH_CACHE", "1")
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)
    monkeypatch.delenv("HPC_AGENT_DISABLE_PLUGINS", raising=False)


def _primitives_only_plugin() -> object:
    """A plugin that registers new primitives but never shapes the CLI."""
    return types.SimpleNamespace(primitive_modules=("some_plugin.primitives",))


def _undeclared_register_cli_plugin() -> object:
    """A LEGACY CLI-shaping plugin: ``register_cli`` but NO manifest (undeclared)."""
    return types.SimpleNamespace(register_cli=lambda subparsers: None)


def _add_only_plugin() -> object:
    """A declared ADD-ONLY plugin: reshapes NO core verb (``notebook-render``)."""
    return types.SimpleNamespace(
        register_cli=lambda subparsers: None,
        MANIFEST=PluginManifest(
            name="hpc-agent-addonly", version="0.1", cli_register=True, reshapes_core_verbs=()
        ),
    )


def _reshaping_plugin(*verbs: str) -> object:
    """A declared plugin that reshapes exactly *verbs*."""
    return types.SimpleNamespace(
        register_cli=lambda subparsers: None,
        MANIFEST=PluginManifest(
            name="hpc-agent-reshaper",
            version="0.1",
            cli_register=True,
            reshapes_core_verbs=verbs,
        ),
    )


def _patch_plugins(monkeypatch: pytest.MonkeyPatch, loaded: tuple[object, ...]) -> None:
    """Make ``load_plugins`` return *loaded* wherever the verdict re-imports it."""
    monkeypatch.setattr(plugins_mod, "load_plugins", lambda: loaded)


# ── cli_reshaping_verdict: the reduced verdict ─────────────────────────────


def test_no_plugins_verdict_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, ())
    assert plugins_mod.cli_reshaping_verdict() == (False, frozenset())


def test_primitives_only_verdict_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))
    assert plugins_mod.cli_reshaping_verdict() == (False, frozenset())


def test_undeclared_reshaper_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    """A legacy ``register_cli`` plugin with no manifest → conservative (True)."""
    _patch_plugins(monkeypatch, (_undeclared_register_cli_plugin(),))
    conservative, reshaped = plugins_mod.cli_reshaping_verdict()
    assert conservative is True and reshaped == frozenset()


def test_add_only_declares_nothing_reshaped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    assert plugins_mod.cli_reshaping_verdict() == (False, frozenset())


def test_declared_reshaper_names_only_its_verbs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, (_reshaping_plugin("submit-s1", "submit-s2"),))
    conservative, reshaped = plugins_mod.cli_reshaping_verdict()
    assert conservative is False and reshaped == frozenset({"submit-s1", "submit-s2"})


def test_undeclared_wins_over_declared_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    """One undeclared reshaper among declared peers still forces the whole-CLI
    conservative gate — we cannot know what the undeclared one touches."""
    _patch_plugins(
        monkeypatch,
        (_reshaping_plugin("submit-s1"), _undeclared_register_cli_plugin()),
    )
    conservative, reshaped = plugins_mod.cli_reshaping_verdict()
    assert conservative is True


def test_non_callable_register_cli_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """``register_cli`` must be callable to count (mirrors the loader's guard)."""
    bogus = types.SimpleNamespace(register_cli="not a function")
    _patch_plugins(monkeypatch, (bogus,))
    assert plugins_mod.cli_reshaping_verdict() == (False, frozenset())


# ── _fast_dispatch_enabled: per-verb gate ──────────────────────────────────


def test_add_only_keeps_core_verb_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WIN: an add-only plugin no longer disqualifies core verbs."""
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is True
    assert dispatch._fast_dispatch_enabled(_OTHER_CORE_VERB) is True


def test_undeclared_reshaper_disqualifies_every_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: a legacy undeclared plugin keeps today's conservative full walk."""
    _patch_plugins(monkeypatch, (_undeclared_register_cli_plugin(),))
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is False
    assert dispatch._fast_dispatch_enabled(_OTHER_CORE_VERB) is False


def test_declared_reshaper_disqualifies_only_named_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-verb granularity: only the declared verb falls back; peers stay fast."""
    _patch_plugins(monkeypatch, (_reshaping_plugin(_CORE_VERB),))
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is False
    assert dispatch._fast_dispatch_enabled(_OTHER_CORE_VERB) is True


def test_kill_switch_wins_over_plugin_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_AGENT_NO_FAST_CLI", "1")
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is False


def test_disable_plugins_short_circuits_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_AGENT_DISABLE_PLUGINS", "1")

    def _boom() -> tuple[object, ...]:
        raise AssertionError("load_plugins must not be consulted when disabled")

    monkeypatch.setattr(plugins_mod, "load_plugins", _boom)
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is True


def test_metadata_error_falls_back_to_full_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``load_plugins`` blow-up degrades to the full path (fail-safe)."""

    def _raise() -> tuple[object, ...]:
        raise RuntimeError("entry-point metadata is corrupt")

    monkeypatch.setattr(plugins_mod, "load_plugins", _raise)
    assert dispatch._fast_dispatch_enabled(_CORE_VERB) is False


# ── _try_fast_dispatch: end-to-end routing under the narrowed gate ──────────


def test_core_verb_fast_dispatches_with_add_only_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an add-only plugin installed, a mapped core verb reaches the shared
    ``_invoke_parsed`` seam (sentinel) — proving it took the fast path."""
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    sentinel = 4242
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: sentinel)
    rc = dispatch._try_fast_dispatch([_CORE_VERB, "--spec", "/no/such/spec.json"])
    assert rc == sentinel


def test_reshaped_core_verb_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verb a declared plugin reshapes takes the full walk even though a
    sibling stays fast."""
    _patch_plugins(monkeypatch, (_reshaping_plugin(_CORE_VERB),))
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: 4242)
    assert dispatch._try_fast_dispatch([_CORE_VERB, "--spec", "/x"]) is None


def test_plugin_verb_falls_through_even_when_gate_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plugin-contributed verb is absent from the map, so it defers to the full
    walk that knows how to dispatch it, even with the gate open."""
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    assert _PLUGIN_VERB not in VERB_MODULE_MAP
    assert dispatch._try_fast_dispatch([_PLUGIN_VERB]) is None


def test_undeclared_reshaper_forces_core_verb_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, (_undeclared_register_cli_plugin(),))
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: 4242)
    assert dispatch._try_fast_dispatch([_CORE_VERB, "--spec", "/x"]) is None


# ── plugin_contributes_primitive_modules: the baked-hydration gate signal ───
#
# The baked ``operations.json`` catalog is CORE-ONLY. A plugin contributing
# ``primitive_modules`` adds verbs the bake cannot carry, so the discovery verbs
# (``describe`` / ``find``) must fall to the full walk rather than answer off the
# core-only bake and MISS them. This predicate is that gate signal.

# The discovery verbs served via baked hydration; both are in the generated map.
_DISCOVERY_VERB = "describe"
_OTHER_DISCOVERY_VERB = "find"


def test_primitives_only_contributes_primitive_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))
    assert plugins_mod.plugin_contributes_primitive_modules() is True


def test_no_plugins_contributes_no_primitive_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_plugins(monkeypatch, ())
    assert plugins_mod.plugin_contributes_primitive_modules() is False


def test_cli_shaping_plugins_contribute_no_primitive_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An add-only or reshaping ``register_cli`` plugin with no ``primitive_modules``
    is handled by the reshaping gate, not the baked-hydration gate."""
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    assert plugins_mod.plugin_contributes_primitive_modules() is False
    _patch_plugins(monkeypatch, (_reshaping_plugin(_CORE_VERB),))
    assert plugins_mod.plugin_contributes_primitive_modules() is False


# ── baked-hydration fast path: a primitive_modules plugin forces the walk ────


def test_primitives_plugin_forces_discovery_off_baked_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FIX: with a TRUSTED (forced) bake but a plugin adding primitive_modules,
    ``describe`` / ``find`` must fall to the full walk. The core-only bake cannot
    carry the plugin's verbs, so serving off it would MISS them — non-byte-
    identical to the full walk (which imports every ``primitive_modules`` module).
    """
    monkeypatch.setenv("HPC_AGENT_FORCE_BAKED_CATALOG", "1")
    _patch_plugins(monkeypatch, (_primitives_only_plugin(),))
    assert dispatch._try_fast_dispatch([_DISCOVERY_VERB, "submit-s1"]) is None
    assert dispatch._try_fast_dispatch([_OTHER_DISCOVERY_VERB, "submit a batch"]) is None


def test_add_only_plugin_keeps_discovery_on_baked_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: a plugin that adds NO primitives leaves the discovery verbs on the
    fast baked-hydration path — they reach the shared ``_invoke_parsed`` seam
    (sentinel) instead of falling back. Proves the new gate fires ONLY on
    ``primitive_modules`` contributors, not on any installed plugin."""
    monkeypatch.setenv("HPC_AGENT_FORCE_BAKED_CATALOG", "1")
    _patch_plugins(monkeypatch, (_add_only_plugin(),))
    sentinel = 4242
    monkeypatch.setattr(dispatch, "_invoke_parsed", lambda args: sentinel)
    assert dispatch._try_fast_dispatch([_DISCOVERY_VERB, "submit-s1"]) == sentinel
    assert dispatch._try_fast_dispatch([_OTHER_DISCOVERY_VERB, "submit a batch"]) == sentinel
