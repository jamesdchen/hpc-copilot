"""Tests for the baked operations.json fallback path.

The fallback exists for wheel installs that ship without ``docs/`` on
the file system: when no @primitive registers (e.g. AOT-frozen
interpreter) ``operations_catalog`` falls through to reading the
shipped ``operations.json``. The bake script
(``scripts/bake_operations_json.py``) is what populates it.

These tests pin three invariants:

1. The shipped JSON is non-empty + lists every registered primitive.
2. The bake's ``--check`` mode is the gate (CI uses it; pre-commit
   wires the writer on @primitive edits).
3. The fallback path in :func:`operations_catalog` returns the same
   shape as the registry path (so wheel-install consumers see the
   same envelope).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import hpc_agent
from tests._registry_helpers import core_only_operations_catalog, core_only_registry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACKAGE_ROOT = Path(hpc_agent.__file__).parent
BAKED = PACKAGE_ROOT / "operations.json"
BAKE_SCRIPT = REPO_ROOT / "scripts" / "bake_operations_json.py"


def test_baked_file_exists():
    """The wheel-install fallback file ships with the package."""
    assert BAKED.is_file(), f"missing baked file: {BAKED}"


def test_baked_file_is_non_empty_list():
    """The baked JSON is a list of operation entries."""
    payload = json.loads(BAKED.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    assert len(payload) > 0


def test_baked_lists_every_registered_primitive():
    """Every primitive in the live registry appears in the baked JSON.

    The baked artifact is core's ``operations.json``; plugin primitives
    (when ``hpc-agent-pro`` is co-installed) are filtered out so the
    comparison is core-vs-core.
    """
    registry_names = {meta.name for meta in core_only_registry().values()}
    baked = json.loads(BAKED.read_text(encoding="utf-8"))
    baked_names = {entry["name"] for entry in baked}
    missing = registry_names - baked_names
    extra = baked_names - registry_names
    assert not missing and not extra, (
        f"baked operations.json drifted from registry "
        f"(missing: {sorted(missing)}, extra: {sorted(extra)}). "
        f"Run scripts/bake_operations_json.py --write to regenerate."
    )


def test_baked_entry_shape_matches_registry():
    """Each baked entry has the same keys as the registry projection."""
    live = {entry["name"]: entry for entry in core_only_operations_catalog()}
    baked = {entry["name"]: entry for entry in json.loads(BAKED.read_text(encoding="utf-8"))}
    for name, live_entry in live.items():
        assert name in baked, f"baked missing {name}"
        assert set(baked[name].keys()) == set(live_entry.keys()), (
            f"baked entry for {name} has different keys than registry projection"
        )


def test_check_mode_reports_clean():
    """The CI gate path: ``--check`` reports up-to-date on a clean tree."""
    result = subprocess.run(
        [sys.executable, str(BAKE_SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"bake_operations_json.py --check failed unexpectedly:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "up to date" in result.stdout
