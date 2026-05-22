"""CI lint: every Python file with @primitive(...) must be listed in
hpc_agent._internal.primitive._PRIMITIVE_MODULES (so the registry sees it).

Greps for the decorator literal, derives the module name from the
file path, and asserts membership. ~30 LOC. No runtime cost.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Decorator application: optional leading whitespace, then ``@primitive(``.
# Excludes occurrences inside docstrings / comments where the literal
# is escaped or wrapped in backticks.
_DECORATOR_RE = re.compile(r"^\s*@primitive\(", re.MULTILINE)

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
    from hpc_agent._internal.primitive import _PRIMITIVE_MODULES

    expected = set(_PRIMITIVE_MODULES)

    # The decorator's own definition site (and its module docstring,
    # which references @primitive(...) prose) is never a registration
    # site; skip explicitly so the regex doesn't pick up docstring
    # mentions.
    self_path = (REPO / "src" / "hpc_agent" / "_internal" / "primitive.py").resolve()

    found: set[str] = set()
    for p in REPO.rglob("*.py"):
        # Skip (match path *components* via ``p.parts``, not substrings
        # of ``str(p)`` — a substring check breaks on Windows, where
        # path components join with ``\`` not ``/``):
        # - .git/             — git's own files
        # - tests/, scripts/  — never registration sites
        # - hpc-agent-pro/    — sibling plugin package; its primitives
        #   register through the plugin seam, not _PRIMITIVE_MODULES
        # - worktrees/        — .claude/worktrees agent-isolated copies
        #   may shadow the real source tree and double-count primitives
        # - .venv/, venv/, build/, dist/ — install / build artifacts
        if set(p.parts) & {
            ".git",
            "tests",
            "scripts",
            "hpc-agent-pro",
            "worktrees",
            ".venv",
            "venv",
            "build",
            "dist",
        }:
            continue
        if p.resolve() == self_path:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Match the decorator-application form only — avoids false
        # positives from docstrings, comments, and the @primitive
        # decorator's own definition site.
        if not _DECORATOR_RE.search(text):
            continue
        found.add(file_to_modname(p))

    missing = found - expected
    if missing:
        print("ERROR: modules with @primitive(...) not in _PRIMITIVE_MODULES:")
        for m in sorted(missing):
            print(f"  {m}")
        print("\nAdd them to _PRIMITIVE_MODULES in hpc_agent/_internal/primitive.py.")
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
