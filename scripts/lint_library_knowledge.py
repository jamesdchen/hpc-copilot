"""CI lint: library-knowledge packages may be imported only at declared assembly points.

Mechanizes the "core dispatches, never branches" rule of the library-knowledge
boundary (docs/internals/engineering-principles.md): modules that encode
knowledge of a specific third-party library — solver adapters, axis-matcher
pattern modules — are *implementations behind a core-owned seam*. Core code
reaches them only through the seam's dispatcher/registry; the seam's wiring
lives at a small number of **declared assembly points**, enumerated below.

Without this lint the boundary erodes silently: each new feature that
imports ``solver_adapters.petsc`` directly adds another core location that
must change when adapter #2 arrives (and another place experiment-blind
library knowledge leaks into general control flow). With it, adding an
assembly point is a *reviewed edit to this file* — a conscious boundary
decision with a diff — rather than an incidental import.

Two rules, both AST-scanned over every ``.py`` under ``src/hpc_agent``:

1. **Boundary**: any import that binds a knowledge package — absolute or
   relative, top-level or lazy, the package root or a submodule, including
   the ``from parent import package`` and ``from package import submodule``
   alias forms — from a file that is neither inside that package nor in its
   assembly-point list is a violation.
2. **Growth trigger**: once a knowledge package has two or more member
   modules, only its declared *registry* assembly point may keep binding
   member modules by name; every other assembly point must consume the
   package-root API. This is the enforced form of "the family's second
   member collapses inline branching into the registry" — the lint stays
   quiet at one member and fires the moment adapter #2 lands.

Tests are exempt (they may exercise anything directly).

List hygiene is enforced: a declared assembly point that no longer exists,
or that no longer imports its package, fails the lint — stale entries get
cleaned, so the list stays an accurate map of where the boundary is wired.

Same scan/report shape as ``lint_subject_imports.py``: every violation
surfaces a ``path:lineno: <message>`` line and the script exits 1.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"
# Dotted name the scan root corresponds to — anchors relative-import resolution.
PACKAGE_PREFIX = "hpc_agent"


@dataclass(frozen=True)
class KnowledgePackage:
    """One package whose modules encode knowledge of a third-party library."""

    #: Implementation dir, relative to the scan root.
    package_dir: str
    #: The library-agnostic surface core code should call instead.
    seam: str
    #: Files (relative to the scan root) allowed to import the package.
    assembly_points: tuple[str, ...]
    #: The ONE assembly point that may keep binding member modules by name
    #: once the family has >= 2 members (the registry/dispatcher). Must be
    #: listed in ``assembly_points``.
    registry: str


KNOWLEDGE_PACKAGES: dict[str, KnowledgePackage] = {
    "hpc_agent.experiment_kit.solver_adapters": KnowledgePackage(
        package_dir="experiment_kit/solver_adapters",
        seam="experiment_kit.checkpoint_formats (formats) / the adapter registry-to-be",
        assembly_points=(
            # The checkpoint-format registry — names each format's adapter.
            "experiment_kit/checkpoint_formats.py",
            # Materializes the solver-instrumented wrapper (entry_point.solver).
            "incorporation/wrap_entry_point.py",
            # Surfaces per-candidate solver detection on the scan output.
            "ops/detect_entry_point.py",
        ),
        registry="experiment_kit/checkpoint_formats.py",
    ),
    "hpc_agent.experiment_kit.axis_matcher.matchers": KnowledgePackage(
        package_dir="experiment_kit/axis_matcher/matchers",
        seam="experiment_kit.axis_matcher (the classifier dispatcher)",
        assembly_points=(
            # The pattern-priority dispatcher — the one importer of matchers.
            "experiment_kit/axis_matcher/_classifier.py",
        ),
        registry="experiment_kit/axis_matcher/_classifier.py",
    ),
}


def _module_package(rel: str) -> str:
    """Dotted package containing the module at scan-root-relative *rel*.

    Both ``pkg/mod.py`` and ``pkg/__init__.py`` resolve ``from . import x``
    against ``pkg``, so the filename is simply dropped.
    """
    return ".".join([PACKAGE_PREFIX, *rel.split("/")[:-1]])


def _iter_import_candidates(tree: ast.AST, module_package: str) -> list[tuple[int, str]]:
    """``(lineno, dotted_name)`` for every module an import statement could bind.

    Includes imports inside functions (lazy imports cross the boundary just
    the same). Relative imports are resolved against *module_package* — a
    ``from ..experiment_kit.solver_adapters import petsc`` climbs parents and
    crosses the boundary like its absolute spelling. ``from pkg import name``
    additionally yields ``pkg.name`` per alias, because when ``name`` is a
    submodule that form binds it just like ``import pkg.name``.
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


