"""CI lint: no hand ``add_argument`` in the registry parser walk — the bake is the truth.

The ``hpc-agent`` CLI surface is generated twice from ONE source: at run time the
argparse tree is built by walking the primitive registry
(:func:`hpc_agent.cli.parser._register_from_registry`, which for every primitive
calls :func:`_add_standard_args` to inject exactly the flags its ``CliShape``
declares), and at build time ``operations.json`` is baked from the same
``CliShape`` declarations. As long as every flag flows through a primitive's
``CliShape.args``, those two renderings agree — the bake is the whole CLI truth.

The trap (latency premortem A2)
-------------------------------

A flag spliced directly into the walk with a hand ``parser.add_argument(...)``
(as ``describe --schema`` once was) exists at run time but is INVISIBLE to the
bake: ``operations.json`` — and everything downstream of it (``capabilities``,
``describe``, the MCP catalog, the fast-path single-verb parser) — never learns
the flag exists. The two renderings silently diverge, and a caller reading the
baked catalog is told a flag the live CLI accepts does not exist.

The rule
--------

Inside :func:`hpc_agent.cli.parser._register_from_registry` there must be NO
``.add_argument(...)`` call. Per-primitive flags belong in the primitive's
``CliShape.args`` (injected via :func:`_add_standard_args`, a *different*
function this lint deliberately does not scan); the standard ``--spec`` /
``--experiment-dir`` / ``--dry-run`` injectors live there too. The walk may only
create subparsers (``sub.add_parser`` / ``add_subparsers``) and delegate to
``_add_standard_args`` — never add a flag itself.

The target function vanishing (a rename that would silently disable the guard)
is itself a failure, mirroring ``lint_mirror_ledger``'s stale-entry guard.

Exits 1 on any finding; exits 0 when clean. The fire path is exercised in
``tests/scripts/test_lint_parser_bake_truth.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: The registry-walk parser file and the function within it the bake mirrors.
TARGET_REL = Path("src/hpc_agent/cli/parser.py")
GUARDED_FUNCTION = "_register_from_registry"


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    """Return the top-level (or nested) ``FunctionDef`` named *name*, if present."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _add_argument_calls(func: ast.FunctionDef) -> list[int]:
    """Line numbers of every ``<expr>.add_argument(...)`` call inside *func*."""
    out: list[int] = []
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            out.append(node.lineno)
    return out


def find_violations(source: str, filename: str) -> list[str]:
    """Return violation strings for *source* (empty == clean).

    Injectable (source + filename) so a test can plant a synthetic
    ``_register_from_registry`` carrying a hand ``add_argument``.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [f"{filename}: could not parse ({exc})"]

    func = _find_function(tree, GUARDED_FUNCTION)
    if func is None:
        return [
            f"{filename}: function {GUARDED_FUNCTION!r} not found — the parser-bake "
            "truth guard scans it for hand ``add_argument`` calls; a rename would "
            "silently disable the guard. Update scripts/lint_parser_bake_truth.py."
        ]

    violations: list[str] = []
    for lineno in _add_argument_calls(func):
        violations.append(
            f"{filename}:{lineno}: hand ``add_argument`` inside {GUARDED_FUNCTION}() — "
            "a flag spliced into the registry walk is INVISIBLE to the operations.json "
            "bake, so the live CLI and the baked catalog silently diverge (latency A2). "
            "Move the flag into the primitive's CliShape.args (injected via "
            "_add_standard_args) so the bake stays the whole CLI truth."
        )
    return violations


def main(target: Path | None = None) -> int:
    path = target if target is not None else REPO / TARGET_REL
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{path}: could not read ({exc})", file=sys.stderr)
        return 1
    violations = find_violations(source, path.as_posix())
    for v in violations:
        print(v, file=sys.stderr)
    if violations:
        print(f"lint_parser_bake_truth: {len(violations)} issue(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
