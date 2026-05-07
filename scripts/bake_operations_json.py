"""Bake the operations catalog to ``claude_hpc/operations.json`` for wheel installs.

Source-tree installs let :func:`claude_hpc._internal.operations.operations_catalog`
project the live ``@primitive`` registry into the catalog dict. Wheel
installs (``pip install claude-hpc`` from PyPI / a built wheel) ship
without ``docs/`` and the registry-loading path is the only thing that
keeps working — but if a downstream consumer disables decorator
registration (e.g. AOT-compiled Pyodide bundle, frozen interpreter)
the catalog goes empty.

This script writes a baked ``operations.json`` next to the package so
``operations_catalog`` has a deterministic fallback. The fallback is
already wired in :func:`operations_catalog` (the
``baked = _baked_path(); if baked.is_file(): ...`` branch); this
script is what populates it.

Same generator pattern as ``build_primitive_frontmatter.py``,
``build_primitive_index.py``, ``build_operations_index.py``,
``build_schemas.py``: pre-commit + CI run ``--check`` so editing a
``@primitive`` decorator without regenerating the JSON is a CI
failure.

Usage::

    uv run python scripts/bake_operations_json.py            # diff
    uv run python scripts/bake_operations_json.py --check    # CI gate
    uv run python scripts/bake_operations_json.py --write    # apply
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from claude_hpc._internal.operations import operations_catalog  # noqa: E402
from claude_hpc._internal.primitive import register_primitives  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "claude_hpc"
OUTPUT_PATH = PACKAGE_ROOT / "operations.json"


def _emit() -> str:
    """Render the catalog as a stable, sorted JSON document."""
    register_primitives()
    catalog = operations_catalog()
    # Already sorted by (verb, name) inside operations_catalog; serialise
    # with sort_keys so within each entry the field order is deterministic.
    return json.dumps(catalog, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    write = "--write" in sys.argv
    check = "--check" in sys.argv

    new = _emit()
    old = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.is_file() else ""
    rel = OUTPUT_PATH.relative_to(REPO_ROOT)

    if old == new:
        # Catalog already up-to-date. Surface the operation count so a
        # green run reports something useful (matches build_schemas's
        # "schemas up to date (NN)" UX).
        n = len(json.loads(new))
        print(f"operations.json up to date ({n} operations)")
        return 0

    if check:
        print(
            f"ERROR: {rel} is out of date — "
            "run scripts/bake_operations_json.py --write to regenerate",
            file=sys.stderr,
        )
        return 1

    if write:
        OUTPUT_PATH.write_text(new, encoding="utf-8")
        print(f"  wrote {rel}")
        n = len(json.loads(new))
        print(f"baked {n} operations")
        return 0

    # Default: print a diff so the human can preview without writing.
    print(f"--- a/{rel}")
    print(f"+++ b/{rel}")
    diff = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        n=3,
    )
    sys.stdout.write("".join(diff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
