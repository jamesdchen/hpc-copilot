"""CI lint: subjects under ``ops/`` and ``meta/`` must not cross-import.

In the post-reorg 5-role layout, each top-level directory under
``src/hpc_agent/ops/`` and ``src/hpc_agent/meta/`` is a *subject* — a
self-contained vertical slice (e.g. ``ops/jobs/``, ``ops/files/``,
``meta/registry/``). Subjects compose horizontally via the shared
``hpc_agent.infra.*`` and ``hpc_agent.state.*`` substrate; they MUST NOT
reach sideways into each other's internals.

This lint enforces that rule by AST-scanning every ``.py`` file under
``src/hpc_agent/ops/<subject>/`` and ``src/hpc_agent/meta/<subject>/``
and rejecting any ``from hpc_agent.<role>.<other_subject>...`` import
where ``<other_subject>`` differs from the file's own subject.

Allowed cross-cutting roots (these aren't subjects, they're substrate):

* ``hpc_agent.infra.*``
* ``hpc_agent.state.*``

The script handles absent directories gracefully — today (PR 0b) neither
``ops/`` nor ``meta/`` exists, so the script is a no-op (exit 0) until
Phase 1 subject PRs land. Once subjects move in, every per-file import
violation gets a ``path:lineno: cross-subject import: ...`` line and the
script exits 1.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src" / "hpc_agent"

# Top-level role directories under ``src/hpc_agent/`` whose immediate
# children are subjects. Add new role roots here when the reorg grows.
SUBJECT_ROLES: tuple[str, ...] = ("ops", "meta")

# Per-role allowed import prefixes that aren't themselves subjects.
# Imports under these prefixes are always fine regardless of which
# subject the importing file lives in.
ALLOWED_NON_SUBJECT_ROOTS: tuple[str, ...] = (
    "hpc_agent.infra",
    "hpc_agent.state",
)


def _subject_of(path: Path, role_root: Path) -> str | None:
    """Return the subject name for a file under ``role_root``, or None
    if the file isn't inside a subject directory (e.g. it's directly in
    ``role_root`` itself, not in a child)."""
    try:
        rel = path.resolve().relative_to(role_root.resolve())
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) < 2:
        # ``role_root/<file>.py`` — not inside a subject.
        return None
    return parts[0]


def _imported_subject(module: str, role: str) -> str | None:
    """If ``module`` is a ``hpc_agent.<role>.<subject>...`` import,
    return ``<subject>``. Otherwise return None.

    Examples (role=``ops``):

    * ``hpc_agent.ops.jobs.api`` → ``"jobs"``
    * ``hpc_agent.ops.jobs``     → ``"jobs"``
    * ``hpc_agent.ops``          → None (no subject in the path)
    * ``hpc_agent.meta.registry``→ None (different role)
    """
    prefix = f"hpc_agent.{role}."
    if module == f"hpc_agent.{role}":
        return None
    if not module.startswith(prefix):
        return None
    rest = module[len(prefix) :]
    head = rest.split(".", 1)[0]
    return head or None


def _is_allowed_non_subject(module: str) -> bool:
    for root in ALLOWED_NON_SUBJECT_ROOTS:
        if module == root or module.startswith(root + "."):
            return True
    return False


def _iter_imports(tree: ast.AST) -> list[tuple[int, str]]:
    """Yield ``(lineno, module_name)`` for every absolute import
    statement in ``tree``. Relative imports are excluded — they can't
    name a different subject by definition."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # ``from . import X`` / ``from ..foo import Y`` — same
                # package, can't cross subjects unless someone climbs
                # back up to ``hpc_agent.*``, which would be a more
                # serious lint failure caught elsewhere.
                continue
            if node.module:
                out.append((node.lineno, node.module))
    return out


def lint_file(path: Path, own_role: str, own_subject: str) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per cross-subject import violation."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    findings: list[tuple[int, str]] = []
    for lineno, module in _iter_imports(tree):
        if _is_allowed_non_subject(module):
            continue
        # Check both roles — a file in ``ops/foo`` may not import from
        # ``meta/bar`` either (different role still counts as a
        # different subject for cross-import purposes).
        for role in SUBJECT_ROLES:
            other = _imported_subject(module, role)
            if other is None:
                continue
            if role == own_role and other == own_subject:
                # Same subject as ourselves — allowed.
                continue
            findings.append(
                (
                    lineno,
                    f"cross-subject import: {own_role}/{own_subject} "
                    f"imports {role}/{other} ({module})",
                )
            )
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(scan_root: Path) -> list[tuple[Path, str, str]]:
    """Yield ``(file, role, subject)`` for every ``.py`` file inside a
    subject directory under ``scan_root/<role>/<subject>/``."""
    targets: list[tuple[Path, str, str]] = []
    for role in SUBJECT_ROLES:
        role_root = scan_root / role
        if not role_root.exists():
            continue
        for subject_dir in sorted(p for p in role_root.iterdir() if p.is_dir()):
            subject = subject_dir.name
            for py in sorted(subject_dir.rglob("*.py")):
                if not py.is_file():
                    continue
                targets.append((py, role, subject))
    return targets


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path, role, subject in iter_targets(root):
        for lineno, hint in lint_file(path, role, subject):
            try:
                rel: str = str(path.resolve().relative_to(REPO))
            except ValueError:
                rel = str(path)
            print(f"{rel}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} cross-subject import(s). "
            f"Route through hpc_agent.infra.* or hpc_agent.state.* "
            f"instead of reaching into another subject.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
