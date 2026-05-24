"""CI lint: every Python file with @primitive(...) must be listed in
hpc_agent._kernel.registry.primitive._PRIMITIVE_MODULES (so the registry sees it).

Greps for the decorator literal, derives the module name from the
file path, and asserts membership. ~30 LOC. No runtime cost.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# After the src-layout migration, packages live under ``src/`` on disk
# but their import names don't include the ``src`` prefix. Strip the
# leading ``src/`` segment when converting paths to module names so
# ``src/hpc_agent/foo.py`` becomes ``hpc_agent.foo``.
_SRC_PREFIX = ("src",)


def file_to_modname(p: Path) -> str:
    rel = p.resolve().relative_to(REPO)
    parts = list(rel.with_suffix("").parts)
    if tuple(parts[: len(_SRC_PREFIX)]) == _SRC_PREFIX:
        parts = parts[len(_SRC_PREFIX) :]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def main() -> int:
    # Import the canonical list at runtime so this script tracks the
    # source of truth without re-typing it.
    sys.path.insert(0, str(REPO / "src"))
    from hpc_agent._kernel.registry.primitive import _PRIMITIVE_MODULES

    expected = set(_PRIMITIVE_MODULES)

    # The decorator's own definition site (and its module docstring,
    # which references @primitive(...) prose) is never a registration
    # site; skip explicitly so the regex doesn't pick up docstring
    # mentions.
    self_path = (REPO / "src" / "hpc_agent" / "_kernel" / "registry" / "primitive.py").resolve()

    found: set[str] = set()
    for p in REPO.rglob("*.py"):
        # Skip (match path *components* via ``p.parts``, not substrings
        # of ``str(p)`` — a substring check breaks on Windows, where
        # path components join with ``\`` not ``/``):
        # Two-tier skip:
        # - SKIP_ANYWHERE: any path-component match suppresses scanning
        #   (these names are unambiguously not registration sites
        #   wherever they appear in the tree).
        # - SKIP_AT_REPO_ROOT: only the FIRST repo-relative component
        #   counts. ``build`` and ``dist`` are common subdirectory names
        #   that also appear in legitimate package paths (e.g.
        #   ``src/hpc_agent/incorporation/build/``); restricting them to
        #   the repo root avoids false-positives that skip real primitive
        #   modules.
        SKIP_ANYWHERE = {
            ".git",
            "tests",
            "scripts",
            "hpc-agent-pro",
            "worktrees",
            ".venv",
            "venv",
            "__pycache__",
        }
        SKIP_AT_REPO_ROOT = {"build", "dist"}
        if set(p.parts) & SKIP_ANYWHERE:
            continue
        try:
            rel_parts = p.resolve().relative_to(REPO).parts
        except ValueError:
            rel_parts = ()
        if rel_parts and rel_parts[0] in SKIP_AT_REPO_ROOT:
            continue
        if p.resolve() == self_path:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Use AST so a ``@primitive(`` literal inside a triple-quoted
        # docstring or string constant doesn't false-trigger the way
        # the prior regex-only check did.
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        has_primitive_decorator = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(target, ast.Name) and target.id == "primitive":
                    has_primitive_decorator = True
                    break
                if isinstance(target, ast.Attribute) and target.attr == "primitive":
                    has_primitive_decorator = True
                    break
            if has_primitive_decorator:
                break
        if not has_primitive_decorator:
            continue
        found.add(file_to_modname(p))

    missing = found - expected
    if missing:
        print("ERROR: modules with @primitive(...) not in _PRIMITIVE_MODULES:")
        for m in sorted(missing):
            print(f"  {m}")
        print("\nAdd them to _PRIMITIVE_MODULES in hpc_agent/_kernel/registry/primitive.py.")
        return 1
    stale = expected - found
    if stale:
        # Warning rather than failure: some modules are intentionally
        # listed (e.g. agent_cli.py registers via the cmd_* dispatcher,
        # not via @primitive). A hard failure would require unwinding
        # those legitimate cases.
        print(
            "WARNING: _PRIMITIVE_MODULES entries with no @primitive(...) decorator:",
            file=sys.stderr,
        )
        for m in sorted(stale):
            print(f"  {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
