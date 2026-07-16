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
