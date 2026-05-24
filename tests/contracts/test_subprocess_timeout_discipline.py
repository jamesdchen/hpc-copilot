"""Contract: every ``subprocess.run`` in tests carries an explicit timeout.

A ``subprocess.run`` call with no ``timeout=`` is a latent CI-hang
risk: a regression that wedges the spawned process pins the runner
indefinitely. The fix is mechanical — pass ``timeout=`` — but the
mechanical-ness is exactly why it gets forgotten. The audit found 13
test files calling ``subprocess.run`` without a timeout.

This test ASTs every ``.py`` file under ``tests/`` and asserts each
``subprocess.run(...)`` call either:

1. Lives in :data:`_ALLOWLIST_FILES` (the canonical helper
   :mod:`tests._subprocess`, which already enforces the discipline at
   the wrapper level), OR
2. Includes ``timeout=`` in its keyword arguments.

The grandfathered set below lists test files that exist on ``main``
and haven't been migrated to :mod:`tests._subprocess` yet — the test
ships GREEN today so the contract lands without blocking PR B on a
mechanical migration sweep. The follow-up work is to migrate each
grandfathered file to ``from tests._subprocess import run_cli`` and
shrink :data:`_GRANDFATHERED` to ``set()``.

Don't grow :data:`_GRANDFATHERED`. New tests must use
:func:`tests._subprocess.run_cli` (or pass an explicit ``timeout=``);
the contract test will fail if a new file slips through.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"

# Files where ``subprocess.run`` without ``timeout=`` is permitted.
# The canonical helper enforces the discipline at the wrapper level
# (its own ``subprocess.run`` call always passes ``timeout=``, so the
# AST check is satisfied — this set is belt-and-suspenders only).
_ALLOWLIST_FILES: set[str] = {
    "tests/_subprocess.py",
}

# Test files on ``main`` that call ``subprocess.run`` without a
# ``timeout=`` kwarg. Each entry is the repo-relative path. The
# follow-up migration is to switch each to :func:`tests._subprocess.run_cli`
# (or add an explicit timeout) and remove its entry here. DO NOT GROW.
_GRANDFATHERED: set[str] = {
    "tests/meta/campaign/atoms/test_cli_campaign.py",
    "tests/cli/_helpers.py",
    "tests/cli/test_discover_search_dirs.py",
    "tests/integration/test_external_harness_compat.py",
    "tests/integration/test_hpc_preamble_integration.py",
    "tests/integration/test_status_integration.py",
    "tests/internal/test_audit_fixes.py",
    "tests/internal/test_bake_operations_json.py",
    "tests/ops/memory/test_interview.py",
    "tests/ops/memory/test_recall.py",
}


def _is_subprocess_run_call(node: ast.AST) -> bool:
    """True iff *node* is a ``subprocess.run(...)`` call expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "run":
        return False
    return isinstance(func.value, ast.Name) and func.value.id == "subprocess"


def _calls_without_timeout(path: Path) -> list[int]:
    """Return line numbers of ``subprocess.run`` calls in *path* that omit ``timeout=``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not _is_subprocess_run_call(node):
            continue
        kw_names = {kw.arg for kw in node.keywords}
        if "timeout" not in kw_names:
            offenders.append(node.lineno)
    return offenders


def test_subprocess_run_has_timeout_kwarg() -> None:
    """No new ``subprocess.run`` without ``timeout=`` in ``tests/``.

    New offenders must either pass ``timeout=`` or route through
    :func:`tests._subprocess.run_cli`. Don't append to
    :data:`_GRANDFATHERED`; the goal is to shrink it.
    """
    if not _TESTS_DIR.is_dir():
        # No tests directory means nothing to scan; trivially clean.
        return

    new_offenders: dict[str, list[int]] = {}
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWLIST_FILES or rel in _GRANDFATHERED:
            continue
        lines = _calls_without_timeout(path)
        if lines:
            new_offenders[rel] = lines

    assert not new_offenders, (
        "New test files call subprocess.run without a timeout= kwarg. "
        "Route through tests._subprocess.run_cli (which enforces "
        "timeout= at the wrapper) or pass an explicit timeout= in the "
        "call. Offenders:\n"
        + "\n".join(
            f"  {p}: lines {', '.join(str(line) for line in lines)}"
            for p, lines in sorted(new_offenders.items())
        )
    )


def test_grandfathered_files_still_exist_and_still_offend() -> None:
    """Keep :data:`_GRANDFATHERED` honest — stale entries must be pruned.

    If a grandfathered file no longer exists, was deleted, or was
    migrated to ``run_cli`` (no remaining un-timed ``subprocess.run``),
    the entry should be removed from :data:`_GRANDFATHERED` so the
    set shrinks over time toward empty.
    """
    stale: list[str] = []
    for rel in sorted(_GRANDFATHERED):
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(f"  {rel}: file no longer exists")
            continue
        if not _calls_without_timeout(path):
            stale.append(f"  {rel}: no longer calls subprocess.run without timeout=")

    assert not stale, (
        "Stale entries in _GRANDFATHERED — remove them so the set "
        "shrinks toward empty:\n" + "\n".join(stale)
    )
