#!/usr/bin/env python3
"""Curated per-module mutation-testing runner (devx B4).

Mutation testing re-runs the test suite once *per mutant*, so a full-tree
sweep of this 8k-test codebase is absurd. This runner encapsulates a CURATED
MODULE MAP -- a handful of high-value, pure-logic modules, each paired with the
focused test file(s) that exercise it -- so a developer never hand-assembles
mutmut CLI args or hand-edits ``[tool.mutmut]`` in ``pyproject.toml``. One
module's scoped sweep stays small enough (its own tests, not the whole suite)
to finish inside a CI job step.

    python scripts/run_mutation.py --list                 # show the module map
    python scripts/run_mutation.py --module block-chain    # sweep one module
    python scripts/run_mutation.py --module block-chain --dry-run
                                                           # validate + print
                                                           #   the scoped config

**Windows is CI-only.** mutmut 3.x hard-``sys.exit(1)``s at import on
``platform.system() == "Windows"`` and imports the POSIX-only ``resource``
module, so it CANNOT run natively on this box (patching the guard still hits
``import resource``). Run the real sweep on Linux -- locally, or via the
``.github/workflows/mutation.yml`` ``workflow_dispatch`` matrix. On a
non-Linux host this script refuses to invoke mutmut and points you there;
``--dry-run`` still works everywhere (it only validates the map + renders the
scoped config, never launching mutmut). See docs/internals/mutation-testing.md.

This runner never edits ``pyproject.toml`` durably: it backs the file up to a
sidecar, writes the scoped ``[tool.mutmut]`` block, runs mutmut, and ALWAYS
restores the original in a ``finally`` (a stale sidecar from an interrupted run
is recovered on the next start). The committed ``[tool.mutmut]`` defaults -- and
the sibling ``scripts/mutmut_shortlist.py`` / scheduled cluster-verb sweep that
depend on them -- are therefore never perturbed.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib

# Repo root = parent of this scripts/ dir.
REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
# Sidecar backup so an interrupted run (Ctrl-C / kill) can be recovered on the
# next start rather than leaving a scoped pyproject.toml in the working tree.
_BACKUP = REPO_ROOT / "pyproject.toml.run_mutation.bak"


@dataclass(frozen=True)
class ModuleScope:
    """One curated mutation target: a source module + its focused test files."""

    key: str
    source: str  # repo-relative .py file to mutate
    tests: tuple[str, ...]  # repo-relative test file(s) that exercise it
    note: str


# ── the curated module map ────────────────────────────────────────────────────
#
# Selection criteria (docs/internals/mutation-testing.md): pure-logic modules
# where a surviving mutant is a real signal, each with a SMALL, focused test
# file so the scoped sweep stays inside a CI step. ``block-chain`` is the
# reference target -- zero lazy body-imports, so every function is
# mutmut-reachable (see the shortlist tool for why that matters).
MODULE_MAP: dict[str, ModuleScope] = {
    "block-chain": ModuleScope(
        key="block-chain",
        source="src/hpc_agent/infra/block_chain.py",
        tests=("tests/ops/test_block_chain.py",),
        note="Deterministic block-successor tables + spec composition. "
        "Pure, zero body-imports -- fully mutmut-reachable. The reference target.",
    ),
    "attestation": ModuleScope(
        key="attestation",
        source="src/hpc_agent/state/attestation.py",
        tests=("tests/state/test_attestation.py",),
        note="Attestation kernel (validate / bind / reduce). Pure logic.",
    ),
    "describe-cache": ModuleScope(
        key="describe-cache",
        source="src/hpc_agent/state/describe_cache.py",
        tests=("tests/cli/test_describe_cache.py",),
        note="Build-content-keyed describe cache -- guard-heavy (disable / "
        "safe-name / partial-registry). Some lazy imports blind mutmut (fewer mutants).",
    ),
    "fast-path-cache": ModuleScope(
        key="fast-path-cache",
        source="src/hpc_agent/cli/_fast_path_cache.py",
        tests=("tests/cli/test_fast_dispatch.py",),
        note="CLI single-verb fast-path resolution cache. Guard + fingerprint logic.",
    ),
    "capabilities-cache": ModuleScope(
        key="capabilities-cache",
        source="src/hpc_agent/state/capabilities_cache.py",
        tests=("tests/cli/test_capabilities_cache.py",),
        note="Build+dist-keyed capabilities-envelope cache -- guard-heavy (disable / "
        "dirty / dist-signature / partial-registry / per-variant). Byte-identity to "
        "the walk is the load-bearing invariant. Some lazy imports blind mutmut.",
    ),
    "combiner": ModuleScope(
        key="combiner",
        source="src/hpc_agent/execution/mapreduce/combiner.py",
        tests=(
            "tests/execution/mapreduce/test_combiner.py",
            "tests/execution/mapreduce/test_combiner_failures.py",
        ),
        note="Deterministic reduce/combine -- the module that computes every "
        "aggregate number. HEAVY (~650 lines): its scoped sweep is the slowest; "
        "budget the most CI time for this key.",
    ),
}


def _fmt_map() -> str:
    """Render the module map as an aligned, human-readable block."""
    width = max(len(k) for k in MODULE_MAP)
    lines = ["Curated mutation module map (--module <key>):", ""]
    for scope in MODULE_MAP.values():
        lines.append(f"  {scope.key.ljust(width)}  {scope.source}")
        for t in scope.tests:
            lines.append(f"  {' '.ljust(width)}    tests: {t}")
        lines.append(f"  {' '.ljust(width)}    {scope.note}")
        lines.append("")
    return "\n".join(lines)


def _validate_scope(scope: ModuleScope) -> list[str]:
    """Return a list of problems (missing source/test paths); empty when clean."""
    problems: list[str] = []
    src = REPO_ROOT / scope.source
    if not src.is_file():
        problems.append(f"source not found: {scope.source}")
    for t in scope.tests:
        if not (REPO_ROOT / t).exists():
            problems.append(f"test path not found: {t}")
    return problems


def _replace_named_array(text: str, key: str, values: list[str]) -> str:
    """Replace the value array of ``<key> = [ ... ]`` inside ``[tool.mutmut]``.

    A minimal line-based rewrite (no tomlkit dep) mirroring
    ``scripts/mutmut_shortlist.py._apply_to_pyproject``: it finds the ``key``
    assignment inside the ``[tool.mutmut]`` table and rewrites through the array's
    closing ``]``, preserving every sibling key. Raises if the key is absent so a
    silent no-scope can never slip through.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_mutmut = False
    replaced = False
    new_block = [f"{key} = ["] + [f'    "{v}",' for v in values] + ["]"]
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mutmut = stripped == "[tool.mutmut]"
        if in_mutmut and stripped.startswith(key):
            while i < len(lines) and "]" not in lines[i]:
                i += 1
            i += 1  # skip the closing-bracket line
            out.extend(new_block)
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        raise SystemExit(f"could not find [tool.mutmut].{key} in {PYPROJECT}")
    return "\n".join(out) + "\n"


