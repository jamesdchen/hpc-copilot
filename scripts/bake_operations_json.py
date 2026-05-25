"""Bake the operations catalog to ``hpc_agent/operations.json``.

:func:`hpc_agent._kernel.registry.operations.operations_catalog` projects the
live ``@primitive`` registry into the catalog dict; the registry is
the only runtime source of truth. This script writes a redundant
on-disk snapshot at ``src/hpc_agent/operations.json`` so the catalog
is greppable / diff-able without booting Python, and CI can fail when
a ``@primitive`` decorator drifts from the committed snapshot.

The runtime catalog does NOT read this file — see the docstring on
``operations_catalog`` — but the bake check IS the gate that keeps
the registry projection deterministic across PRs.

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

from hpc_agent._kernel.registry.operations import operations_catalog  # noqa: E402
from hpc_agent._kernel.registry.primitive import get_registry, register_primitives  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "hpc_agent"
OUTPUT_PATH = PACKAGE_ROOT / "operations.json"


def _emit() -> str:
    """Render the catalog as a stable, sorted JSON document.

    Filters to core-only primitives — the baked artifact lives inside
    the core package (``src/hpc_agent/operations.json``) and CI bakes
    it without ``hpc-agent-pro`` installed. When a dev has the plugin
    installed in the same env, its primitives are skipped so the
    artifact stays stable. Plugins publish their primitives at runtime
    via the ``hpc_agent.plugins`` entry-point group; baking their
    catalog (if needed) is each plugin's responsibility.
    """
    register_primitives()
    core_names = {
        name
        for name, meta in get_registry().items()
        if (getattr(meta.func, "__module__", "") or "").startswith("hpc_agent.")
    }
    catalog = [entry for entry in operations_catalog() if entry["name"] in core_names]
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
