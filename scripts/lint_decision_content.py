#!/usr/bin/env python3
"""Decision-content drift lint.

Cross-checks operationally-shared markdown blocks (decision trees, dialog
templates) across files that paraphrase the same content. Each file marks
the canonical block with HTML comments::

    <!-- decision-content:<tag> start -->
    ... content ...
    <!-- decision-content:<tag> end -->

This lint extracts every such block, groups by tag, and asserts the
content (whitespace-normalised) is byte-identical across all files
sharing the tag. Fails on drift with the two file paths that disagree.

When a maintainer edits a shared block in one file, the lint catches
the omission to update the others.

Usage::

    python scripts/lint_decision_content.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_MARKER = re.compile(
    r"<!--\s*decision-content:([a-z][a-z0-9-]*)\s*start\s*-->"
    r"(.*?)"
    r"<!--\s*decision-content:\1\s*end\s*-->",
    re.DOTALL,
)

# Files known to participate. Add new entries here when introducing a
# new shared block in another file. The lint walks only these files.
PARTICIPATING_FILES: list[Path] = [
    REPO_ROOT / "src" / "slash_commands" / "skills" / "hpc-classify-axis" / "SKILL.md",
    REPO_ROOT / "src" / "slash_commands" / "commands" / "submit-hpc.md",
]


def extract_blocks(path: Path) -> dict[str, str]:
    """Return ``{tag: normalised_content}`` for every block in *path*.

    Whitespace normalisation: strip trailing whitespace per line and
    strip blank lines at the block boundaries. Trailing-whitespace
    drift (which renders identically) doesn't fail the lint.
    """
    text = path.read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for m in _MARKER.finditer(text):
        tag = m.group(1)
        content = m.group(2).strip()
        normalised = "\n".join(line.rstrip() for line in content.splitlines())
        if tag in blocks:
            print(
                f"ERROR: {path.relative_to(REPO_ROOT)} has duplicate decision-content:{tag} block",
                file=sys.stderr,
            )
            sys.exit(1)
        blocks[tag] = normalised
    return blocks


def main() -> int:
    by_tag: dict[str, list[tuple[Path, str]]] = {}
    for path in PARTICIPATING_FILES:
        if not path.is_file():
            print(
                f"ERROR: participating file missing: {path.relative_to(REPO_ROOT)}",
                file=sys.stderr,
            )
            return 1
        for tag, content in extract_blocks(path).items():
            by_tag.setdefault(tag, []).append((path, content))

    errors: list[str] = []
    for tag, occurrences in sorted(by_tag.items()):
        if len(occurrences) < 2:
            # Only one file has this tag — nothing to cross-check.
            continue
        first_path, first_content = occurrences[0]
        for path, content in occurrences[1:]:
            if content != first_content:
                errors.append(
                    f"decision-content:{tag!r} differs between "
                    f"{first_path.relative_to(REPO_ROOT)} and "
                    f"{path.relative_to(REPO_ROOT)}"
                )

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(
        f"decision-content blocks consistent ({len(by_tag)} unique tag(s) "
        f"across {len(PARTICIPATING_FILES)} file(s))"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