def render_scoped_pyproject(scope: ModuleScope) -> str:
    """Return the pyproject.toml text scoped to *scope* (source + tests only).

    Rewrites ``[tool.mutmut].paths_to_mutate`` to the single source module and
    ``[tool.mutmut].tests_dir`` to the module's focused test file(s). mutmut 3.x
    treats both keys as deprecated aliases (``source_paths`` /
    ``pytest_add_cli_args_test_selection``) but honours them, and ``tests_dir``
    accepts individual test-file paths -- that is the per-module test-selection
    lever that keeps one sweep inside a CI step. Every other key (``also_copy``,
    ``do_not_mutate``, the xdist-override ``pytest_add_cli_args``) is preserved.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    text = _replace_named_array(text, "paths_to_mutate", [scope.source])
    text = _replace_named_array(text, "tests_dir", list(scope.tests))
    # Fail loudly if the rewrite produced non-parseable TOML.
    tomllib.loads(text)
    return text


def _recover_stale_backup() -> None:
    """Restore pyproject from a leftover sidecar (a prior interrupted run)."""
    if _BACKUP.exists():
        print(f"recovering pyproject.toml from stale backup {_BACKUP.name} (prior run interrupted)")
        PYPROJECT.write_text(_BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
        _BACKUP.unlink()


def run_sweep(scope: ModuleScope) -> int:
    """Scope pyproject, run mutmut, restore pyproject, print survivors.

    Assumes the caller already gated on platform (mutmut is unusable on Windows).
    """
    original = PYPROJECT.read_text(encoding="utf-8")
    scoped = render_scoped_pyproject(scope)
    _BACKUP.write_text(original, encoding="utf-8")
    try:
        PYPROJECT.write_text(scoped, encoding="utf-8")
        print(f"scoped [tool.mutmut] to {scope.source}")
        print(f"  tests: {', '.join(scope.tests)}\n")

        mutants = REPO_ROOT / "mutants"
        if mutants.exists():
            import shutil

            shutil.rmtree(mutants, ignore_errors=True)

        # mutmut exits non-zero when any mutant survives -- that is the SIGNAL,
        # not a runner failure, so a non-zero ``run`` is tolerated and the
        # results step below carries the outcome.
        run = subprocess.run(
            [sys.executable, "-m", "mutmut", "run"],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
        )
        print(
            f"\nmutmut run exit code: {run.returncode} (non-zero = survivors/skips, not a failure)"
        )

        results = subprocess.run(
            [sys.executable, "-m", "mutmut", "results"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print("\n===== mutmut results =====")
        print(results.stdout or "(no results output)")
        if results.stderr:
            print(results.stderr, file=sys.stderr)
    finally:
        PYPROJECT.write_text(original, encoding="utf-8")
        _BACKUP.unlink(missing_ok=True)
        print("restored original pyproject.toml")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Curated per-module mutation-testing runner (devx B4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--module",
        metavar="KEY",
        help="run a scoped mutation sweep on this module key (see --list).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the curated module map and exit.",
    )
    parser.add_argument(
        "--keys",
        action="store_true",
        help="print the module keys as a JSON array (drives the CI matrix) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the module + render the scoped [tool.mutmut] block "
        "WITHOUT running mutmut (works on every platform).",
    )
    args = parser.parse_args(argv)

    if args.keys:
        import json

        print(json.dumps(list(MODULE_MAP)))
        return 0

    if args.list or (not args.module and not args.dry_run):
        print(_fmt_map())
        if not args.list:
            print("Pass --module <key> to run a sweep, or --dry-run to validate.")
        return 0

    if not args.module:
        print("error: --dry-run requires --module <key>.", file=sys.stderr)
        print(_fmt_map(), file=sys.stderr)
        return 2

    scope = MODULE_MAP.get(args.module)
    if scope is None:
        print(f"error: unknown module key {args.module!r}.\n", file=sys.stderr)
        print(_fmt_map(), file=sys.stderr)
        return 2

    problems = _validate_scope(scope)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"module {scope.key!r} validated: source + {len(scope.tests)} test path(s) exist.\n")
        print("scoped [tool.mutmut] block that would be written:\n")
        scoped = render_scoped_pyproject(scope)
        # Echo just the [tool.mutmut] section for a readable proof.
        section: list[str] = []
        capturing = False
        for line in scoped.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if s == "[tool.mutmut]":
                    capturing = True
                elif capturing:
                    break
            if capturing:
                section.append(line)
        print("\n".join(section))
        print("\n(dry run -- mutmut NOT invoked; TOML validated as parseable.)")
        return 0

    # Real sweep: gate on platform. mutmut is unusable on Windows.
    if platform.system() != "Linux":
        print(
            f"refusing to run mutmut on {platform.system()}: mutmut 3.x is Linux-only "
            "(it sys.exit(1)s on Windows and imports the POSIX-only `resource` module).",
            file=sys.stderr,
        )
        print(
            "Run the real sweep on Linux: locally, or via the "
            "`.github/workflows/mutation.yml` workflow_dispatch matrix.\n"
            "On this box, use --dry-run to validate the scoped config.",
            file=sys.stderr,
        )
        return 3

    _recover_stale_backup()
    return run_sweep(scope)


if __name__ == "__main__":
    raise SystemExit(main())
