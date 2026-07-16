"""CI lint: a heavy third-party library must not be imported at module scope.

Latency plan B1 (``docs/plans/latency-elimination-2026-07-16/``,
claim ``cold.jsonschema-import-side-effect`` /
``cold.registry-populated-by-importing-the-world``): importing the primitive
registry imports the world — every ``@primitive`` module is imported so its
decorator fires, and a single module-scope ``import jsonschema`` in any one of
them drags the whole ``jsonschema`` + ``referencing`` stack (and, if present,
its slow ``rfc3987``-family format dependency) into *cold* startup, even for a
CLI turn that never validates a spec. ``jsonschema`` is only needed at the
moment a ``--spec`` payload is validated, so the import belongs inside the
function that validates — paid on first-validate, never at import.

The discipline: import a heavy library LAZILY, inside the function that uses it
(the same shape ``_kernel/contract/schema.py`` and the campaign atoms already
use). This lint is that discipline's enforcement row — a NEW module-scope
``import jsonschema`` fails CI instead of silently re-inflating cold startup.

What it flags
-------------

An ``import <heavy>`` / ``from <heavy> import ...`` whose imported root module is
in :data:`HEAVY_IMPORTS` **and** whose statement sits at module scope (left
margin, ``col_offset == 0``) — i.e. it executes unconditionally at module load.

Deliberately NOT flagged
------------------------

* A LAZY import inside a function/method body (``col_offset > 0``) — the whole
  point; that is where a heavy import belongs.
* An import guarded by ``if TYPE_CHECKING:`` — indented, so ``col_offset > 0``,
  and never executed at runtime anyway.
* Any library NOT in :data:`HEAVY_IMPORTS`. The set is deliberately tiny: only
  libraries whose module-scope import measurably inflates cold startup and that
  are *not* needed at import time. ``pydantic`` / ``referencing`` are pervasive
  module-scope authoring surfaces and are intentionally absent.

ALLOWLIST escape valve
----------------------

A file that genuinely must import a heavy library eagerly adds a cited entry to
:data:`ALLOWLIST` (scan-root-relative posix path) — the same escape valve
``lint_atomic_durable_writes.py`` / ``lint_remote_read_ack.py`` use. It is empty
today: after the B1 lazy-import move (budget.py, converged.py) no module in the
tree imports a heavy library at module scope, so HEAD is clean.

Every violation surfaces a ``path:lineno: heavy import not lazy: ...`` line and
the script exits 1. The fire path is pinned by
``tests/scripts/test_lint_lazy_heavy_imports.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src"

# Root module names whose module-scope import inflates cold startup and that are
# only needed at call time (never at import time). Match is on the *root* of a
# dotted name: ``import jsonschema.validators`` and ``from jsonschema.validators
# import X`` both resolve to root ``jsonschema``.
HEAVY_IMPORTS: frozenset[str] = frozenset(
    {
        "jsonschema",
    }
)

# Cited exemptions: scan-root-relative posix paths of a module that legitimately
# imports a heavy library at module scope. Empty by construction — add an entry
# only as a reviewed decision (and prefer moving the import into the function
# that uses it).
ALLOWLIST: frozenset[str] = frozenset()


def _import_roots(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Return the root module name(s) an import statement pulls in.

    ``import a.b, c`` → ``["a", "c"]``; ``from a.b import x`` → ``["a"]``.
    A bare relative ``from . import x`` (no module) contributes nothing.
    """
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    # ImportFrom: a relative import (level > 0) is in-package, never a heavy
    # third-party library, and ``node.module`` is None for ``from . import x``.
    if node.level and node.level > 0:
        return []
    if not node.module:
        return []
    return [node.module.split(".", 1)[0]]


def lint_file(path: Path, scan_root: Path | None = None) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per module-scope heavy import in *path*."""
    root = scan_root if scan_root is not None else SCAN_ROOT
    rel = _relpath(path, root)
    if rel in ALLOWLIST:
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        # Module-scope = left margin. A lazy import inside a function/method, or
        # one under ``if TYPE_CHECKING:``, is indented (col_offset > 0) and is
        # exactly what this lint wants people to do, so it is skipped.
        if node.col_offset != 0:
            continue
        for heavy in _import_roots(node):
            if heavy in HEAVY_IMPORTS:
                findings.append(
                    (
                        node.lineno,
                        f"heavy import not lazy: module-scope `import {heavy}` "
                        f"forces {heavy} into cold startup for every registry "
                        f"import; move it inside the function that uses it "
                        f"(paid on first use, not at import). Add a cited "
                        f"ALLOWLIST entry {rel!r} only if eager import is required.",
                    )
                )
    findings.sort(key=lambda f: f[0])
    return findings


def _relpath(path: Path, scan_root: Path) -> str:
    try:
        return path.resolve().relative_to(scan_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def iter_targets(scan_root: Path) -> list[Path]:
    pkg = scan_root / "hpc_agent"
    if not pkg.exists():
        return []
    return sorted(p for p in pkg.rglob("*.py") if p.is_file())


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        for lineno, hint in lint_file(path, root):
            try:
                disp = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                disp = path.as_posix()
            print(f"{disp}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} module-scope heavy import(s). Importing the primitive "
            f"registry imports the world, so one module-scope heavy import "
            f"inflates cold startup for every turn (latency plan B1). Move the "
            f"import inside the function that uses it, or add a cited ALLOWLIST "
            f"entry in scripts/lint_lazy_heavy_imports.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
