"""CI lint: files annotated ``# @pure: no-io`` must not import I/O modules.

A file opting into the pure-no-IO contract declares (via a header comment
on one of its first 10 lines) that it neither performs I/O directly nor
imports modules whose sole purpose is to perform I/O. This lets later
PRs reorganize the package along subject lines while preserving the
guarantee that "pure" planning / scoring helpers stay deterministic and
side-effect-free.

The script:

* Walks every ``.py`` file under ``src/hpc_agent/``.
* Looks at the first 10 lines for an exact ``# @pure: no-io`` header.
* For annotated files, AST-walks all imports and rejects any of:
    - ``import subprocess`` / ``from subprocess import ...``
    - ``import socket`` / ``from socket import ...``
    - ``import requests`` / ``from requests import ...``
    - ``import paramiko`` / ``from paramiko import ...``
    - ``import urllib.request`` / ``from urllib import request`` /
      ``from urllib.request import ...``
* Also rejects, inside the same annotated files, direct calls to:
    - builtin ``open(...)``
    - ``<expr>.read_text(...)`` / ``.read_bytes(...)`` /
      ``.write_text(...)`` / ``.write_bytes(...)`` / ``.open(...)``
  These are the pathlib-flavoured I/O surface; the ``open(`` builtin is
  the bare-builtin form.

Exits 1 (with one ``path:lineno: <hint>`` per offender) if any annotated
file violates the contract; exits 0 when clean. Files without the
annotation are ignored entirely.

Today (PR 0b) no source files are annotated except the three pure
planning helpers (``constraints.py``, ``resubmit_batching.py``,
``throughput.py``); Phase 1 subject PRs will annotate the rest of the
``planning/`` move target.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"

PURE_MARKER = "# @pure: no-io"
HEADER_SCAN_LINES = 10

# Modules whose presence in an annotated file is by itself a violation.
# Matched on the import root (``urllib.request`` matches both the dotted
# module and the ``from urllib import request`` form).
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "subprocess",
        "socket",
        "requests",
        "paramiko",
        "urllib.request",
    }
)

# Method names that mean "this expression performs file I/O" — pathlib's
# read/write surface plus the ``.open(...)`` method. ``open(`` as a bare
# builtin is matched separately because it's a Name, not an Attribute.
FORBIDDEN_IO_METHODS: frozenset[str] = frozenset(
    {
        "read_text",
        "read_bytes",
        "write_text",
        "write_bytes",
        "open",
    }
)


def _has_pure_marker(path: Path) -> bool:
    """Return True iff one of the first ``HEADER_SCAN_LINES`` lines is
    exactly the pure marker (after stripping trailing whitespace).
    """
    try:
        with path.open(encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                if i >= HEADER_SCAN_LINES:
                    return False
                if raw.rstrip() == PURE_MARKER:
                    return True
    except (OSError, UnicodeDecodeError):
        return False
    return False


def _is_forbidden_module(name: str) -> bool:
    """A dotted module name is forbidden if it equals a forbidden root
    or starts with ``<forbidden>.`` (so ``urllib.request.Request`` would
    be caught via the ``urllib.request`` prefix)."""
    if name in FORBIDDEN_MODULES:
        return True
    return any(name.startswith(forbidden + ".") for forbidden in FORBIDDEN_MODULES)


def _check_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Find forbidden import statements. Returns ``(lineno, name)`` per
    offender."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden_module(alias.name):
                    findings.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            # Direct ``from <forbidden> import ...`` (mod itself
            # forbidden) — e.g. ``from subprocess import run``.
            if mod and _is_forbidden_module(mod):
                findings.append((node.lineno, mod))
                continue
            # ``from urllib import request`` — split form: the module is
            # ``urllib`` (allowed) and the imported name is ``request``.
            # Catch this by reconstructing ``<mod>.<name>`` per alias.
            if mod:
                for alias in node.names:
                    qualified = f"{mod}.{alias.name}"
                    if _is_forbidden_module(qualified):
                        findings.append((node.lineno, qualified))
    return findings


def _check_io_calls(tree: ast.AST) -> list[tuple[int, str]]:
    """Find direct I/O calls (builtin ``open``, pathlib read/write/open).

    Returns ``(lineno, hint_name)`` per offender. The hint is the
    callable name as it would appear in the source.
    """
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            findings.append((node.lineno, "open"))
        elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_IO_METHODS:
            findings.append((node.lineno, func.attr))
    return findings


def lint_file(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per finding for one annotated file.

    Files without the pure marker return ``[]`` without parsing.
    """
    if not _has_pure_marker(path):
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
    for lineno, name in _check_imports(tree):
        findings.append((lineno, f"forbidden I/O import in @pure file: {name}"))
    for lineno, name in _check_io_calls(tree):
        findings.append((lineno, f"forbidden I/O call in @pure file: {name}"))
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        for lineno, hint in lint_file(path):
            # Best-effort relative path for readability; fall back to
            # the absolute path if the file lives outside the repo
            # (e.g. when invoked under a tmp fixture in tests).
            try:
                rel: str = str(path.resolve().relative_to(REPO))
            except ValueError:
                rel = str(path)
            print(f"{rel}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} pure-file violation(s). "
            f"Remove the offending I/O imports/calls or drop the "
            f"'{PURE_MARKER}' header.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
