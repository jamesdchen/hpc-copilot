"""CI lint: the orchestrator may import only the backend seam, never a concrete backend.

Mechanizes the backend seam of issue #337 ("enforce the seam"). The
orchestrator packages — the flows under ``src/hpc_agent/{ops, meta,
recovery, incorporation, integration}`` — drive submission/recovery
without knowing *which* scheduler runs underneath. They reach a backend
only through the library-agnostic seam: the ``HPCBackend`` interface, the
registry lookups (``get_backend`` / ``get_backend_class`` /
``registered_backend_names`` / ``backend_requires_ssh``), and the
construction factory (``build_remote_backend`` / ``BackendBuildContext``
/ ``build_backend_class``). Those all live behind the package root
``hpc_agent.infra.backends`` (re-exported there), the construction module
``hpc_agent.infra.backends.remote_factory``, and the scheduler-as-data
``hpc_agent.infra.backends.profile``.

A *concrete* backend module — ``sge`` / ``slurm`` / their ``*_remote``
remote subclasses, and the internals ``_engine`` / ``_remote_base`` /
``_scripts`` / ``query`` — encodes one scheduler's command syntax. The
moment an orchestrator flow imports one of those by name, the seam has a
hole: that flow now changes when a scheduler's internals change, and
adding a third scheduler family means editing the flow. This lint forbids
it. ``infra`` itself (where the backends live), the wiring layer
``_wire``, and the tests are NOT orchestrator packages and are unscanned.

The rule is AST-scanned over every ``.py`` under each orchestrator
package, covering the evasive spellings the same way the sibling import
lints do (``lint_subject_imports.py`` / ``lint_library_knowledge.py``):
absolute and relative imports, top-level and lazy (function-body)
imports, the ``from hpc_agent.infra.backends import <concrete>`` alias
form, and ``from hpc_agent.infra.backends.<concrete> import name``. Every
violation surfaces a ``path:lineno: <message>`` line and the script
exits 1.

If a legitimate orchestrator import of a concrete module ever appears,
the fix is to route it through the seam — or, if it is genuinely
unavoidable, add a documented, cited entry to ``ALLOWLIST`` below (a
reviewed boundary decision with a diff), never to delete the rule.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"
# Dotted name the scan root corresponds to — anchors relative-import resolution.
PACKAGE_PREFIX = "hpc_agent"

# Orchestrator packages (relative to the scan root). These drive flows
# against a backend without knowing which scheduler runs underneath. NOT
# ``infra`` (the backends live there), NOT ``_wire`` (the wiring layer),
# NOT tests.
ORCHESTRATOR_PACKAGES: tuple[str, ...] = (
    "ops",
    "meta",
    "recovery",
    "incorporation",
    "integration",
)

# The backend package root and the two submodules the orchestrator may
# import: ``remote_factory`` (the construction factory) and ``profile``
# (scheduler-as-data, orchestrator-safe). The package root re-exports
# the whole seam — ``HPCBackend``, the registry functions, and the
# construction surface — so importing it (or a re-exported name off it)
# is always fine.
BACKENDS_ROOT = "hpc_agent.infra.backends"
ALLOWED_BACKEND_MODULES: frozenset[str] = frozenset(
    {
        BACKENDS_ROOT,
        f"{BACKENDS_ROOT}.remote_factory",
        f"{BACKENDS_ROOT}.profile",
    }
)

# Concrete backend class modules — each encodes one scheduler's command
# syntax / internals. Forbidden from the orchestrator.
FORBIDDEN_BACKEND_MODULES: frozenset[str] = frozenset(
    f"{BACKENDS_ROOT}.{name}"
    for name in (
        "sge",
        "slurm",
        "sge_remote",
        "slurm_remote",
        "_engine",
        "_remote_base",
        "_scripts",
        "query",
    )
)

# Documented, cited exceptions — ``(orchestrator_rel_path, concrete_module)``
# pairs the rule deliberately permits. Add an entry only as a reviewed boundary
# decision, with a comment citing why.
#
# #S5 / incident 6 (deployment-consistency guard): the post-deploy executor-
# existence preflight and the REPO_DIR↔deploy-target single derivation are
# module-level remote/deploy utilities (``deploy_target_for`` /
# ``executor_script_path`` / ``preflight_executor_exists``) co-located with the
# remote backend base. ``build_submit_spec`` and ``submit_flow`` reach for them
# to verify the deploy *before* scheduling — routing through the HPCBackend
# interface would not fit (these are not per-backend methods).
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("incorporation/build/submit_spec.py", "hpc_agent.infra.backends._remote_base"),
        ("ops/submit_flow.py", "hpc_agent.infra.backends._remote_base"),
        # range-kill (B4): kill.py reuses the backend's canonical
        # ``_expand_task_range`` to map a global undone-task set to per-wave
        # local array indices — the ONE definition of the index grammar; a
        # local reimplementation would be the mirror-twin the boundary exists
        # to prevent (2026-07-16 deployed-bug-sweep B4).
        ("ops/monitor/kill.py", "hpc_agent.infra.backends.query"),
        # ``inspect-deployment`` is the general case of S5's
        # ``preflight_executor_exists`` — a read-only listing over the same
        # throttled transport. It reuses the SAME ``deploy_target_for``
        # REPO_DIR↔deploy-target derivation (one owner, no inline duplicate of
        # ``remote_path.rstrip('/')``) to resolve the path from a run's
        # journaled remote_path. Same reviewed boundary as the two callers above.
        ("ops/inspect_deployment.py", "hpc_agent.infra.backends._remote_base"),
    }
)


def _module_package(rel: str) -> str:
    """Dotted package containing the module at scan-root-relative *rel*.

    Both ``pkg/mod.py`` and ``pkg/__init__.py`` resolve ``from . import x``
    against ``pkg``, so the filename is simply dropped.
    """
    return ".".join([PACKAGE_PREFIX, *rel.split("/")[:-1]])


def _iter_import_candidates(tree: ast.AST, module_package: str) -> list[tuple[int, str]]:
    """``(lineno, dotted_name)`` for every module an import statement could bind.

    Includes imports inside functions (a lazy import crosses the seam just
    the same). Relative imports are resolved against *module_package* — a
    ``from ..infra.backends import slurm`` climbs parents and binds the
    concrete module like its absolute spelling. ``from pkg import name``
    additionally yields ``pkg.name`` per alias, because when ``name`` is a
    submodule that form binds it just like ``import pkg.name`` (this is what
    catches ``from hpc_agent.infra.backends import slurm``).
    """
    out: list[tuple[int, str]] = []
    pkg_parts = module_package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > len(pkg_parts):
                    continue  # climbs above the distribution root — broken import
                base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module
            if not module:
                continue
            out.append((node.lineno, module))
            out.extend(
                (node.lineno, f"{module}.{alias.name}") for alias in node.names if alias.name != "*"
            )
    return out


def _forbidden_module(module: str) -> str | None:
    """If *module* names (or reaches into) a forbidden concrete backend module,
    return that concrete module's dotted name. Otherwise return None.

    An allowed seam module is never forbidden even though it shares the
    ``hpc_agent.infra.backends`` prefix — checked first so a
    ``remote_factory`` / ``profile`` import (or a re-export off the package
    root) is cleared before the concrete-module test runs.
    """
    if module in ALLOWED_BACKEND_MODULES:
        return None
    for concrete in FORBIDDEN_BACKEND_MODULES:
        if module == concrete or module.startswith(concrete + "."):
            return concrete
    return None


def _orchestrator_package(rel: str) -> str | None:
    """The orchestrator package a scan-root-relative file lives in, or None."""
    head = rel.split("/", 1)[0]
    return head if head in ORCHESTRATOR_PACKAGES else None


def lint_file(path: Path, scan_root: Path) -> list[tuple[int, str]]:
    """``(lineno, message)`` per concrete-backend import violation in *path*."""
    rel = path.resolve().relative_to(scan_root.resolve()).as_posix()
    if _orchestrator_package(rel) is None:
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    findings: list[tuple[int, str]] = []
    # ``from pkg.concrete import name`` yields both ``pkg.concrete`` and
    # ``pkg.concrete.name`` candidates for one line — report each crossing once.
    seen: set[tuple[int, str]] = set()
    for lineno, module in _iter_import_candidates(tree, _module_package(rel)):
        concrete = _forbidden_module(module)
        if concrete is None:
            continue
        if (rel, concrete) in ALLOWLIST:
            continue
        if (lineno, concrete) in seen:
            continue
        seen.add((lineno, concrete))
        findings.append(
            (
                lineno,
                f"backend-boundary import: orchestrator file {rel} imports concrete "
                f"backend module {module}. The orchestrator may use only the backend "
                f"seam — the HPCBackend interface and the registry/factory functions "
                f"re-exported from {BACKENDS_ROOT} (or {BACKENDS_ROOT}.remote_factory / "
                f"{BACKENDS_ROOT}.profile). Route through the seam; if this import is "
                f"genuinely unavoidable, add a cited ALLOWLIST entry in "
                f"scripts/lint_backend_boundary.py (a reviewed boundary decision, #337).",
            )
        )
    findings.sort(key=lambda f: f[0])
    return findings


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for py in sorted(root.rglob("*.py")):
        for lineno, message in lint_file(py, root):
            print(f"{py}:{lineno}: {message}", file=sys.stderr)
            failures += 1
    if failures:
        print(f"lint_backend_boundary: {failures} violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
