"""Behaviour-pinning coverage for the CLI verb→module resolution seam.

Audit unit 4a. The single-verb fast path (:func:`hpc_agent.cli.dispatch.
_try_fast_dispatch`) resolves an ungrouped verb through the generated
``VERB_MODULE_MAP`` (``_verb_module_map.py``) to a ``(primitive_name,
module_name)`` pair, imports ONLY that module, builds a one-verb parser, and
dispatches — falling back (returns ``None``) to the full ``register_primitives``
+ ``build_parser`` walk on any miss. Both paths funnel every parsed verb through
``dispatch_primitive`` (``_dispatch.py``), so the load-bearing property is that
the resolution table and the two dispatch paths agree on WHICH primitive runs.

The existing ``tests/cli/test_fast_dispatch.py`` battery already pins the
map/registry sync, grouped-verb exclusion, the kill switch, the leading-flag and
stale-module fallbacks, the ``fast_path_safe`` opt-in set, and a subprocess
byte-identity check. This file does NOT duplicate those; it DEEPENS the seam
with in-process, mutation-killing pins around:

* the exact resolution (module + primitive) the fast path hands to the importer
  and the parser builder, incl. the ``(primitive_name, module_name)`` UNPACK
  ORDER and the verb→renamed-primitive indirection (``preflight`` →
  ``check-preflight``),
* the "cache never serves a wrong module for a different verb" invariant
  (distinct verbs resolve to their own modules in sequence),
* the map-miss / non-CliShape refusals (guard-can-fire with a genuinely unknown
  verb and a genuinely non-CliShape shape), incl. the full-path error contract,
* fast-path vs full-path PARITY: both parsers bind a dispatch func that routes
  the same verb to the same primitive name,
* the lazy-import short-circuit (``register_single_module`` is inert once full
  registration has completed).

Each assertion's comment/docstring names the mutant it kills.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

import hpc_agent._kernel.registry.primitive as primitive_mod
import hpc_agent.cli.parser as parser_mod
from hpc_agent.cli import dispatch
from hpc_agent.cli._dispatch import CliShape, _leaf_verb, dispatch_primitive
from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP


@pytest.fixture(autouse=True)
def _full_registry() -> None:
    """Ensure the full registry is populated (the session fixture already does
    this, but pin it locally so ``_REGISTRATION_DONE`` is True for the
    idempotency test regardless of ordering)."""
    primitive_mod.register_primitives()


@pytest.fixture
def _fast_path_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the single-verb fast path ON (disable plugins → no reshaping gate,
    clear the kill switch) so ``_try_fast_dispatch`` reaches the map lookup."""
    monkeypatch.setenv("HPC_AGENT_DISABLE_PLUGINS", "1")
    monkeypatch.delenv("HPC_AGENT_NO_FAST_CLI", raising=False)


# Representative rows: two identity rows (verb == primitive) and three
# verb-OVERRIDE rows where the map key differs from the primitive name (the
# CliShape.verb indirection — the "normalization" of this surface).
_IDENTITY_ROWS = ["monitor-flow", "reproduce-run"]
_OVERRIDE_ROWS = ["preflight", "reconcile", "discover"]


# ── resolution table: exact module + primitive, and the unpack order ───────────


@pytest.mark.parametrize("verb", _IDENTITY_ROWS + _OVERRIDE_ROWS)
def test_fast_dispatch_hands_importer_the_module_and_builder_the_primitive(
    verb: str, monkeypatch: pytest.MonkeyPatch, _fast_path_on: None
) -> None:
    """The fast path registers the MODULE string and builds the parser for the
    PRIMITIVE name — in that assignment.

    kills: swapping the ``primitive_name, module_name = entry`` unpack in
    ``_try_fast_dispatch`` (a swap would import the primitive name as a module
    and build a parser for the module path — both wrong), and any drift of the
    ``VERB_MODULE_MAP[verb]`` tuple's two fields.
    """
    expected_primitive, expected_module = VERB_MODULE_MAP[verb]
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        primitive_mod,
        "register_single_module",
        lambda module_name: seen.__setitem__("module", module_name),
    )

    def _fake_build(primitive_name: str) -> None:
        seen["primitive"] = primitive_name
        return None  # None → the fast path defers; we never run the primitive.

    monkeypatch.setattr(parser_mod, "build_single_verb_parser", _fake_build)

    rc = dispatch._try_fast_dispatch([verb])

    assert rc is None  # _fake_build returned None → deferred, as intended
    assert seen["module"] == expected_module
    assert seen["primitive"] == expected_primitive


