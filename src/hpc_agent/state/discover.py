"""Experiment-executor discovery.

Shared helpers used by ``/submit-hpc`` Step 1 (scanning the repo for
runnable executors) and the ``hpc-agent build-executor`` CLI
subcommand. The contract is intentionally minimal — an **executor**
is a ``.py`` file matching either of two patterns:

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

from hpc_agent._internal.primitive import primitive

__all__ = [
    "ExecutorInfo",
    "ReducerInfo",
    "discover_executors",
    "discover_reducers",
    "is_executor_source",
]

import ast
from dataclasses import dataclass, field
from pathlib import Path

# Module names that signal a CLI framework. Matched against any ``import X``
# or ``from X import ...`` statement at the top level of the script.
_CLI_FRAMEWORKS = frozenset({"argparse", "click", "typer", "fire"})

# Directory names we scan when the caller does not pass an explicit path.
# Callers that want a tighter scan (e.g. an integrator that knows
# ``src/`` is modules-only) pass ``search_dirs=("scripts",)``
# explicitly. hpc-agent deliberately does not auto-detect
# directory-layout conventions — that's caller knowledge.
_DEFAULT_CANDIDATE_DIRS = ("executors", "scripts", "src")


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
    cli="hpc-agent discover --experiment-dir <path> [--search-dirs <a,b,c>]",
    agent_facing=True,
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


# ─── Reducer discovery ────────────────────────────────────────────────────
#
# A reducer is a per-experiment Python file that takes a result_dir (or a
# directory of metrics.json blobs) and produces an aggregated metric — the
# user-side counterpart to the framework's :mod:`hpc_agent.mapreduce.reduce`
# helpers. We detect them via two signals:
#
#   1. **Filename stem** matches one of :data:`_REDUCER_NAME_HINTS` (e.g.
#      ``aggregate.py``, ``qlike.py``, ``score.py``). Stem-as-substring
#      catches ``aggregate_qlike.py`` / ``compute_qlike.py`` too.
#   2. **Top-level callable** named one of :data:`_REDUCER_FUNCTION_NAMES`
#      (e.g. ``def aggregate(...)``, ``def reduce(...)``). One non-trivial
#      parameter is enough — same heuristic ``compute(args)`` uses.
#
# Either signal alone qualifies. The detection is intentionally generous
# because the agent's failure mode (writing a fresh reducer when one
# already exists) is more costly than the occasional false positive. A
# false positive is just an extra file the agent surfaces to the user —
# easy to dismiss; the user knows their own repo.

# Filename stems / substrings that suggest a reducer. Lowercased; matched
# as substring against the file's stem.
_REDUCER_NAME_HINTS: frozenset[str] = frozenset(
    {
        "aggregate",
        "aggregator",
        "reduce",
        "reducer",
        "evaluate",
        "evaluation",
        "score",
        "scoring",
        "metric",
        "metrics",
        # Common loss-function names the agent's QLIKE complaint motivated.
        "qlike",
        "rmse",
        "mae",
        "mse",
        "mape",
        "smape",
        "loss",
        "summarise",
        "summarize",
    }
)

# Top-level function names that signal a reducer entry point.
_REDUCER_FUNCTION_NAMES: frozenset[str] = frozenset(
    {"aggregate", "reduce", "score", "evaluate", "summarize", "summarise"}
)

# Default search dirs. Mirrors :data:`_DEFAULT_CANDIDATE_DIRS` plus dedicated
# reducer/aggregator directories — common in repos that separate reducers
# from per-task executors.
_DEFAULT_REDUCER_DIRS = (
    "aggregators",
    "reducers",
    "scoring",
    "scripts",
    "src",
)


@dataclass(frozen=True)
class ReducerInfo:
    """Metadata about a discovered reducer file.

    Attributes
    ----------
    path:
        Absolute path to the ``.py`` file.
    name:
        File stem (no extension).
    matches:
        Tuple of signals that matched (e.g. ``("name", "function:aggregate")``).
        Useful for the agent's ranking + the slash command's "why this
        candidate" explanation.
    docstring:
        First line of the module docstring, if any.
    """

    path: Path
    name: str
    matches: tuple[str, ...]
    docstring: str | None = None


def _classify_reducer(source: str, *, path: Path) -> ReducerInfo | None:
    """Return :class:`ReducerInfo` if *source* looks like a reducer, else None.

    Signals are OR'd: one matching name hint OR one matching top-level
    function is enough. Multiple matches are recorded in ``matches`` so the
    caller can rank.
    """
    matches: list[str] = []

    stem = path.stem.lower()
    for hint in _REDUCER_NAME_HINTS:
        if hint in stem:
            matches.append(f"name:{hint}")
            break  # one stem hint is enough

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _REDUCER_FUNCTION_NAMES
            and node.args.args  # at least one parameter — same heuristic as compute()
        ):
            matches.append(f"function:{node.name}")

    if not matches:
        return None

    docstring = ast.get_docstring(tree)
    first_line = docstring.splitlines()[0].strip() if docstring else None
    return ReducerInfo(
        path=path,
        name=path.stem,
        matches=tuple(matches),
        docstring=first_line,
    )


@primitive(
    name="discover-reducers",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent discover-reducers --experiment-dir <path>",
)
def discover_reducers(
    root: Path | str,
    *,
    search_dirs: tuple[str, ...] | None = None,
    recursive: bool = True,
) -> list[ReducerInfo]:
    """Scan *root* for likely reducer / aggregator ``.py`` files.

    The motivating failure mode: the agent at /aggregate-hpc time writes
    a fresh QLIKE / RMSE / etc. aggregator instead of finding the one the
    user (or a prior agent run) already committed to the repo. This
    primitive surfaces every candidate so the agent can ask "use
    aggregators/qlike.py or write a new one?" instead of defaulting to
    write-new.

    Parameters
    ----------
    root:
        Experiment-repo root (typically the user's CWD).
    search_dirs:
        Subdirectory names under *root* to scan. ``None`` (default) tries
        :data:`_DEFAULT_REDUCER_DIRS` and falls back to *root* itself if
        none exist.
    recursive:
        Walk each search dir recursively. Default ``True`` (reducers
        often live a level deep, e.g. ``src/eval/qlike.py``); contrast
        with :func:`discover_executors` which defaults to non-recursive
        because executors live at predictable top-level paths.

    Returns
    -------
    A list of :class:`ReducerInfo`, sorted with multi-signal matches
    first (i.e. the file with both a name hint AND a function entry
    point ranks above one with only a name hint), then alphabetically.
    Files without any reducer signal are excluded.
    """
    root = Path(root).resolve()
    if not root.is_dir():
        return []

    if search_dirs is None:
        dirs = [root / d for d in _DEFAULT_REDUCER_DIRS if (root / d).is_dir()]
        if not dirs:
            dirs = [root]
    else:
        dirs = [root / d for d in search_dirs if (root / d).is_dir()]

    found: list[ReducerInfo] = []
    seen: set[Path] = set()
    for d in dirs:
        iterator = d.rglob("*.py") if recursive else d.glob("*.py")
        for py in iterator:
            if py.name in _SKIP_BASENAMES:
                continue
            if any(part in _SKIP_DIRS for part in py.parts):
                continue
            resolved = py.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                info = _classify_reducer(py.read_text(encoding="utf-8"), path=resolved)
            except OSError:
                continue
            if info is not None:
                found.append(info)

    return sorted(found, key=lambda i: (-len(i.matches), i.name))
