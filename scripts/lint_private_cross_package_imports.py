"""CI lint: no cross-package import of a leading-underscore PRIVATE symbol.

Sibling to ``lint_backend_boundary.py`` / ``lint_subject_imports.py`` — same
per-file AST walk with absolute + relative import resolution, same
``main(scan_root=None) -> int`` shape. Where the backend lint mechanizes ONE
seam, this one mechanizes a whole-tree convention: a leading single underscore
means "package-private", so a symbol like ``run_record._current_homedir`` is a
promise that only code inside ``run_record``'s own package may reach it. When a
DIFFERENT package imports that private name, the promise is silently broken —
the "private" symbol has become a cross-package API with none of the review a
public promotion gets (the W2 finding: four such symbols had accreted 30+
cross-package importers between them).

The rule
--------

For every ``from X import name`` (absolute or relative) in a file under
``src/hpc_agent`` where

* ``X`` resolves into ``hpc_agent`` (``hpc_agent`` or ``hpc_agent.*``), and
* ``name`` is a leading-single-underscore private symbol — starts with ``_``,
  is not a dunder (``__x``), and is not the bare ``_`` — and
* the importing file's package is NOT inside ``X``'s parent package (the
  package that OWNS the private symbol: for ``X = hpc_agent.state.run_record``
  that is ``hpc_agent.state``; equal-or-descendant counts as inside, so
  same-package and sub-package imports are always fine),

a violation is emitted — UNLESS the triple ``(importer_rel, module, name)`` is
listed in the external allowlist ``scripts/private_cross_import_allowlist.txt``.

Submodule guard
---------------

``from hpc_agent.pkg import _mod`` where ``pkg/_mod.py`` (or
``pkg/_mod/__init__.py``) exists on disk is importing a private *submodule*,
not a symbol — a different thing (module privacy, not symbol privacy). Those
are skipped: the guard checks the filesystem before flagging.

The allowlist is a shrink-only burn-down ledger
-----------------------------------------------

``scripts/private_cross_import_allowlist.txt`` lists the remaining sanctioned
cross-package private imports (one ``importer_rel :: module :: _symbol`` triple
per line, ``#`` comments allowed). Two guardrails keep it honest, mirroring
``lint_mirror_ledger.py``:

* A NEW cross-package private import that is not allowlisted fires immediately
  (the fire path) — the debt cannot grow silently.
* A STALE allowlist entry — a triple whose import no longer exists in the tree
  (the symbol was promoted, or the import was deleted) — also fires, forcing
  the entry's removal. The ledger only shrinks.

``--print-current`` prints every current cross-package private import in the
ledger's triple format (used to SEED the allowlist after a promotion wave).

Exits 1 on any finding; exits 0 when clean. The fire paths are exercised in
``tests/scripts/test_lint_private_cross_package_imports.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"
# Dotted name the scan root corresponds to — anchors relative-import resolution.
PACKAGE_PREFIX = "hpc_agent"
ALLOWLIST_REL = Path("scripts/private_cross_import_allowlist.txt")


def _module_package(rel: str) -> str:
    """Dotted package containing the module at scan-root-relative *rel*.

    Both ``pkg/mod.py`` and ``pkg/__init__.py`` resolve ``from . import x``
    against ``pkg``, so the filename is simply dropped.
    """
    return ".".join([PACKAGE_PREFIX, *rel.split("/")[:-1]])


def _resolve_module(node: ast.ImportFrom, module_package: str) -> str | None:
    """Absolute dotted module for an ``ImportFrom``, resolving relatives.

    Returns None for a relative import that climbs above the distribution root
    (a broken import) or a bare relative ``from . import x`` with no module.
    """
    if node.level:
        pkg_parts = module_package.split(".")
        if node.level > len(pkg_parts):
            return None
        base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
        return f"{base}.{node.module}" if node.module else base
    return node.module


def _iter_from_imports(tree: ast.AST, module_package: str) -> list[tuple[int, str, str]]:
    """``(lineno, module, name)`` for every ``from module import name`` alias.

    Includes imports inside function bodies (a lazy import crosses the boundary
    just the same). ``import x`` / ``import x.y`` bind whole modules, never a
    leading-underscore symbol, so only ``ImportFrom`` is walked. ``*`` imports
    carry no name and are skipped.
    """
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _resolve_module(node, module_package)
        if not module:
            continue
        for alias in node.names:
            if alias.name == "*":
                continue
            out.append((node.lineno, module, alias.name))
    return out


def _is_private_symbol(name: str) -> bool:
    """A leading-SINGLE-underscore private symbol: ``_x`` but not ``__x`` / ``_``."""
    return name.startswith("_") and not name.startswith("__") and name != "_"


def _in_hpc_agent(module: str) -> bool:
    """Does *module* name (or reach into) the shipped ``hpc_agent`` package?"""
    return module == PACKAGE_PREFIX or module.startswith(PACKAGE_PREFIX + ".")


def _is_inside(pkg: str, parent: str) -> bool:
    """Is *pkg* equal to *parent* or a descendant package of it?"""
    return pkg == parent or pkg.startswith(parent + ".")


def _module_to_path(module: str, scan_root: Path) -> Path | None:
    """Filesystem path (sans extension) the dotted *module* maps to under scan_root.

    ``scan_root`` corresponds to ``hpc_agent``, so the leading ``hpc_agent``
    component is dropped: ``hpc_agent.state`` -> ``<scan_root>/state``.
    """
    parts = module.split(".")
    if parts[0] != PACKAGE_PREFIX:
        return None
    return scan_root.joinpath(*parts[1:])


def _is_private_submodule(module: str, name: str, scan_root: Path) -> bool:
    """Is ``module.name`` a private SUBMODULE on disk rather than a symbol?

    ``from hpc_agent.pkg import _mod`` where ``pkg/_mod.py`` or
    ``pkg/_mod/__init__.py`` exists is a module import — module privacy, not the
    symbol privacy this lint governs.
    """
    base = _module_to_path(module, scan_root)
    if base is None:
        return False
    return (base / f"{name}.py").is_file() or (base / name / "__init__.py").is_file()


def _collect_violations(scan_root: Path) -> list[tuple[str, str, str, int]]:
    """Every ``(importer_rel, module, name, lineno)`` cross-package private import.

    Unfiltered by the allowlist — this is the raw set both ``main`` (which then
    subtracts the allowlist) and ``--print-current`` (which seeds it) build on.
    """
    resolved_root = scan_root.resolve()
    out: list[tuple[str, str, str, int]] = []
    for py in sorted(scan_root.rglob("*.py")):
        rel = py.resolve().relative_to(resolved_root).as_posix()
        module_package = _module_package(rel)
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for lineno, module, name in _iter_from_imports(tree, module_package):
            if not _in_hpc_agent(module):
                continue
            if not _is_private_symbol(name):
                continue
            if _is_private_submodule(module, name, scan_root):
                continue
            owner_package = module.rsplit(".", 1)[0] if "." in module else module
            if _is_inside(module_package, owner_package):
                continue
            out.append((rel, module, name, lineno))
    return out


def _load_allowlist(repo: Path | None = None) -> set[tuple[str, str, str]]:
    """Sanctioned ``(importer_rel, module, _symbol)`` triples from the ledger.

    Loader idiom mirrors ``lint_mirror_ledger._load_allowlist``: blank lines and
    ``#`` comments are ignored; each remaining line is three ``::``-separated
    fields.
    """
    root = repo if repo is not None else REPO
    path = root / ALLOWLIST_REL
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    entries: set[tuple[str, str, str]] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [p.strip() for p in stripped.split("::")]
        if len(parts) == 3 and all(parts):
            entries.add((parts[0], parts[1], parts[2]))
    return entries


#: Sanctioned cross-package private imports, loaded at import time. Tests
#: monkeypatch this to exercise the allowlist path against a synthetic tree.
ALLOWLIST: set[tuple[str, str, str]] = _load_allowlist()


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    triples = _collect_violations(root)
    current_keys = {(rel, module, name) for (rel, module, name, _lineno) in triples}
    failures = 0
    for rel, module, name, lineno in sorted(triples):
        if (rel, module, name) in ALLOWLIST:
            continue
        print(
            f"{rel}:{lineno}: private-cross-package import: {rel} imports the "
            f"leading-underscore symbol {name!r} from {module}, whose owning package "
            f"is not the importer's. A single leading underscore means package-private; "
            f"promote {name!r} to a public name (drop the underscore, keep a back-compat "
            f"alias) and import that — or, if genuinely sanctioned, add the triple "
            f"'{rel} :: {module} :: {name}' to {ALLOWLIST_REL.as_posix()} (a shrink-only "
            f"burn-down ledger).",
            file=sys.stderr,
        )
        failures += 1
    # Stale-entry guard: an allowlisted triple whose import is gone must be
    # removed — the ledger shrinks as symbols are promoted, never rots.
    for rel, module, name in sorted(ALLOWLIST):
        if (rel, module, name) not in current_keys:
            print(
                f"{ALLOWLIST_REL.as_posix()}: stale entry "
                f"'{rel} :: {module} :: {name}' — this cross-package private import no "
                f"longer exists in the tree; remove it (the ledger only shrinks).",
                file=sys.stderr,
            )
            failures += 1
    if failures:
        print(f"lint_private_cross_package_imports: {failures} issue(s)", file=sys.stderr)
        return 1
    return 0


def _print_current(scan_root: Path | None = None) -> None:
    """Print every current cross-package private import as a ledger triple."""
    root = scan_root if scan_root is not None else SCAN_ROOT
    lines = sorted(
        {f"{rel} :: {module} :: {name}" for (rel, module, name, _l) in _collect_violations(root)}
    )
    for line in lines:
        print(line)


if __name__ == "__main__":
    if "--print-current" in sys.argv[1:]:
        _print_current()
        raise SystemExit(0)
    raise SystemExit(main())