@pytest.mark.parametrize("verb", _OVERRIDE_ROWS)
def test_verb_override_resolves_to_renamed_primitive_not_the_verb(verb: str) -> None:
    """A verb-override row maps the CLI verb to a DIFFERENTLY-named primitive, and
    the primitive's ``_leaf_verb`` round-trips back to the same verb.

    kills: a map row that points a verb at the like-named primitive (e.g.
    ``preflight`` → ``preflight`` instead of ``check-preflight``), which would
    resolve ``get_meta`` to the wrong / missing primitive.
    """
    primitive_name, _module = VERB_MODULE_MAP[verb]
    assert primitive_name != verb, f"{verb} is expected to be a verb-override row"
    shape = primitive_mod.get_meta(primitive_name).cli
    assert isinstance(shape, CliShape)
    # The full parser would expose this primitive under exactly the map key —
    # the fast/full agreement on the dispatched verb string.
    assert _leaf_verb(primitive_name, shape) == verb


def test_map_module_matches_the_primitives_defining_module() -> None:
    """Every mapped module string is the module that actually DEFINES the mapped
    primitive's function.

    kills: a map row whose module column drifted from where the primitive lives
    (the fast path would import the wrong module and ``build_single_verb_parser``
    would then find the primitive unregistered → silent full-path fallback,
    losing the fast path). Cross-checks the whole table against the registry.
    """
    registry = primitive_mod.get_registry()
    for verb, (primitive_name, module_name) in VERB_MODULE_MAP.items():
        meta = registry[primitive_name]
        assert meta.func.__module__ == module_name, (
            f"map row {verb!r} points at {module_name!r} but "
            f"{primitive_name!r} is defined in {meta.func.__module__!r}"
        )


# ── cache correctness: distinct verbs never cross-serve a module ───────────────


def test_distinct_verbs_resolve_to_their_own_modules_in_sequence(
    monkeypatch: pytest.MonkeyPatch, _fast_path_on: None
) -> None:
    """Resolving verb A then verb B imports each verb's OWN module — the second
    resolution is never served the first verb's module.

    kills: any mutation that hoists/caches a single resolved entry across calls
    (e.g. a module-level memo that ignores ``argv[0]``); each call must key on
    its own verb.
    """
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        primitive_mod,
        "register_single_module",
        lambda module_name: calls.append(("module", module_name)),
    )

    def _fake_build(primitive_name: str) -> None:
        calls.append(("primitive", primitive_name))
        return None

    monkeypatch.setattr(parser_mod, "build_single_verb_parser", _fake_build)

    dispatch._try_fast_dispatch(["preflight"])
    dispatch._try_fast_dispatch(["reconcile"])

    assert calls == [
        ("module", VERB_MODULE_MAP["preflight"][1]),
        ("primitive", VERB_MODULE_MAP["preflight"][0]),
        ("module", VERB_MODULE_MAP["reconcile"][1]),
        ("primitive", VERB_MODULE_MAP["reconcile"][0]),
    ]


# ── map-miss / unknown-verb refusal (guard can fire) ───────────────────────────


