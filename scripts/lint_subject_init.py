"""CI lint: subject ``__init__.py`` files are docstring-only.

The post-reorg convention (documented in ``docs/architecture.md``):
each subject under ``src/hpc_agent/ops/<subject>/`` and
``src/hpc_agent/meta/<subject>/`` has an ``__init__.py`` that holds at
most a module docstring. NO eager re-exports — callers reach the leaf
module directly. The reasons:

* The cross-subject-imports lint relies on imports naming the leaf
  module (``hpc_agent.ops.monitor.status``) so it can attribute every
  symbol to a subject. Re-exports in ``__init__.py`` collapse that
  to ``hpc_agent.ops.monitor`` and break attribution.
* Registry registration runs by *importing* each primitive module, so
  re-exports add no value (the decorator side-effect happens either
  way) but they DO grow the package's import-time blast radius.
* Empty ``__init__.py`` makes "what does this subject export?" trivially
  scannable: read the leaf modules' top-level names.

This script walks every ``ops/<subject>/__init__.py`` and
``meta/<subject>/__init__.py`` and rejects any statement that isn't:

* a module-level docstring (``ast.Expr`` of ``ast.Constant``), or
* ``from __future__ import …`` (no-op at runtime; purely a parser hint).

Anything else — a re-export, a conditional import, a constant
definition — is a contract violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "hpc_agent"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_future_import(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module == "__future__"


def main() -> int:
    violations: list[str] = []
    for role_dir in (SRC / "ops", SRC / "meta"):
        if not role_dir.is_dir():
            continue
        for subject_dir in sorted(p for p in role_dir.iterdir() if p.is_dir()):
            init = subject_dir / "__init__.py"
            if not init.is_file():
                continue
            try:
                src = init.read_text(encoding="utf-8")
            except OSError:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                violations.append(f"{init.relative_to(REPO).as_posix()}: SyntaxError: {exc}")
                continue
            for node in tree.body:
                if _is_docstring(node) or _is_future_import(node):
                    continue
                violations.append(
                    f"{init.relative_to(REPO).as_posix()}:L{node.lineno}: "
                    f"non-docstring statement ({type(node).__name__}). "
                    "Subject __init__.py must be docstring-only — move "
                    "the symbol into a leaf module and import it from "
                    "there directly."
                )

    if violations:
        print("ERROR: subject __init__.py files must be docstring-only:")
        for v in violations:
            print(f"  {v}")
        print(
            "\nWhy: empty subject __init__ keeps the cross-subject-imports "
            "lint's attribution honest (leaf-named imports identify the "
            "subject; re-exports collapse that) and avoids growing the "
            "package's import-time blast radius."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
