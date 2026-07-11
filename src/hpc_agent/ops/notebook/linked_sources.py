"""Import → source-file resolution: the ONE definition shared by two verbs.

Extracted verbatim from ``notebook-lint``'s rule-3 machinery (2026-07-07, the
draft-context plan's "one resolution definition" requirement). ``notebook-lint``
reports imports that resolve to a file under a caller ``source_root``; the
``notebook-draft-context`` projection resolves the SAME way to point the drafting
agent at each engine's defining file. Both must resolve identically — so the
resolution lives here once and both import it, rather than forking a second copy.

Pure, stdlib-only (``ast`` + the shared hashing primitive): judges import ORIGIN
IDENTITY only, never import content/semantics (the Q1 boundary flag). Relative
imports (``level > 0``) are skipped — a relative origin is inside the same
package, not a cross-``source_root`` link. An import that resolves to nothing is
stdlib / site-packages, never a link (returned as unresolved, never a finding).
"""

from __future__ import annotations

import ast
from pathlib import Path

from hpc_agent._wire.actions.notebook_lint import LinkedSource
from hpc_agent.state.audit_source import sha256_normalized

__all__ = [
    "imported_modules",
    "resolve_module_file",
    "resolve_linked_sources",
]


def resolve_module_file(module: str, root_dirs: list[Path]) -> Path | None:
    """Resolve a dotted *module* name to a file under one of *root_dirs*.

    ``foo.bar`` → ``foo/bar.py`` or ``foo/bar/__init__.py`` under each root; the
    first hit (roots in declared order) wins. ``None`` when nothing resolves —
    an unresolvable import is stdlib / site-packages, never a link.

    When the module's FIRST component names the root itself (``src.data.loading``
    under root ``src`` — the repo-root-relative import style), the root-prefixed
    probe would double the prefix (``src/src/data/loading.py``); the leading
    component is also tried stripped. Every candidate stays under a declared
    root, so the lint boundary (links resolve UNDER a source_root) is unchanged.
    """
    parts = module.split(".")
    rel = Path(*parts)
    for root in root_dirs:
        candidates = [root / rel.with_suffix(".py"), root / rel / "__init__.py"]
        if parts[0] == root.name:
            if len(parts) == 1:
                candidates.append(root / "__init__.py")
            else:
                stripped = Path(*parts[1:])
                candidates.extend(
                    (root / stripped.with_suffix(".py"), root / stripped / "__init__.py")
                )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def imported_modules(tree: ast.Module) -> list[str]:
    """Dotted module names an ``import`` / ``from`` statement brings in.

    For ``from pkg import name`` both ``pkg`` and ``pkg.name`` are candidates
    (``name`` may be a submodule file); the resolver keeps whichever exists.
    Relative imports (``level > 0``) are skipped — a relative origin is inside
    the same package, not a cross-``source_root`` link this rule reports.
    """
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0 or not node.module:
                continue
            modules.append(node.module)
            modules.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def resolve_linked_sources(
    tree: ast.Module,
    experiment_dir: Path,
    root_dirs: list[Path],
) -> list[LinkedSource]:
    """Report imports resolving to a file under a declared ``source_root``.

    Deduped by resolved file (two import forms can name one origin). ``module_sha``
    is the shared hashing primitive over the file text — the exact value T9
    recomputes to drift-check the link.
    """
    seen_files: set[Path] = set()
    linked: list[LinkedSource] = []
    for module in imported_modules(tree):
        resolved = resolve_module_file(module, root_dirs)
        if resolved is None:
            continue
        resolved = resolved.resolve()
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        try:
            rel = str(resolved.relative_to(experiment_dir.resolve()))
        except ValueError:
            rel = str(resolved)
        linked.append(
            LinkedSource(
                module=module,
                file=rel,
                module_sha=sha256_normalized(resolved.read_text(encoding="utf-8")),
            )
        )
    return linked
