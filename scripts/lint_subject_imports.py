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
where ``<other_subject>`` differs from the file's own subject. The
evasive spellings are covered too: relative imports are resolved against
the importing file's package (``from ...meta.registry import x`` climbs
parents and crosses subjects like its absolute spelling), and
``from hpc_agent.<role> import <subject>`` binds the subject through an
alias without its dotted path ever appearing in the ``from`` clause.
EVERY candidate is checked against the real subject directories: only a
directory under a role root is a subject, so re-exported functions and
role-root MODULE files (shared op-level surface like
``ops/evidence_embed.py`` — design-pinned homes importable from any
subject) don't false-positive.

Allowed cross-cutting roots (these aren't subjects, they're substrate):

* ``hpc_agent.infra.*``
* ``hpc_agent.state.*``

The script handles absent role roots gracefully (post-reorg both
``ops/`` and ``meta/`` exist; the absent-role branch survives so the
script stays useful if a future role root is added late). Every
per-file import violation surfaces a ``path:lineno: cross-subject
import: ...`` line and the script exits 1.
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


def _iter_imports(tree: ast.AST, module_package: str) -> list[tuple[int, str]]:
    """Yield ``(lineno, module_name)`` for every module an import
    statement in ``tree`` could bind.

    Relative imports are resolved against *module_package* — a
    ``from ...meta.registry import x`` climbs parents and crosses subjects
    exactly like its absolute spelling, so it must not be skipped. For
    ``from pkg import name``, ``pkg.name`` is additionally yielded per
    alias: when ``name`` is a subject package, that form binds it just
    like ``import pkg.name``. The caller checks every candidate against
    the real subject directories, so an alias that is a re-exported
    function never false-positives.
    """
    out: list[tuple[int, str]] = []
    pkg_parts = module_package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module: str | None
            if node.level:
                if node.level > len(pkg_parts):
                    continue  # climbs above the distribution root — broken import
                base = ".".join(pkg_parts[: len(pkg_parts) - (node.level - 1)])
                module = f"{base}.{node.module}" if node.module else base
            else:
                module = node.module
            if not module:
                continue
            out.append((node.lineno, module))
            out.extend(
                (node.lineno, f"{module}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            )
    return out


def lint_file(
    path: Path,
    own_role: str,
    own_subject: str,
    module_package: str,
    subjects_by_role: dict[str, set[str]],
) -> list[tuple[int, str]]:
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
    # ``from pkg.sub import name`` yields both ``pkg.sub`` and
    # ``pkg.sub.name`` candidates for one line — report each crossing once.
    seen: set[tuple[int, str, str]] = set()
    for lineno, module in _iter_imports(tree, module_package):
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
            if other not in subjects_by_role.get(role, set()):
                # Not a subject DIRECTORY: a role-root module file
                # (``from hpc_agent.ops.evidence_embed import ...``) or a
                # re-exported helper bound via
                # ``from hpc_agent.<role> import <name>`` — subjects are
                # directories, so neither is a subject crossing.
                continue
            if (lineno, role, other) in seen:
                continue
            seen.add((lineno, role, other))
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


def _subjects_by_role(scan_root: Path) -> dict[str, set[str]]:
    """The real subject directories per role — the reference set for
    deciding whether an alias-derived import names a subject."""
    return {
        role: {p.name for p in (scan_root / role).iterdir() if p.is_dir()}
        for role in SUBJECT_ROLES
        if (scan_root / role).exists()
    }


def _module_package(path: Path, scan_root: Path) -> str:
    """Dotted package containing the module at *path* — anchors
    relative-import resolution (the scan root corresponds to ``hpc_agent``)."""
    rel = path.resolve().relative_to(scan_root.resolve())
    return ".".join(["hpc_agent", *rel.parts[:-1]])


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    subjects = _subjects_by_role(root)
    failures = 0
    for path, role, subject in iter_targets(root):
        package = _module_package(path, root)
        for lineno, hint in lint_file(path, role, subject, package, subjects):
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
