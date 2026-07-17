"""Tests for the CLI single-verb fast path (``hpc_agent.cli.dispatch``).

The fast path imports only the module that defines a known ungrouped verb,
skipping the full ~100-module ``register_primitives`` walk. The load-bearing
property is *behavioural equivalence*: the fast path must produce byte-identical
output to the full path, only faster. We prove that with a subprocess that runs
the same verb twice — once fast, once with the ``HPC_AGENT_NO_FAST_CLI=1`` kill
switch forcing the full path — and compares stdout.

The remaining unit tests cover the fall-back guards (a stale-map or
grouped/unknown verb must defer to the full path) and the generated map's sync
with the registry, all of which run against the full registry the session
fixture already populated.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

# A one-liner that runs the real CLI entry point in a FRESH interpreter, so the
# registration latch starts unset and the fast path is genuinely exercised.
_RUNNER = "import sys; from hpc_agent.cli.dispatch import main; sys.exit(main(sys.argv[1:]))"


def _run_cli(args: list[str], *, force_full: bool) -> subprocess.CompletedProcess[str]:
    import os

    env = dict(os.environ)
    # Hermetic: never reach a real cluster binary, and keep plugin scanning off
    # so the fast path is taken (a stray installed plugin would force full).
    env["HPC_AGENT_DISABLE_PLUGINS"] = "1"
    if force_full:
        env["HPC_AGENT_NO_FAST_CLI"] = "1"
    else:
        env.pop("HPC_AGENT_NO_FAST_CLI", None)
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.slow
def test_fast_and_full_paths_are_byte_identical() -> None:
    """A mapped verb yields the same envelope + exit code on both paths."""
    # ``--spec`` of a missing file → a deterministic spec_invalid envelope,
    # emitted after argparse but without touching a cluster or needing fixtures.
    args = ["monitor-flow", "--spec", "/no/such/fast-dispatch-spec.json"]
    fast = _run_cli(args, force_full=False)
    full = _run_cli(args, force_full=True)
    assert fast.returncode == full.returncode == 1
    assert fast.stdout == full.stdout
    assert '"ok": false' in fast.stdout
    assert '"spec_invalid"' in fast.stdout


def _run_discovery(args: list[str], *, mode: str) -> subprocess.CompletedProcess[str]:
    """Run a discovery verb (``describe`` / ``find``) in a fresh interpreter.

    ``mode`` selects the path: ``"baked"`` forces the baked-hydration fast path
    (``HPC_AGENT_FORCE_BAKED_CATALOG=1`` — a source checkout has no build
    fingerprint, so the trust gate is off by default); ``"full"`` forces the
    full registry walk via the kill switch. The describe cache is disabled so
    the comparison exercises the catalog SOURCE (bake vs live), not a prior hit.
    """
    import os

    env = dict(os.environ)
    env["HPC_AGENT_DISABLE_PLUGINS"] = "1"
    env["HPC_NO_DESCRIBE_CACHE"] = "1"
    env.pop("HPC_AGENT_NO_FAST_CLI", None)
    env.pop("HPC_AGENT_FORCE_BAKED_CATALOG", None)
    if mode == "baked":
        env["HPC_AGENT_FORCE_BAKED_CATALOG"] = "1"
    elif mode == "full":
        env["HPC_AGENT_NO_FAST_CLI"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _RUNNER, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    "args",
    [
        ["describe", "submit-s1"],  # a primitive contract
        ["describe", "hpc-submit"],  # a skill (bake-independent path)
        ["describe", "definitely-not-a-verb"],  # not-found + did-you-mean
        ["describe", "submit-s1", "--schema"],  # --schema steers to full path
        ["find", "submit a batch"],  # intent-phrase keyword scan
        ["find", "reconcile"],  # fuzzy name match
    ],
)
def test_discovery_baked_hydration_is_byte_identical_to_full_walk(args: list[str]) -> None:
    """B4/B5 premortem A1: a fast-path ``describe`` / ``find`` served off the
    hydrated ``operations.json`` bake is byte-identical to the full-walk answer —
    never the ~4-entry partial registry. Proven cross-module (submit-s1's row,
    the whole catalog for find's scan) so a partial-registry regression shows."""
    # Cross-worker lock: the seeded-stale test below poisons the SHARED
    # packaged bake in place; a forced-bake subprocess reading mid-window
    # reports drift with the sentinel in the baked answer (the e41f25e2
    # py3.12 CI red). See tests/_bake_lock.py.
    from tests._bake_lock import bake_file_lock

    with bake_file_lock():
        baked = _run_discovery(args, mode="baked")
        full = _run_discovery(args, mode="full")
    assert baked.returncode == full.returncode
    assert baked.stdout == full.stdout, f"fast/full drift for {args!r}"


@pytest.mark.slow
def test_seeded_stale_bake_falls_back_to_walk_byte_identical(tmp_path: object) -> None:
    """Enforcement row 5: staleness is content-keyed on the BUILD FINGERPRINT,
    not the version string. A source checkout carries no ``BUILD_SHA``, so its
    (possibly stale) bake is NEVER trusted — ``describe`` / ``find`` walk. We
    prove the walk ignores a deliberately-wrong seeded bake by comparing the
    default (no-force) path to the kill-switch full path: identical, and NOT the
    stale bake's content. A version-string key would wrongly trust the bake."""
    import json
    from importlib.resources import files

    from tests._bake_lock import bake_file_lock

    # WRITER side of the cross-worker bake lock: this test mutates the ONE
    # shared packaged operations.json on disk. Every content-reader (the
    # byte-identity test above, tests/scripts/test_bake_operations_json.py)
    # takes the same lock, so the poison window can never leak into another
    # xdist worker's assertions. See tests/_bake_lock.py for the incident.
    with bake_file_lock():
        bake_path = files("hpc_agent") / "operations.json"
        original = bake_path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        catalog = json.loads(original)
        # Corrupt one row's summary so a bake-trusting path would emit the poison.
        poison = "STALE-BAKE-POISON-DO-NOT-SERVE"
        for entry in catalog:
            if entry.get("name") == "submit-s1":
                entry["summary"] = poison
        _seeded_stale_bake_body(bake_path, original, catalog, poison)


def _seeded_stale_bake_body(bake_path, original: str, catalog, poison: str) -> None:
    """Body of the seeded-stale test; the caller holds the bake lock throughout."""
    import json
    import os

    try:
        bake_path.write_text(  # type: ignore[attr-defined]
            json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["HPC_AGENT_DISABLE_PLUGINS"] = "1"
        env["HPC_NO_DESCRIBE_CACHE"] = "1"
        # DEV default: BUILD_SHA is None and no force → the stale bake is untrusted.
        env.pop("HPC_AGENT_FORCE_BAKED_CATALOG", None)
        env.pop("HPC_AGENT_NO_FAST_CLI", None)
        default = subprocess.run(
            [sys.executable, "-c", _RUNNER, "describe", "submit-s1"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        env_full = dict(env)
        env_full["HPC_AGENT_NO_FAST_CLI"] = "1"
        full = subprocess.run(
            [sys.executable, "-c", _RUNNER, "describe", "submit-s1"],
            capture_output=True,
            text=True,
            env=env_full,
            timeout=120,
        )
        assert default.stdout == full.stdout
        assert poison not in default.stdout, "stale bake was trusted — content key failed"
    finally:
        bake_path.write_text(original, encoding="utf-8")  # type: ignore[attr-defined]


def test_resolve_catalog_walks_when_plugin_adds_primitive_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_resolve_catalog`` must NOT serve the CORE-ONLY bake when an installed
    plugin contributes ``primitive_modules`` — even a TRUSTED bake would MISS the
    plugin's verbs. It falls to the full walk (whose ``operations_catalog`` sees
    every imported primitive), so the discovery surface is the whole truth.

    Driven at the ``_resolve_catalog`` seam: force a partial registry + a trusted,
    poisoned bake, and assert the poison is never returned when a plugin adds
    primitives (walk taken) but IS returned when none do (bake taken)."""
    import hpc_agent._kernel.registry.plugins as plugins_mod
    import hpc_agent._kernel.registry.primitive as primitive_mod
    from hpc_agent._kernel.registry.operations import operations_catalog
    from hpc_agent.cli.setup import _resolve_catalog

    poison = [{"name": "BAKE-ONLY-POISON", "verb": "query", "summary": ""}]
    # Pretend registration has not completed so the bake branch is reachable, but
    # keep the real (already-populated) registry so ``operations_catalog`` answers.
    monkeypatch.setattr(primitive_mod, "_REGISTRATION_DONE", False)
    monkeypatch.setattr(primitive_mod, "baked_catalog_usable", lambda: True)
    monkeypatch.setattr(primitive_mod, "load_baked_catalog", lambda: poison)
    monkeypatch.setattr(primitive_mod, "register_primitives", lambda: None)

    # A plugin adding primitives → the bake is incomplete → full walk.
    monkeypatch.setattr(plugins_mod, "plugin_contributes_primitive_modules", lambda: True)
    walked = _resolve_catalog()
    assert walked == operations_catalog()
    assert walked != poison, "core-only bake was served despite a primitive_modules plugin"

    # No plugin primitives → the trusted bake is served (unchanged core-only path).
    monkeypatch.setattr(plugins_mod, "plugin_contributes_primitive_modules", lambda: False)
    assert _resolve_catalog() == poison


@pytest.mark.slow
def test_fast_path_help_matches_full() -> None:
    """``<verb> --help`` is identical fast vs full (the fast path builds the
    same single subparser the full walk would)."""
    args = ["monitor-flow", "--help"]
    fast = _run_cli(args, force_full=False)
    full = _run_cli(args, force_full=True)
    assert fast.returncode == full.returncode == 0
    assert fast.stdout == full.stdout


def test_leading_flag_defers_to_full_path() -> None:
    """A leading global flag (``--version``/top-level ``--help``) is not a verb,
    so the fast path declines it."""
    from hpc_agent.cli.dispatch import _try_fast_dispatch

    assert _try_fast_dispatch(["--version"]) is None
    assert _try_fast_dispatch(["--help"]) is None
    assert _try_fast_dispatch([]) is None


def test_unknown_verb_defers_to_full_path() -> None:
    """A verb absent from the map (typo, Tier-3 ``run``, grouped parent) defers
    so the full parser can render its did-you-mean / group help."""
    from hpc_agent.cli.dispatch import _try_fast_dispatch

    assert _try_fast_dispatch(["definitely-not-a-verb"]) is None
    assert _try_fast_dispatch(["run"]) is None  # Tier-3, no @primitive backing
    assert _try_fast_dispatch(["clusters"]) is None  # verb-group parent


def test_kill_switch_disables_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HPC_AGENT_NO_FAST_CLI=1`` forces every verb onto the full path."""
    from hpc_agent.cli.dispatch import _fast_dispatch_enabled, _try_fast_dispatch

    monkeypatch.setenv("HPC_AGENT_NO_FAST_CLI", "1")
    assert _fast_dispatch_enabled() is False
    # Even a mapped verb defers when the kill switch is set.
    assert _try_fast_dispatch(["monitor-flow"]) is None


def test_fast_path_safe_opt_in_set_is_pinned() -> None:
    """Enforcement row 1: the ``fast_path_safe`` opt-in set is ENUMERATED. This
    equality pin (reviewed-edit pattern) is the canary — a verb joining the set
    without a reviewer moving this literal, or a later default-flip that makes
    every handler ``fast_path_safe``, turns it red. install-commands (rank 13)
    plus the B4/B5 baked-hydration discovery verbs are the whole set; a
    registry-introspecting handler (``capabilities``) is deliberately absent."""
    from hpc_agent._kernel.registry.primitive import get_registry
    from hpc_agent.cli._dispatch import CliShape

    safe = {
        name
        for name, meta in get_registry().items()
        if isinstance(meta.cli, CliShape) and meta.cli.fast_path_safe
    }
    assert safe == {"install-commands", "describe", "find"}


def test_baked_catalog_usable_is_content_keyed_on_build_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement row 5: the discovery verbs trust the bake keyed on the BUILD
    FINGERPRINT (``_build_info.BUILD_SHA``), never the version string. A source
    checkout has ``BUILD_SHA is None`` — so the bake is untrusted even though
    ``__version__`` is set (a version-string key would wrongly trust it). A wheel
    stamps ``BUILD_SHA`` and gets the win; the force env is a test/opt-in seam."""
    import hpc_agent
    import hpc_agent._build_info as bi
    from hpc_agent._kernel.registry.primitive import baked_catalog_usable

    monkeypatch.delenv("HPC_AGENT_FORCE_BAKED_CATALOG", raising=False)
    assert hpc_agent.__version__  # a version string exists...
    monkeypatch.setattr(bi, "BUILD_SHA", None)
    assert baked_catalog_usable() is False  # ...yet the untrusted bake is NOT used.
    monkeypatch.setattr(bi, "BUILD_SHA", "deadbeef")
    assert baked_catalog_usable() is True  # a stamped wheel trusts its shipped bake.
    monkeypatch.setattr(bi, "BUILD_SHA", None)
    monkeypatch.setenv("HPC_AGENT_FORCE_BAKED_CATALOG", "1")
    assert baked_catalog_usable() is True  # the explicit test/opt-in seam.


def test_build_parser_memoized_and_registers_plugins_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build_parser memo (warm in-proc win): a second call on an unchanged
    registry returns the SAME parser without re-running plugin ``register_cli``;
    a registry change (bumped generation) invalidates the cache."""
    import hpc_agent._kernel.registry.plugins as plugins_mod
    import hpc_agent._kernel.registry.primitive as primitive_mod
    from hpc_agent.cli import parser as parser_mod

    parser_mod._reset_parser_memo()
    calls = {"n": 0}
    original = plugins_mod.register_plugin_cli

    def _counting(sub: object) -> None:
        calls["n"] += 1
        return original(sub)  # type: ignore[arg-type]

    monkeypatch.setattr(plugins_mod, "register_plugin_cli", _counting)
    p1 = parser_mod.build_parser()
    p2 = parser_mod.build_parser()
    assert p1 is p2, "memo should return the same parser on an unchanged registry"
    assert calls["n"] == 1, "plugin register_cli must run exactly once under the memo"

    # A generation bump (any registry mutation) forces a rebuild.
    primitive_mod._bump_generation()
    p3 = parser_mod.build_parser()
    assert p3 is not p1, "a registry change must invalidate the memo"
    assert calls["n"] == 2
    parser_mod._reset_parser_memo()


def test_stale_map_pointing_at_missing_module_defers_not_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OTHER staleness mode (#59): a map entry whose defining module was
    renamed/deleted must degrade to the full path, not crash ``main()`` with a
    raw ``ModuleNotFoundError`` (no envelope, wrong exit code).

    Before the fix ``register_single_module`` was a bare ``import_module`` that
    ``_try_fast_dispatch`` wrapped in nothing, so the ImportError escaped for a
    verb the full walk dispatches fine. The guard is the ``except ImportError:
    return None`` fall-through.
    """
    import hpc_agent._kernel.registry.primitive as primitive_mod
    from hpc_agent.cli.dispatch import _try_fast_dispatch

    verb = next(iter(VERB_MODULE_MAP))

    def _raise_import_error(module_name: str) -> None:
        raise ModuleNotFoundError(f"No module named {module_name!r}")

    # ``_try_fast_dispatch`` imports ``register_single_module`` from the registry
    # module at call time — patch it there so its import raises the
    # rename/delete-mode ImportError. The fast path must defer (None), not raise.
    monkeypatch.setattr(primitive_mod, "register_single_module", _raise_import_error)

    assert _try_fast_dispatch([verb]) is None


def test_build_single_verb_parser_rejects_grouped_and_absent() -> None:
    """The single-verb parser builder guards against a stale map pointing at a
    grouped, handler, or non-existent primitive."""
    from hpc_agent.cli.parser import build_single_verb_parser

    assert build_single_verb_parser("no-such-primitive") is None
    # ``campaign-status`` is verb-grouped (CliShape.group set) → not fast-pathable.
    assert build_single_verb_parser("campaign-status") is None


def test_map_never_contains_grouped_verbs() -> None:
    """The generated map never contains a grouped primitive — those nest under a
    parent subparser and must take the full path."""
    from hpc_agent._kernel.registry.primitive import get_registry
    from hpc_agent.cli._dispatch import CliShape

    registry = get_registry()
    name_to_module = {name: mod for _, (name, mod) in VERB_MODULE_MAP.items()}
    for name in name_to_module:
        shape = registry[name].cli
        assert isinstance(shape, CliShape)
        assert shape.group is None, f"{name} is grouped but in the fast-path map"


def test_map_handler_entries_are_fast_path_safe() -> None:
    """A handler primitive may only appear in the map when it opted in via
    ``CliShape.fast_path_safe`` (rank 13). A registry-introspecting handler
    (``capabilities`` / ``describe``) must NEVER be mapped — the fast path leaves
    the registry unpopulated. (With the committed map still stale on this branch
    the handler rows may be absent; when present they MUST be safe.)"""
    from hpc_agent._kernel.registry.primitive import get_registry
    from hpc_agent.cli._dispatch import CliShape

    registry = get_registry()
    for _verb, (name, _mod) in VERB_MODULE_MAP.items():
        shape = registry[name].cli
        assert isinstance(shape, CliShape)
        if shape.handler is not None:
            assert shape.fast_path_safe, (
                f"{name} has a handler but is NOT fast_path_safe — it must take the full path"
            )


def test_generated_map_is_in_sync_with_registry() -> None:
    """The committed VERB_MODULE_MAP matches what the registry yields now.

    DISCLOSED-RED on this branch (rank 13): the generator was extended to map
    ``fast_path_safe`` handler primitives (``install-commands``), but the
    regenerated ``_verb_module_map.py`` is deliberately NOT committed — the
    integrator regens it at merge (``scripts/build_verb_module_map.py --write``).
    Until then this pin is EXPECTED to fail with ``install-commands`` missing;
    that is the visible signal the regen is pending, not a defect. A stale map
    only costs speed (it falls back), never correctness."""
    from hpc_agent._kernel.registry.primitive import get_registry
    from hpc_agent.cli._dispatch import CliShape, _leaf_verb

    expected: dict[str, tuple[str, str]] = {}
    for name, meta in get_registry().items():
        shape = meta.cli
        if not isinstance(shape, CliShape) or shape.group is not None:
            continue
        if shape.handler is not None and not shape.fast_path_safe:
            continue
        module = getattr(meta.func, "__module__", None)
        if module:
            expected[_leaf_verb(name, shape)] = (name, module)

    assert dict(VERB_MODULE_MAP) == expected, (
        "verb-module map is stale; run `uv run python scripts/build_verb_module_map.py --write` "
        "(EXPECTED RED on the pkg/o7-dispatch branch until the integrator regens — see docstring)"
    )