def _imports_package(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _member_count(spec: KnowledgePackage, scan_root: Path) -> int:
    """Family members in the package: public ``*.py`` modules plus public
    subpackage dirs. Underscore-prefixed names are implementation details
    (the shared ``_common.py`` a registry collapse naturally extracts), not
    a second adapter — they must not arm the growth trigger."""
    pkg = scan_root / spec.package_dir
    if not pkg.is_dir():
        return 0
    modules = [
        p for p in pkg.glob("*.py") if p.name != "__init__.py" and not p.name.startswith("_")
    ]
    subpackages = [
        p
        for p in pkg.iterdir()
        if p.is_dir() and not p.name.startswith("_") and (p / "__init__.py").is_file()
    ]
    return len(modules) + len(subpackages)


def _binds_member_module(
    module: str, package: str, spec: KnowledgePackage, scan_root: Path
) -> bool:
    """True when *module* names a member module of the package — as opposed to
    the package root or a root-level re-exported function/class."""
    if module == package:
        return False
    first = module[len(package) + 1 :].split(".")[0]
    pkg = scan_root / spec.package_dir
    return (pkg / f"{first}.py").is_file() or (pkg / first / "__init__.py").is_file()


def lint_file(path: Path, scan_root: Path) -> list[tuple[int, str]]:
    """``(lineno, message)`` per knowledge-package import violation in *path*."""
    rel = path.resolve().relative_to(scan_root.resolve()).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    findings: list[tuple[int, str]] = []
    # One finding per (line, package, rule) — ``from pkg.sub import name``
    # yields both ``pkg.sub`` and ``pkg.sub.name`` candidates for one line.
    seen: set[tuple[int, str, str]] = set()
    for lineno, module in _iter_import_candidates(tree, _module_package(rel)):
        for package, spec in KNOWLEDGE_PACKAGES.items():
            if not _imports_package(module, package):
                continue
            if rel.startswith(spec.package_dir + "/"):
                continue
            if rel not in spec.assembly_points:
                if (lineno, package, "boundary") not in seen:
                    seen.add((lineno, package, "boundary"))
                    findings.append(
                        (
                            lineno,
                            f"library-knowledge import: {rel} imports {module}, but is "
                            f"not a declared assembly point for {package}. Route through "
                            f"the seam ({spec.seam}) — or, if this file IS a new assembly "
                            f"point, add it to KNOWLEDGE_PACKAGES in "
                            f"scripts/lint_library_knowledge.py (a reviewed boundary "
                            f"decision).",
                        )
                    )
                continue
            if (
                rel != spec.registry
                and _member_count(spec, scan_root) >= 2
                and _binds_member_module(module, package, spec, scan_root)
                and (lineno, package, "registry") not in seen
            ):
                seen.add((lineno, package, "registry"))
                findings.append(
                    (
                        lineno,
                        f"growth trigger: {rel} binds member module {module}, but "
                        f"{package} now has multiple members — collapse inline library "
                        f"branching into the registry ({spec.registry}) and consume the "
                        f"package-root API here instead "
                        f"(docs/internals/engineering-principles.md).",
                    )
                )
    findings.sort(key=lambda f: f[0])
    return findings


def lint_assembly_point_hygiene(scan_root: Path) -> list[str]:
    """Stale-entry guard: every declared assembly point must exist AND still
    import its package — otherwise the list drifts from reality and stops
    being a map of where the boundary is wired."""
    problems: list[str] = []
    for package, spec in KNOWLEDGE_PACKAGES.items():
        if spec.registry not in spec.assembly_points:
            problems.append(
                f"{spec.registry}: declared as the registry for {package} but is not "
                "in its assembly_points — fix the KNOWLEDGE_PACKAGES entry"
            )
        for rel in spec.assembly_points:
            path = scan_root / rel
            if not path.is_file():
                problems.append(
                    f"{rel}: declared as an assembly point for {package} but does not "
                    "exist — remove the stale entry"
                )
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                problems.append(f"{rel}: declared assembly point is unparseable")
                continue
            candidates = _iter_import_candidates(tree, _module_package(rel))
            if not any(_imports_package(module, package) for _, module in candidates):
                problems.append(
                    f"{rel}: declared as an assembly point for {package} but no longer "
                    "imports it — remove the stale entry"
                )
    return problems


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for problem in lint_assembly_point_hygiene(root):
        print(f"{root}: {problem}", file=sys.stderr)
        failures += 1
    for py in sorted(root.rglob("*.py")):
        for lineno, message in lint_file(py, root):
            print(f"{py}:{lineno}: {message}", file=sys.stderr)
            failures += 1
    if failures:
        print(f"lint_library_knowledge: {failures} violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
