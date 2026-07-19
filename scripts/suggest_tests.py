"""ADVISORY diff -> test-selection for agent inner loops.

Maps a git diff to the pytest modules most likely to cover it, so an agent's
inner edit->test loop can run a *fast, focused* slice instead of the full suite
between edits. This tool is **advisory only** (MAINTAINER-RULED 2026-07-16): the
full suite stays mandatory at CI and in the `/release` skill. Every run prints
an advisory disclaimer, and any changed source file that no rule could map is
listed **loudly** (never silently dropped — the no-silent-caps rule).

Three mapping passes, unioned:

1. **Mirror-path convention.** ``src/hpc_agent/X/Y.py`` -> ``tests/X/test_Y*.py``
   (the ``src/hpc_agent/`` prefix drops; the leaf becomes ``test_<leaf>*.py``).
   Only matches that exist on disk are kept.

2. **Import-graph pass** (AST, deterministic, no imports executed). Every test
   module's imports are parsed once; a changed src module is a hit if a test
   imports it **directly**, or imports a src module that itself imports the
   changed module (**one hop**, via a reverse-dependency map built over ``src``).
   This is what catches the common case where the mirror path lies — e.g.
   ``ops/decision/journal/verify_relay.py`` is exercised by
   ``tests/ops/test_verify_relay.py``, not ``tests/ops/decision/journal/...``.

3. **Hand-curated cross-consumer map** (:data:`CROSS_CONSUMER`) for shared
   predicates whose blast radius no path or import edge reveals — a block-drive
   lifecycle change fans out to every workflow's ``test_blocks.py`` and the
   attention projections even though those tests never import the kernel module.
   Seeded from tonight's lesson (the block_drive fan-out) plus the infra/io/
   verify_relay consumers.

Usage::

    python scripts/suggest_tests.py [<ref>]                 # PRINT the advisory slice
    python scripts/suggest_tests.py --run [--base <ref>]    # RUN pytest on the slice

``<ref>`` (positional) / ``--base`` both default to ``HEAD`` (working tree +
staged vs the last commit); ``--base`` wins when both are given. Pass a ref
(``main``, a sha, ``origin/main``) to scope a branch's worth of change.

``--run`` executes ``pytest`` on **exactly** the suggested slice and exits with
pytest's own return code. An *empty* suggestion set never silently passes: it
prints a loud "no targeted tests suggested — run the full battery" line and
exits ``0`` (so an agent's loop is unblocked but explicitly told the slice was
empty). ``--run`` is still ADVISORY — the full suite remains the CI / ``/release``
gate; a green slice is a fast signal, never a merge warrant.

The fire path is exercised in ``tests/scripts/test_suggest_tests.py``.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO / "src"
TESTS_ROOT = REPO / "tests"
PKG = "hpc_agent"

ADVISORY_LINE = "advisory selection — CI runs everything; the full suite stays mandatory"

# ---------------------------------------------------------------------------
# Hand-curated cross-consumer map.
#
# Keys are ``src/hpc_agent``-relative POSIX paths of shared predicates whose
# consumers no path/import edge reveals. Values are repo-relative test targets
# (a directory OR a file) that must run when the key changes. Add an entry only
# as a reviewed decision, with a comment citing the shared predicate.
#
# Seed rationale:
#  * block_drive is the lifecycle loop driver; a boundary/marker change there
#    broke every workflow's block wiring before (94c0c484 "None-marker boundary"
#    CI-red). The workflows' ``test_blocks.py`` + the attention/status
#    projections consume the driver's output without importing the module.
#  * infra/clusters.py resolves cluster profiles consumed all across submit.
#  * infra/io.py is the atomic-durable-write seam under state + decision writes.
#  * verify_relay's audit is consumed by the kernel hooks that gate relays.
# ---------------------------------------------------------------------------
CROSS_CONSUMER: dict[str, list[str]] = {
    "_kernel/lifecycle/block_drive.py": [
        "tests/ops/attention/",
        "tests/ops/status/test_snapshot_attention.py",
        "tests/ops/monitor/test_blocks.py",
        "tests/ops/aggregate/test_blocks.py",
        "tests/meta/campaign/test_blocks.py",
        "tests/ops/test_block_gate_and_speculate.py",
        "tests/ops/test_block_chain.py",
    ],
    "infra/clusters.py": [
        "tests/infra/",
        "tests/ops/submit/",
    ],
    "infra/io.py": [
        "tests/state/",
        "tests/ops/decision/",
    ],
    "ops/decision/journal/verify_relay.py": [
        "tests/ops/test_verify_relay.py",
        "tests/_kernel/hooks/",
    ],
}


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------
def changed_files(ref: str = "HEAD") -> list[Path]:
    """Return repo-relative paths changed vs *ref* (working tree + staged).

    Uses ``git diff --name-only <ref>`` — the tree state (working + staged)
    against the named commit, so an agent's uncommitted edits are seen with the
    default ``HEAD``.
    """
    out = subprocess.run(
        ["git", "diff", "--name-only", ref],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return [Path(line) for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# module <-> path helpers
# ---------------------------------------------------------------------------
def _module_of(src_rel: Path) -> str | None:
    """Dotted module name for a ``src/hpc_agent/...`` path, or ``None``.

    ``src/hpc_agent/ops/decision/journal/verify_relay.py`` ->
    ``hpc_agent.ops.decision.journal.verify_relay``. An ``__init__.py`` collapses
    to its package. Non-``.py`` or non-package files return ``None``.
    """
    parts = src_rel.parts
    if len(parts) < 2 or parts[0] != "src" or parts[1] != PKG:
        return None
    if src_rel.suffix != ".py":
        return None
    tail = list(parts[1:])  # drop the leading "src"
    if tail[-1] == "__init__.py":
        tail = tail[:-1]
    else:
        tail[-1] = tail[-1][: -len(".py")]
    return ".".join(tail)


def _imported_modules(tree: ast.AST, self_module: str) -> set[str]:
    """Collect every ``hpc_agent.*`` module a parsed source file imports.

    Handles ``import a.b.c``, ``from a.b import c`` (records both the package and
    ``a.b.c`` so ``from pkg import submodule`` is caught), and relative
    ``from . import x`` / ``from ..y import z`` resolved against *self_module*.
    """
    found: set[str] = set()
    self_pkg_parts = self_module.split(".")[:-1]  # package of the importing file
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PKG):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and self_pkg_parts:
                base_parts = self_pkg_parts[: len(self_pkg_parts) - (node.level - 1)]
                base = ".".join(base_parts)
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if not mod.startswith(PKG):
                continue
            found.add(mod)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{mod}.{alias.name}")
    return found


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _iter_py(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


# ---------------------------------------------------------------------------
# the three passes
# ---------------------------------------------------------------------------
def mirror_targets(src_rel: Path) -> list[Path]:
    """Mirror-path matches (``tests/X/test_Y*.py``) that exist on disk."""
    parts = src_rel.parts
    if len(parts) < 3 or parts[0] != "src" or parts[1] != PKG or src_rel.suffix != ".py":
        return []
    if parts[-1] == "__init__.py":
        return []
    sub = parts[2:-1]  # dirs under hpc_agent
    leaf = parts[-1][: -len(".py")]
    test_dir = TESTS_ROOT.joinpath(*sub)
    if not test_dir.is_dir():
        return []
    return sorted(test_dir.glob(f"test_{leaf}*.py"))


def _reverse_src_deps() -> dict[str, set[str]]:
    """Map each src module -> the set of src modules that import it (one hop)."""
    rev: dict[str, set[str]] = {}
    for path in _iter_py(SRC_ROOT / PKG):
        importer = _module_of(path.relative_to(REPO))
        if importer is None:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        for imported in _imported_modules(tree, importer):
            rev.setdefault(imported, set()).add(importer)
    return rev


def _module_matches(imports: set[str], target: str) -> bool:
    """True if *imports* references *target* module (exact or as a package)."""
    if target in imports:
        return True
    prefix = target + "."
    return any(i == target or i.startswith(prefix) for i in imports)


def _reach_modules(modules: set[str], reverse_deps: dict[str, set[str]]) -> set[str]:
    """The *modules* plus every src module one hop away (a direct importer).

    Folds package-prefix reverse edges too, so a changed ``__init__``/package
    reaches importers of any of its submodules.
    """
    reach: set[str] = set(modules)
    for mod in modules:
        reach |= reverse_deps.get(mod, set())
        for imported, importers in reverse_deps.items():
            if imported == mod or imported.startswith(mod + "."):
                reach |= importers
    return reach


def import_graph_targets(
    changed_modules: set[str],
    test_imports: dict[Path, set[str]],
    reverse_deps: dict[str, set[str]],
) -> set[Path]:
    """Test modules importing a changed module directly or one hop away."""
    reach = _reach_modules(changed_modules, reverse_deps)
    return {
        test_path
        for test_path, imports in test_imports.items()
        if any(_module_matches(imports, m) for m in reach)
    }


def cross_consumer_targets(src_rel_posix: str) -> list[Path]:
    """Resolve a curated cross-consumer entry to existing test paths."""
    out: list[Path] = []
    for rel in CROSS_CONSUMER.get(src_rel_posix, []):
        p = REPO / rel
        if p.exists():
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# top-level
# ---------------------------------------------------------------------------
class Selection:
    """Result of a suggest pass: selected pytest targets + unmapped files."""

    REASONS = ("mirror", "import-graph", "cross-consumer")

    def __init__(self) -> None:
        self.targets: set[Path] = set()
        self.unmapped: list[Path] = []
        self.non_src: list[Path] = []
        self.reasons: dict[str, set[str]] = {r: set() for r in self.REASONS}

    def _rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(REPO).as_posix()
        except ValueError:
            return p.as_posix()

    def add(self, paths, reason: str) -> bool:
        """Record *paths* under *reason*; return True if any were added."""
        any_added = False
        for p in paths:
            self.targets.add(p)
            self.reasons[reason].add(self._rel(p))
            any_added = True
        return any_added

    def pytest_args(self) -> list[str]:
        return sorted(self._rel(p) for p in self.targets)


def suggest(ref: str = "HEAD") -> Selection:
    """Build a :class:`Selection` for the diff of the tree vs *ref*."""
    sel = Selection()
    files = changed_files(ref)

    # Parse tests once (import-graph pass shared across all changed files).
    test_imports: dict[Path, set[str]] = {}
    for tp in _iter_py(TESTS_ROOT):
        mod = ".".join(("tests", *tp.relative_to(TESTS_ROOT).with_suffix("").parts))
        tree = _parse(tp)
        if tree is not None:
            test_imports[tp] = _imported_modules(tree, mod)
    reverse_deps = _reverse_src_deps()

    for f in files:
        posix = f.as_posix()
        if not (posix.startswith(f"src/{PKG}/") and f.suffix == ".py"):
            sel.non_src.append(f)
            continue

        mapped = sel.add(mirror_targets(f), "mirror")
        cc_key = posix[len(f"src/{PKG}/") :]
        mapped = sel.add(cross_consumer_targets(cc_key), "cross-consumer") or mapped

        module = _module_of(f)
        if module is not None:
            reach = _reach_modules({module}, reverse_deps)
            ig = {
                tp
                for tp, imports in test_imports.items()
                if any(_module_matches(imports, m) for m in reach)
            }
            mapped = sel.add(ig, "import-graph") or mapped

        # No pass produced a target for this changed source file -> loud.
        if not mapped:
            sel.unmapped.append(f)
    return sel


def render(sel: Selection, ref: str) -> str:
    """Human/agent-facing report string."""
    lines: list[str] = []
    lines.append(f"# suggest-tests ({ADVISORY_LINE})")
    lines.append(f"# diff base: {ref}")
    lines.append("")
    args = sel.pytest_args()
    if args:
        lines.append(f"# {len(args)} advisory target(s):")
        for a in args:
            why = sorted(r for r, s in sel.reasons.items() if a in s)
            lines.append(f"#   {a}  [{', '.join(why)}]")
        lines.append("")
        lines.append("pytest " + " ".join(args))
    else:
        lines.append("# no test targets mapped from this diff.")
    lines.append("")

    if sel.unmapped:
        lines.append(
            f"# !! {len(sel.unmapped)} changed source file(s) mapped to NO test "
            f"(run more, not less):"
        )
        for f in sorted(set(sel.unmapped), key=lambda p: p.as_posix()):
            lines.append(f"#   UNMAPPED  {f.as_posix()}")
    if sel.non_src:
        others = sorted(set(sel.non_src), key=lambda p: p.as_posix())
        shown = ", ".join(p.as_posix() for p in others[:8]) + (" ..." if len(others) > 8 else "")
        lines.append(
            f"# note: {len(sel.non_src)} changed non-src file(s) not considered "
            f"(docs/tests/config): {shown}"
        )
    lines.append("")
    lines.append(f"# {ADVISORY_LINE}. Before merge / release, run the FULL suite.")
    return "\n".join(lines)


NO_TARGETS_LINE = (
    "!! no targeted tests suggested from this diff — run the full battery "
    "(pytest) before you trust this change"
)


def run_suggested(sel: Selection, ref: str) -> int:
    """Execute pytest on EXACTLY the suggested slice; return pytest's exit code.

    An empty suggestion set is never a silent pass: it prints
    :data:`NO_TARGETS_LINE` loudly and returns ``0`` (the diff mapped to nothing
    runnable, so the caller's loop is unblocked but explicitly told to fall back
    to the full battery). This is still ADVISORY — the full suite is the only
    merge / release evidence.
    """
    args = sel.pytest_args()
    if not args:
        print(f"\n# {NO_TARGETS_LINE}.")
        return 0
    cmd = [sys.executable, "-m", "pytest", *args]
    print("\n# running advisory slice: pytest " + " ".join(args))
    sys.stdout.flush()  # order our line before pytest's inherited output when piped
    return subprocess.run(cmd, cwd=REPO).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ref",
        nargs="?",
        default="HEAD",
        help="git ref to diff the working tree against (default: HEAD).",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        default=None,
        help="git ref to diff against (overrides the positional ref); pairs with --run.",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "execute pytest on exactly the suggested slice and exit with pytest's "
            "code; an empty slice prints a loud 'run the full battery' line and exits 0. "
            "ADVISORY: the full suite still gates CI and /release."
        ),
    )
    ns = parser.parse_args(argv)
    ref = ns.base if ns.base is not None else ns.ref
    try:
        sel = suggest(ref)
    except subprocess.CalledProcessError as exc:
        print(f"suggest-tests: git diff failed for ref {ref!r}: {exc.stderr}", file=sys.stderr)
        return 2
    print(render(sel, ref))
    if ns.run:
        return run_suggested(sel, ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