def test_map_miss_defers_without_importing_anything(
    monkeypatch: pytest.MonkeyPatch, _fast_path_on: None
) -> None:
    """A genuinely unknown verb (absent from the map) defers to the full path and
    imports NOTHING — the ``entry is None`` guard fires before ``register_single_
    module`` is ever reached.

    kills: inverting the ``if entry is None: return None`` guard, or moving the
    import ahead of it (which would try to register ``None``/a bogus module for
    an unknown verb before deferring).
    """
    touched: list[str] = []
    monkeypatch.setattr(primitive_mod, "register_single_module", lambda m: touched.append(m))

    assert "definitely-not-a-verb-xyz" not in VERB_MODULE_MAP  # genuine miss
    assert dispatch._try_fast_dispatch(["definitely-not-a-verb-xyz"]) is None
    assert touched == [], "an unmapped verb must not import any module"


def test_unknown_verb_full_path_exits_two_with_unknown_command_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full-path refusal contract for a genuinely unknown verb: argparse
    rejects the invalid subcommand with ``SystemExit(2)`` and an ``unknown
    command`` diagnostic on stderr.

    kills: a regression that swallows the unknown verb (exit 0) or changes the
    exit code away from argparse's 2 / drops the diagnostic. Guard-can-fire: the
    verb is genuinely absent from every tier of the parser.
    """
    with pytest.raises(SystemExit) as excinfo:
        dispatch.main(["definitely-not-a-verb-xyz"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "unknown command" in err
    assert "definitely-not-a-verb-xyz" in err


def test_dispatch_primitive_refuses_non_clishape_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dispatch_primitive`` refuses a registry meta whose ``cli=`` is not a
    :class:`CliShape` (e.g. a legacy invocation string) with a loud ``TypeError``
    rather than mis-dispatching it.

    kills: inverting/removing the ``if not isinstance(shape, CliShape): raise
    TypeError`` guard in ``_dispatch.py``. Guard-can-fire: a stub meta carries a
    genuine ``str`` cli.
    """

    class _StubMeta:
        cli = "hpc-agent legacy-string-cli"

        @staticmethod
        def func(**_kwargs: Any) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(primitive_mod, "get_meta", lambda _name: _StubMeta())
    with pytest.raises(TypeError, match="not CliShape"):
        dispatch_primitive("legacy-cli-verb", argparse.Namespace())


# ── fast-path vs full-path parity ──────────────────────────────────────────────


def test_fast_and_full_parsers_bind_the_same_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a mapped verb, the single-verb parser and the full parser both bind a
    dispatch func that routes to the SAME primitive name.

    kills: a fast-path parser that binds the wrong primitive (e.g. the verb
    string instead of the resolved primitive name), which would silently execute
    a different verb than the full path for identical argv. The two paths must
    agree on which primitive runs.
    """
    verb = "monitor-flow"
    primitive_name, _module = VERB_MODULE_MAP[verb]
    argv = [verb, "--spec", "unused.json"]  # --spec is argparse-required for this verb

    recorded: list[str] = []
    monkeypatch.setattr(
        parser_mod,
        "dispatch_primitive",
        lambda name, ns: recorded.append(name) or 0,  # type: ignore[func-returns-value]
    )

    single = parser_mod.build_single_verb_parser(primitive_name)
    assert single is not None
    single.parse_args(argv).func(argparse.Namespace())
    fast_name = recorded.pop()

    full = parser_mod.build_parser()
    full.parse_args(argv).func(argparse.Namespace())
    full_name = recorded.pop()

    assert fast_name == full_name == primitive_name


# ── lazy-import short-circuit (caching correctness) ────────────────────────────


def test_register_single_module_is_inert_after_full_registration() -> None:
    """Once ``_REGISTRATION_DONE`` is set (full registry populated),
    ``register_single_module`` is a no-op — it does NOT re-import, so a bogus
    module name passes without raising.

    kills: removing the ``if _REGISTRATION_DONE: return`` short-circuit, which
    would send the bogus name to ``importlib.import_module`` and raise
    ``ModuleNotFoundError``. Pins the fast-path importer's idempotency: a warm
    process that already walked the registry never re-pays a single-module import.
    """
    assert primitive_mod._REGISTRATION_DONE is True  # set by the autouse fixture
    # No raise: the short-circuit returns before importlib touches the name.
    primitive_mod.register_single_module("hpc_agent.this.module.does.not.exist")
