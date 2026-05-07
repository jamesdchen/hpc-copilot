#!/usr/bin/env python3
"""Fail if any primitive doc body is still ``_Documentation pending._``.

Pre-commit + CI gate. The frontmatter-rewrite script
(``scripts/build_primitive_frontmatter.py``) creates each primitive's
doc with a stub body when an atom is added; this gate enforces that the
body gets filled in before merge.

Usage::

    uv run python scripts/check_no_pending_primitive_docs.py

Exit codes:
    0 — every primitive doc has a real body
    1 — one or more docs still carry the placeholder
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRIMITIVES_DIR = REPO_ROOT / "docs" / "primitives"
PLACEHOLDER = "_Documentation pending._"


def main() -> int:
    if not PRIMITIVES_DIR.is_dir():
        print(f"ERROR: {PRIMITIVES_DIR} not found", file=sys.stderr)
        return 1
    offenders = []
    for path in sorted(PRIMITIVES_DIR.glob("*.md")):
        if path.name == "README.md":
            continue
        if PLACEHOLDER in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(REPO_ROOT))
    if not offenders:
        return 0
    print(
        f"ERROR: {len(offenders)} primitive doc(s) still carry the '{PLACEHOLDER}' placeholder:",
        file=sys.stderr,
    )
    for p in offenders:
        print(f"  {p}", file=sys.stderr)
    print(
        "\nFill in each body using the template in "
        "docs/internals/adding-a-primitive.md (agent-facing vs internal).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
