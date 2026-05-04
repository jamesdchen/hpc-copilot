"""Experiment-executor discovery.

Shared helpers used by ``/submit-hpc`` Step 1 (scanning the repo for
runnable executors) and the ``hpc-mapreduce build-executor`` CLI
subcommand (used by MARs orchestrators that scaffold experiments
programmatically). The contract is intentionally minimal — an
**executor** is a ``.py`` file matching either of two patterns:

- **New contract (preferred):** exports ``compute(args) -> None``. CLI
  dispatch lives in the auto-generated ``.hpc/cli.py``; the executor is
  pure compute. No ``__main__`` block needed.
- **Old contract (transitional):** has both an ``argparse``-style CLI
  (``argparse`` / ``click`` / ``typer`` / ``fire``) AND an
  ``if __name__ == "__main__":`` block. Self-dispatching script.

A **shared utility** is a ``.py`` file matching neither.

No registry, no ABC, no plugin hooks — just a filesystem scan plus a light
source parse. Any Python file with the right shape qualifies.
"""

from __future__ import annotations

from claude_hpc._internal._primitive import primitive

__all__ = [
    "ExecutorInfo",
    "detect_mars_tier",
    "discover_executors",
    "is_executor_source",
    "read_meta_json",
]

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

# Module names that signal a CLI framework. Matched against any ``import X``
# or ``from X import ...`` statement at the top level of the script.
_CLI_FRAMEWORKS = frozenset({"argparse", "click", "typer", "fire"})

# Directory names we scan when the caller does not pass an explicit path.
# Used when no MARs ``meta.json`` marker is present at *root*.
_DEFAULT_CANDIDATE_DIRS = ("executors", "scripts", "src")

# When *root* looks like a MARs experiment (``meta.json`` present), MARs's
# layout contract is ``scripts/`` = entrypoints, ``src/`` = modules. We must
# not mis-detect modules under ``src/`` as executors.
_MARS_CANDIDATE_DIRS = ("scripts",)


def _default_candidate_dirs(root: Path) -> tuple[str, ...]:
    """Return the default search-dir tuple for *root*.

    Detects MARs Tier-2 / Tier-1 experiments by the presence of a
    ``meta.json`` file at the experiment-dir root and narrows the scan to
    ``scripts/`` (Tier-2 entrypoints) — Tier-1 ``probe.py`` lives at the
    root and is picked up by the existing root-level fallback path.
    """
    if (root / "meta.json").is_file():
        return _MARS_CANDIDATE_DIRS
    return _DEFAULT_CANDIDATE_DIRS


# Files we always skip — only ``__init__.py`` is interesting now that all
# framework artifacts live under ``.hpc/`` (which is excluded wholesale by
# the directory-name check in ``discover_executors``).
_SKIP_BASENAMES = frozenset({"__init__.py"})

# Directory names whose contents are never user code — framework
# scaffolding, caches, build artifacts.
_SKIP_DIRS = frozenset({".hpc", ".git", "__pycache__", ".mypy_cache"})


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
        Only relevant for the old self-dispatching contract.
    cli_framework:
        Name of the detected CLI module (``"argparse"``, ``"click"``,
        ``"typer"``, ``"fire"``) or ``None`` if none was found.
        Only relevant for the old self-dispatching contract.
    has_compute_function:
        Whether the module exports ``compute(args)`` at the top level
        (the new pure-compute contract; CLI lives in ``.hpc/cli.py``).
    imports:
        Deduplicated top-level imports, used by ``/submit-hpc`` to
        classify the executor as CPU/GPU/DL.
    docstring:
        The module docstring's first line, if any — handy for summaries.
    """

    path: Path
    name: str
    has_main_guard: bool
    cli_framework: str | None
    imports: tuple[str, ...] = field(default_factory=tuple)
    docstring: str | None = None
    has_compute_function: bool = False

    @property
    def is_executor(self) -> bool:
        """True under either the new or old executor contract.

        New (preferred): exports ``compute(args)`` — the dispatcher in
        ``.hpc/cli.py`` provides argv parsing and entry-point.

        Old (transitional): has ``__main__`` guard plus a recognized
        CLI framework — self-dispatching script.
        """
        if self.has_compute_function:
            return True
        return self.has_main_guard and self.cli_framework is not None


def read_meta_json(experiment_dir: Path | str) -> dict | None:
    """Return the parsed ``meta.json`` of *experiment_dir* if present and valid.

    MARs's ``meta.json`` is the authoritative experiment-context marker
    (``experiment_id``, ``seed``, ``purpose``, …). This helper lets every
    surface — CLI, slash command, future MARs adapters — read it through one
    seam.

    Returns ``None`` when the file is missing, unreadable, or not a JSON
    object. Never raises; claude-hpc is not the place to validate MARs's
    schema beyond extracting the fields it knows about.
    """
    p = Path(experiment_dir) / "meta.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def detect_mars_tier(experiment_dir: Path | str) -> int | None:
    """Infer the MARs tier of *experiment_dir* from path layout.

    MARs's directory contract:

    - Tier-1 probes live under ``probes/probe-*`` with ``probe.py`` at the
      root of the probe directory.
    - Tier-2 runs live under ``runs/run-*`` with ``scripts/`` for entrypoints.

    Returns the tier as ``1`` or ``2`` when both the path pattern and the
    expected marker file are present, otherwise ``None``. Pure path
    inspection — does not parse ``meta.json``.
    """
    p = Path(experiment_dir).resolve()
    name = p.name
    parent = p.parent.name
    if parent == "probes" and name.startswith("probe-") and (p / "probe.py").is_file():
        return 1
    if parent == "runs" and name.startswith("run-") and (p / "scripts").is_dir():
        return 2
    return None


def is_executor_source(source: str) -> bool:
    """Quick check on raw Python source text.

    Returns ``True`` iff the source parses and matches either the new
    contract (top-level ``compute(args)`` function) or the old contract
    (``__main__`` guard plus a recognized CLI framework import).
    """
    return _parse_source(source).is_executor


@primitive(
    name="discover-executors",
    verb="query",
    side_effects=[],
    idempotent=True,
)
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
        candidates = _default_candidate_dirs(root)
        dirs = [root / d for d in candidates if (root / d).is_dir()]
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
            # Skip framework subdirs entirely — both reserved (.hpc/) and
            # build/cache dirs that occasionally contain Python files.
            if any(part in _SKIP_DIRS for part in py.parts):
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
    has_compute_function = False

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
        elif (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "compute"
            # New contract requires compute to take parsed args as a
            # positional parameter; reject zero-arg ``def compute():``
            # (most likely a coincidence, not the executor entry point).
            and node.args.args
        ):
            has_compute_function = True

    docstring = ast.get_docstring(tree)
    first_line = docstring.splitlines()[0].strip() if docstring else None

    return ExecutorInfo(
        path=path or Path("<unknown>"),
        name=(path.stem if path else "<unknown>"),
        has_main_guard=has_main_guard,
        cli_framework=cli_framework,
        imports=tuple(dict.fromkeys(imports)),  # dedup, preserve order
        docstring=first_line,
        has_compute_function=has_compute_function,
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
