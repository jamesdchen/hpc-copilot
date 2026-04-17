"""Experiment-executor discovery.

Shared helpers used by both ``/submit`` (when scanning a repo for runnable
scripts) and ``/build-executor`` (when deciding which existing executor to
clone). The contract is intentionally minimal:

- An **executor** is a ``.py`` file that has both an ``argparse``-style CLI
  (it imports ``argparse`` or uses ``click``/``typer``) and an
  ``if __name__ == "__main__":`` entry point.
- A **shared utility** is a ``.py`` file with neither.

No registry, no ABC, no plugin hooks — just a filesystem scan plus a light
source parse. Any Python CLI script works.
"""

from __future__ import annotations

__all__ = [
    "ExecutorInfo",
    "discover_executors",
    "is_executor_source",
]

import ast
from dataclasses import dataclass, field
from pathlib import Path

# Module names that signal a CLI framework. Matched against any ``import X``
# or ``from X import ...`` statement at the top level of the script.
_CLI_FRAMEWORKS = frozenset({"argparse", "click", "typer", "fire"})

# Directory names we scan when the caller does not pass an explicit path.
_DEFAULT_CANDIDATE_DIRS = ("executors", "scripts", "src")

# Files we always skip — framework plumbing, caches, tests.
_SKIP_BASENAMES = frozenset(
    {
        "__init__.py",
        "_hpc_dispatch.py",
        "_hpc_combiner.py",
        "hpc_chunking_shim.py",
    }
)


@dataclass(frozen=True)
class ExecutorInfo:
    """Metadata extracted statically from a candidate executor file.

    Attributes
    ----------
    path:
        Absolute path to the ``.py`` file.
    name:
        Stem of the filename (e.g. ``ml_ridge`` for ``ml_ridge.py``).
    has_main_guard:
        Whether the module has an ``if __name__ == "__main__":`` block.
    cli_framework:
        Name of the detected CLI module (``"argparse"``, ``"click"``,
        ``"typer"``, ``"fire"``) or ``None`` if none was found.
    imports:
        Deduplicated top-level imports, used by ``/submit`` to classify the
        executor as CPU/GPU/DL.
    docstring:
        The module docstring's first line, if any — handy for summaries.
    """

    path: Path
    name: str
    has_main_guard: bool
    cli_framework: str | None
    imports: tuple[str, ...] = field(default_factory=tuple)
    docstring: str | None = None

    @property
    def is_executor(self) -> bool:
        """An executor has BOTH a main guard and a recognized CLI framework."""
        return self.has_main_guard and self.cli_framework is not None


def is_executor_source(source: str) -> bool:
    """Quick check on raw Python source text.

    Returns ``True`` iff the source parses and has (a) a top-level
    ``if __name__ == "__main__"`` guard and (b) an import of a recognized
    CLI framework.
    """
    info = _parse_source(source)
    return info.has_main_guard and info.cli_framework is not None


def discover_executors(
    root: Path | str,
    *,
    search_dirs: tuple[str, ...] | None = None,
    recursive: bool = False,
) -> list[ExecutorInfo]:
    """Scan *root* for executor ``.py`` files.

    Parameters
    ----------
    root:
        Experiment-repo root (typically the user's CWD when they invoke
        ``/submit`` or ``/build-executor``).
    search_dirs:
        Names of subdirectories under *root* to scan. If ``None`` (the
        default), try each of :data:`_DEFAULT_CANDIDATE_DIRS` in turn and
        collect from every one that exists. If every candidate is missing,
        fall back to scanning *root* itself.
    recursive:
        If ``True``, walk each search dir recursively; otherwise only the
        top level.

    Returns
    -------
    A list of :class:`ExecutorInfo`, sorted by ``name``. Non-executor files
    (utilities, ``__init__.py``, and files we can't parse) are excluded so
    callers can present the list directly to the user.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        return []

    if search_dirs is None:
        dirs = [root / d for d in _DEFAULT_CANDIDATE_DIRS if (root / d).is_dir()]
        if not dirs:
            dirs = [root]
    else:
        dirs = [root / d for d in search_dirs if (root / d).is_dir()]

    found: list[ExecutorInfo] = []
    seen: set[Path] = set()
    for d in dirs:
        iterator = d.rglob("*.py") if recursive else d.glob("*.py")
        for py in iterator:
            if py.name in _SKIP_BASENAMES:
                continue
            resolved = py.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                info = _parse_source(py.read_text(encoding="utf-8"), path=resolved)
            except (OSError, SyntaxError):
                continue
            if info.is_executor:
                found.append(info)

    return sorted(found, key=lambda i: i.name)


# ─── Internals ────────────────────────────────────────────────────────────


def _parse_source(source: str, *, path: Path | None = None) -> ExecutorInfo:
    """Parse *source* and extract executor metadata. Never raises on bad AST.

    When called via :func:`is_executor_source`, *path* is unused but still
    required to build a meaningful :class:`ExecutorInfo`. A synthetic placeholder
    is substituted in that case.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ExecutorInfo(
            path=path or Path("<unknown>"),
            name=(path.stem if path else "<unknown>"),
            has_main_guard=False,
            cli_framework=None,
        )

    imports: list[str] = []
    cli_framework: str | None = None
    has_main_guard = False

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                imports.append(top)
                if cli_framework is None and top in _CLI_FRAMEWORKS:
                    cli_framework = top
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top = node.module.split(".")[0]
            imports.append(top)
            if cli_framework is None and top in _CLI_FRAMEWORKS:
                cli_framework = top
        elif isinstance(node, ast.If) and _is_main_guard(node.test):
            has_main_guard = True

    docstring = ast.get_docstring(tree)
    first_line = docstring.splitlines()[0].strip() if docstring else None

    return ExecutorInfo(
        path=path or Path("<unknown>"),
        name=(path.stem if path else "<unknown>"),
        has_main_guard=has_main_guard,
        cli_framework=cli_framework,
        imports=tuple(dict.fromkeys(imports)),  # dedup, preserve order
        docstring=first_line,
    )


def _is_main_guard(test: ast.expr) -> bool:
    """Return ``True`` for ``__name__ == "__main__"`` (in either order)."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        _is_name_dunder(left)
        and _is_main_str(right)
        or (_is_name_dunder(right) and _is_main_str(left))
    )


def _is_name_dunder(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__name__"


def _is_main_str(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "__main__"
