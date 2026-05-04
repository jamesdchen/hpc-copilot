"""CI lint: every Python file with @primitive(...) must be listed in
claude_hpc._internal._primitive._PRIMITIVE_MODULES (so the registry sees it).

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


def file_to_modname(p: Path) -> str:
    rel = p.resolve().relative_to(REPO)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def main() -> int:
    # Import the canonical list at runtime so this script tracks the
    # source of truth without re-typing it.
    sys.path.insert(0, str(REPO))
    from claude_hpc._internal._primitive import _PRIMITIVE_MODULES

    expected = set(_PRIMITIVE_MODULES)

    # The decorator's own definition site (and its module docstring,
    # which references @primitive(...) prose) is never a registration
    # site; skip explicitly so the regex doesn't pick up docstring
    # mentions.
    self_path = (REPO / "claude_hpc" / "_internal" / "_primitive.py").resolve()

    found: set[str] = set()
    for p in REPO.rglob("*.py"):
        s = str(p)
        # Skip:
        # - .git/             — git's own files
        # - tests/, scripts/  — never registration sites
        # - .claude/worktrees/ — agent-isolated worktrees may shadow the
        #   real source tree with their own copies; treating those as
        #   first-class would double-count primitives
        # - .venv/, venv/, build/, dist/ — install / build artifacts
        if (
            "/.git/" in s
            or "/tests/" in s
            or "/scripts/" in s
            or "/.claude/worktrees/" in s
            or "/.venv/" in s
            or "/venv/" in s
            or "/build/" in s
            or "/dist/" in s
        ):
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
        print("\nAdd them to _PRIMITIVE_MODULES in claude_hpc/_internal/_primitive.py.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
