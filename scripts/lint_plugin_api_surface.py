"""CI lint: the notebook-render example plugin may import only the DECLARED core API surface.

The shipped, CI-gated example plugin
``examples/plugins/hpc-agent-notebook-render/`` reaches ~18 core module
paths — most of them PAST any documented seam (``ops.notebook.canonical``,
``state.data_trace``, ``_wire.actions.*``, the private
``_kernel.registry.primitive`` / ``cli._dispatch``). None of that surface
was ever frozen, so a core reorg that renames or moves one of those modules
would silently break a plugin the project ships and tests. This lint freezes
the real surface as a versioned allowlist and pins it in BOTH directions.

Two-directional pinning (:func:`main`, the default run):

1. **Stay-inside** (:func:`check_within_allowlist`) — every ``hpc_agent.*``
   import the plugin actually makes must be declared in
   :data:`ALLOWED_PLUGIN_IMPORTS`, down to the symbol. A plugin edit that
   reaches a new, undeclared core module fails here until the allowlist +
   ``docs/reference/plugin-api-contract.md`` are widened in the same PR.
2. **Anti-drift** (:func:`check_allowlist_resolves`) — every allowlisted
   module/symbol must still resolve in the installed core. A core reorg that
   moves ``ops.notebook.canonical.build_canonical_view`` (or drops it) fails
   here, forcing a *conscious* contract update rather than a silent break of
   the CI-gated plugin.

The optional ``--fire-path`` leg (:func:`fire_path`) is the CI ``plugins``
job's proof that the surface is not just import-resolvable but functionally
whole: with the plugin installed it registers ``notebook-render`` /
``notebook-ingest-signoffs`` as real CLI verbs, each carrying a
:class:`~hpc_agent.cli._dispatch.CliShape`. It is guarded so a core-only
checkout (plugin absent) skips cleanly.

The import scan ``ast.walk``\\s the WHOLE tree, so ``TYPE_CHECKING``-guarded
and function-local imports (``render.py`` hides ``SectionView`` under
``TYPE_CHECKING`` and the ``notebook_record_receipt`` models inside a
function body) are frozen exactly like the top-level ones.

To extend the surface: widen :data:`ALLOWED_PLUGIN_IMPORTS`, add the row in
``docs/reference/plugin-api-contract.md``, and bump :data:`CONTRACT_VERSION`
only on a *narrowing* — all in one PR. See that doc's "How to extend".
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Make ``hpc_agent`` importable from a source checkout for the anti-drift +
# fire-path legs, matching lint_plugin_manifests.py's bootstrap idiom.
sys.path.insert(0, str(REPO / "src"))

# The shipped, CI-gated example plugin whose import surface this lint freezes.
PLUGIN_SRC_ROOT = REPO / "examples" / "plugins" / "hpc-agent-notebook-render" / "src"

# Bump ONLY on a narrowing of the surface (a removed module/symbol) — a
# widening is backward-compatible for the plugin and needs no version bump.
CONTRACT_VERSION = "1"

# The core distribution root. An import target is "core" when it names this
# package or a submodule of it.
CORE_PREFIX = "hpc_agent"

# The frozen plugin -> core API surface: each permitted core module path mapped
# to the exact symbols the plugin may import from it. ``("*",)`` means the whole
# module is imported wholesale (``from hpc_agent import errors`` binds the
# ``errors`` submodule). Built from the AST scan of the real plugin — every
# entry is exercised by ``examples/plugins/hpc-agent-notebook-render/``; the
# ``test_allowlist_covers_the_real_plugin_exactly`` test forbids a dead entry.
# Grouped to mirror docs/reference/plugin-api-contract.md.
ALLOWED_PLUGIN_IMPORTS: dict[str, tuple[str, ...]] = {
    # -- sanctioned seams -------------------------------------------------
    "hpc_agent.errors": ("*",),
    "hpc_agent.infra.io": ("append_jsonl_line",),
    "hpc_agent._wire.plugin_manifest": ("PluginManifest",),
    # -- primitive registry ----------------------------------------------
    "hpc_agent._kernel.registry.primitive": ("primitive", "SideEffect"),
    # -- CLI shape --------------------------------------------------------
    "hpc_agent.cli._dispatch": ("CliShape",),
    # -- wire action models ----------------------------------------------
    "hpc_agent._wire.actions.decision_journal": ("AppendDecisionInput",),
    "hpc_agent._wire.actions.notebook_record_receipt": (
        "NotebookReceiptEntry",
        "NotebookRecordReceiptSpec",
    ),
    # -- ops verb entrypoints --------------------------------------------
    "hpc_agent.ops.decision.journal": ("append_decision",),
    "hpc_agent.ops.notebook.audit_view": ("HUMAN_REQUIRED", "SectionView"),
    "hpc_agent.ops.notebook.canonical": (
        "AuditConfig",
        "build_canonical_view",
        "read_recorded_config",
    ),
    "hpc_agent.ops.notebook.record_receipt_op": ("notebook_record_receipt",),
    "hpc_agent.ops.notebook.render_store": ("write_render",),
    # -- state APIs -------------------------------------------------------
    "hpc_agent.state.audit_source": ("parse_percent_source",),
    "hpc_agent.state.data_trace": ("ingest_trace", "make_record", "stdlib_measure"),
    "hpc_agent.state.decision_journal": ("read_decisions",),
    "hpc_agent.state.notebook_audit": ("audit_section",),
    "hpc_agent.state.utterances": ("append_utterance", "is_harness_injected"),
    # -- mapreduce trace constants ---------------------------------------
    "hpc_agent.execution.mapreduce.data_trace_contract": (
        "TRACE_SOURCE_RUNNER",
        "TRACE_TRANSPORT_FILENAME",
    ),
}

# The plugin distribution + the verbs its @primitive decorators register.
PLUGIN_MODULE = "hpc_agent_notebook_render"
PLUGIN_VERBS = ("notebook-render", "notebook-ingest-signoffs")


def _is_core_module(name: str) -> bool:
    """True if a dotted import target names the core package or a submodule."""
    return name == CORE_PREFIX or name.startswith(CORE_PREFIX + ".")


def scan_plugin_imports(src_root: Path) -> set[tuple[str, str]]:
    """Every ``(module, symbol)`` an import under *src_root* binds against core.

    ``ast.walk`` visits the WHOLE tree, so ``TYPE_CHECKING``-guarded and
    function-local imports are collected exactly like top-level ones. For
    ``from M import a, b`` each name yields ``(M, a)`` / ``(M, b)``; a
    ``from M import *`` yields ``(M, "*")``; a plain ``import a.b.c`` yields
    ``(a.b.c, "*")`` (the module bound wholesale). Relative imports
    (``level > 0``) are intra-plugin and skipped; only ``hpc_agent`` /
    ``hpc_agent.*`` targets are kept.

    Pure: reads files, resolves nothing. The submodule-binding form
    ``from hpc_agent import errors`` is left as ``("hpc_agent", "errors")``;
    :func:`_resolve_allow_key` reconciles it with the allowlisted
    ``hpc_agent.errors`` entry.
    """
    found: set[tuple[str, str]] = set()
    for py in sorted(Path(src_root).rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_core_module(alias.name):
                        found.add((alias.name, "*"))
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative -> intra-plugin, never core
                    continue
                module = node.module or ""
                if not _is_core_module(module):
                    continue
                for alias in node.names:
                    found.add((module, alias.name))
    return found


def _resolve_allow_key(module: str, symbol: str, allow: dict[str, tuple[str, ...]]) -> str | None:
    """Which allowlist key (if any) authorises ``(module, symbol)``.

    ``module`` may be an allowlisted module path directly; or, for the
    submodule-binding form ``from parent import module`` (e.g.
    ``from hpc_agent import errors``), the imported *name* completes an
    allowlisted path ``parent.name`` that is permitted wholesale (tuple
    ``("*",)``). Returns the matched allowlist key, or None.
    """
    if module in allow:
        return module
    combined = f"{module}.{symbol}"
    if combined in allow and allow[combined] == ("*",):
        return combined
    return None


def check_within_allowlist(
    scanned: set[tuple[str, str]], allow: dict[str, tuple[str, ...]]
) -> list[str]:
    """Violation strings for any scanned import the allowlist does not permit.

    A ``(module, symbol)`` fires when the module is absent from *allow*, or
    when the module is allowlisted but the symbol is not in its tuple (unless
    the tuple is the wholesale ``("*",)``).
    """
    violations: list[str] = []
    for module, symbol in sorted(scanned):
        key = _resolve_allow_key(module, symbol, allow)
        if key is None:
            violations.append(
                f"undeclared core import: the plugin imports {symbol!r} from {module!r}, "
                f"which is not in the pinned plugin->core API surface (ALLOWED_PLUGIN_IMPORTS, "
                f"contract v{CONTRACT_VERSION}). Route through a declared seam, or widen the "
                f"allowlist + add the row in docs/reference/plugin-api-contract.md "
                f"(see 'How to extend')."
            )
            continue
        if key != module:
            # from <parent> import <allowlisted-submodule> — the whole module is
            # allowed (its tuple is ``("*",)``); nothing further to check.
            continue
        allowed_symbols = allow[key]
        if allowed_symbols == ("*",) or symbol in allowed_symbols:
            continue
        violations.append(
            f"undeclared core symbol: the plugin imports {symbol!r} from {module!r}, which is "
            f"allowlisted but only for {allowed_symbols!r} (contract v{CONTRACT_VERSION}). Widen "
            f"the tuple + update docs/reference/plugin-api-contract.md if this symbol is intended."
        )
    return violations


def unused_allowlist_entries(
    scanned: set[tuple[str, str]], allow: dict[str, tuple[str, ...]]
) -> set[str]:
    """Allowlist keys not exercised by any scanned import (dead entries).

    Keeps the surface honest in the third direction: the allowlist must not
    grow entries the plugin no longer imports (a reorg that dropped a plugin
    import would otherwise leave a stale, misleading permission).
    """
    used: set[str] = set()
    for module, symbol in scanned:
        key = _resolve_allow_key(module, symbol, allow)
        if key is not None:
            used.add(key)
    return set(allow) - used


def check_allowlist_resolves(allow: dict[str, tuple[str, ...]]) -> list[str]:
    """Violation strings for any allowlisted module/symbol that no longer resolves.

    The anti-drift leg: ``importlib``-imports each module and ``getattr``\\s
    each non-``"*"`` symbol. A core reorg that renames/moves/drops an entry
    fails here, forcing a conscious contract update.
    """
    violations: list[str] = []
    for module in sorted(allow):
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — any import failure is a drift signal
            violations.append(
                f"anti-drift: allowlisted core module {module!r} no longer imports ({exc!r}). "
                f"A core reorg moved it; update ALLOWED_PLUGIN_IMPORTS + "
                f"docs/reference/plugin-api-contract.md (contract v{CONTRACT_VERSION})."
            )
            continue
        for symbol in allow[module]:
            if symbol == "*":
                continue
            if not hasattr(mod, symbol):
                violations.append(
                    f"anti-drift: allowlisted symbol {module}.{symbol} no longer resolves "
                    f"({symbol!r} missing). A core reorg moved/renamed it; update "
                    f"ALLOWED_PLUGIN_IMPORTS + docs/reference/plugin-api-contract.md "
                    f"(contract v{CONTRACT_VERSION})."
                )
    return violations


def fire_path() -> int:
    """Prove the plugin actually registers its two verbs, each with a CliShape.

    CI-only (the ``plugins`` job's notebook-render leg): needs the plugin
    installed. Guarded — where the plugin is absent it prints a skip and
    returns 0, so a core-only checkout is unaffected.
    """
    try:
        plugin = importlib.import_module(PLUGIN_MODULE)
    except ImportError:
        print(
            f"{PLUGIN_MODULE} not installed — fire-path is a no-op (runs only in the CI "
            "plugins job where the notebook-render plugin is installed)"
        )
        return 0

    from hpc_agent._kernel.registry.primitive import get_meta, get_registry, register_primitives
    from hpc_agent.cli._dispatch import CliShape

    register_primitives()
    # Import the plugin's primitive modules for their @primitive side effects.
    # register_primitives already does this via the entry point; importing them
    # explicitly makes the fire-path self-contained and order-independent.
    for modname in getattr(plugin, "primitive_modules", ()):
        importlib.import_module(modname)
    registry = get_registry()

    violations: list[str] = []
    for verb in PLUGIN_VERBS:
        if verb not in registry:
            violations.append(
                f"fire-path: plugin verb {verb!r} did not register — the plugin->core API "
                "surface it imports has broken its @primitive registration."
            )
            continue
        meta = get_meta(verb)
        if not isinstance(meta.cli, CliShape):
            violations.append(
                f"fire-path: plugin verb {verb!r} registered but its .cli is "
                f"{type(meta.cli).__name__}, not CliShape — the CLI-shape seam drifted."
            )
    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        return 1
    print(
        f"plugin fire-path OK (contract v{CONTRACT_VERSION}): "
        f"{', '.join(PLUGIN_VERBS)} register with a CliShape"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Default: stay-inside + anti-drift. ``--fire-path``: the CI plugins-job leg."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--fire-path" in args:
        return fire_path()

    scanned = scan_plugin_imports(PLUGIN_SRC_ROOT)
    violations = check_within_allowlist(scanned, ALLOWED_PLUGIN_IMPORTS)
    violations += check_allowlist_resolves(ALLOWED_PLUGIN_IMPORTS)
    if violations:
        for v in violations:
            print(v, file=sys.stderr)
        print(f"lint_plugin_api_surface: {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print(
        f"plugin API surface OK (contract v{CONTRACT_VERSION}, "
        f"{len(ALLOWED_PLUGIN_IMPORTS)} modules)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
