#!/usr/bin/env python3
"""Compute the mutmut *reachability* shortlist for the cluster-verb modules.

Motivation (devx B4, ``docs/plans/devx-and-pack-seams-2026-07-15.md``): mutmut
3.x silently skips any function that has an ``import`` statement in its body
(``docs/internals/mutation-testing.md`` -> "Limitations"). This codebase uses
lazy imports heavily in exactly the high-severity cluster verbs
(``submit_flow`` / ``aggregate_flow`` / transport), so a full sweep produces
few-to-zero mutants there -- a silent wrong-path costs real cluster time and
mutmut never sees it.

This script does two jobs, both purely static (AST only, no execution):

1. ``report`` (default) -- classify every top-level function / method in the
   target modules as mutmut-REACHABLE (no body import; mutmut can mutate it)
   or mutmut-BLIND (a lazy import blocks it). The BLIND set is the *extraction
   shortlist*: the concrete functions whose module-scope-import extraction
   would buy new mutation coverage. This is the spec the maintainer ruling
   asked for -- it names the work without doing the churned-file refactor.

2. ``paths`` -- emit the newline-separated source paths mutmut should scope
   ``[tool.mutmut].paths_to_mutate`` to for a targeted sweep. With
   ``--changed-since REF`` the target set is intersected with the files a diff
   touched, so a scheduled run can scope to just-changed cluster verbs. With
   ``--apply-to-pyproject`` the computed list is written into
   ``[tool.mutmut].paths_to_mutate`` in place (used by the scheduled
   ``mutation.yml`` workflow on its ephemeral checkout -- never committed).

Neither mode runs mutmut or the test suite. It is safe to run anywhere.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Repo root = parent of this scripts/ dir.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The cluster-verb modules the B4 ruling targets: high-severity paths where a
# silent wrong-path costs real cluster time. Relative to REPO_ROOT. Transport
# is a package now (was infra/remote.py when the doc was first written); the
# submit / aggregate flows are single churned files.
DEFAULT_TARGETS: tuple[str, ...] = (
    "src/hpc_agent/ops/submit_flow.py",
    "src/hpc_agent/ops/aggregate_flow.py",
    "src/hpc_agent/infra/transport/__init__.py",
    "src/hpc_agent/infra/transport/_pull.py",
    "src/hpc_agent/infra/transport/_combiner.py",
)

# The scoped ``tests_dir`` for the scheduled sweep (memo Unit A). The committed
# ``[tool.mutmut].tests_dir`` is the WHOLE 8k-test suite -- with it, mutmut
# cannot fit a baseline + a single mutant inside the sweep step, so every one of
# the 6076 cluster-verb mutants stayed ``not checked`` and the job read green on
# ZERO signal. This is the focused, IN-PROCESS covering set for the five
# cluster-verb targets above -- pure-API + block-flow tests that exercise
# submit_flow / aggregate_flow / transport without a live cluster or a
# subprocess (mutmut cannot instrument a child interpreter, and deselects
# ``@pytest.mark.slow``). Scoping ``tests_dir`` here is what lets a baseline run
# and mutants actually get checked. Missing entries are warned-and-skipped at
# apply time so a test rename degrades gracefully rather than breaking the sweep.
CLUSTER_VERB_TESTS: tuple[str, ...] = (
    # submit_flow
    "tests/ops/test_submit_flow_pure_api.py",
    "tests/ops/submit/test_flow.py",
    # aggregate_flow
    "tests/ops/test_aggregate_flow_pure_api.py",
    "tests/ops/test_aggregate_flow_pure_api_reduce.py",
    # transport/{__init__,_pull,_combiner}
    "tests/infra/test_remote.py",
    "tests/infra/test_transport_pull.py",
    "tests/infra/test_transport_prune.py",
    "tests/infra/test_combiner_progress.py",
    "tests/infra/test_transport_delta_cache_checkpoint.py",
)


# mutant exit codes in a ``*.meta`` ``exit_code_by_key`` map: 1 = killed,
# 0 = survived, 33/34 = no-tests/skipped, ``null`` = NEVER EXECUTED. A mutant is
# "checked" iff mutmut produced any non-null verdict for it.
def count_checked_mutants(mutants_dir: Path) -> tuple[int, int]:
    """Return ``(checked, total)`` across every ``*.meta`` under *mutants_dir*.

    ``total`` counts every mutant key mutmut generated; ``checked`` counts those
    with a NON-NULL exit code (killed / survived / no-tests / skipped -- i.e.
    mutmut actually evaluated them). A ``null`` code means the mutant was never
    executed (the zero-signal failure the sweep hit on run 29560911639, where all
    6076 were null). This is the tripwire's measurement -- pure I/O over the same
    ``*.meta`` artifacts the triage read, so it is unit-testable without mutmut.
    Delegates to :func:`_tally_mutants` (which also computes the stronger *signal*
    count the refined tripwire gates on).
    """
    _signal, checked, total = _tally_mutants(mutants_dir)
    return checked, total


def _tally_mutants(mutants_dir: Path) -> tuple[int, int, int]:
    """Return ``(signal, checked, total)`` over every ``*.meta`` under *dir*.

    ``signal`` = mutants killed (1) OR survived (0) -- i.e. actually exercised;
    ``checked`` = any non-null code (adds 33 no-tests / 34 skipped); ``total`` =
    every generated key. The tripwire gates on ``signal`` so a run whose mutants
    are ALL exit-33 "no tests" (``checked > 0`` but zero real signal) still turns
    the job RED rather than faking green (triage-2 refinement).
    """
    import json

    signal = 0
    checked = 0
    total = 0
    for meta in sorted(mutants_dir.rglob("*.meta")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        codes = data.get("exit_code_by_key")
        if not isinstance(codes, dict):
            continue
        for code in codes.values():
            total += 1
            if code is not None:
                checked += 1
            if code in (0, 1):
                signal += 1
    return signal, checked, total


@dataclass
class FuncVerdict:
    qualname: str
    lineno: int
    blind: bool
    # First lazy-import statement text, for the report.
    first_import: str | None = None


@dataclass
class ModuleReport:
    path: str
    reachable: list[FuncVerdict] = field(default_factory=list)
    blind: list[FuncVerdict] = field(default_factory=list)
    parse_error: str | None = None


def _has_body_import(node: ast.AST) -> ast.stmt | None:
    """Return the first import statement lexically inside ``node``'s subtree,
    NOT descending into nested function / class definitions (those are their
    own mutmut units). ``node`` is a FunctionDef / AsyncFunctionDef."""
    for child in ast.iter_child_nodes(node):
        found = _walk_for_import(child)
        if found is not None:
            return found
    return None


def _walk_for_import(node: ast.AST) -> ast.stmt | None:
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return node
    # Do not cross into a nested def/class -- it is a separate mutmut unit.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return None
    for child in ast.iter_child_nodes(node):
        found = _walk_for_import(child)
        if found is not None:
            return found
    return None


def _import_text(stmt: ast.stmt) -> str:
    if isinstance(stmt, ast.ImportFrom):
        mod = ("." * (stmt.level or 0)) + (stmt.module or "")
        names = ", ".join(a.name for a in stmt.names)
        return f"from {mod} import {names}"
    if isinstance(stmt, ast.Import):
        return "import " + ", ".join(a.name for a in stmt.names)
    return ast.dump(stmt)


def _classify_functions(tree: ast.AST, prefix: str = "") -> list[FuncVerdict]:
    """Classify every function definition at this scope (and methods one class
    deep) as blind / reachable."""
    verdicts: list[FuncVerdict] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qn = f"{prefix}{node.name}"
            stmt = _has_body_import(node)
            verdicts.append(
                FuncVerdict(
                    qualname=qn,
                    lineno=node.lineno,
                    blind=stmt is not None,
                    first_import=_import_text(stmt) if stmt is not None else None,
                )
            )
        elif isinstance(node, ast.ClassDef):
            verdicts.extend(_classify_functions(node, prefix=f"{node.name}."))
    return verdicts


def analyze_module(path: Path) -> ModuleReport:
    rel = path.relative_to(REPO_ROOT).as_posix()
    report = ModuleReport(path=rel)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError) as exc:  # pragma: no cover - defensive
        report.parse_error = str(exc)
        return report
    for v in _classify_functions(tree):
        (report.blind if v.blind else report.reachable).append(v)
    return report


def _changed_paths(ref: str) -> set[str]:
    """Repo-relative POSIX paths changed vs ``ref`` (name-only diff)."""
    out = subprocess.run(
        ["git", "diff", "--name-only", ref, "--"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def resolve_targets(targets: list[str], changed_since: str | None) -> list[Path]:
    resolved: list[Path] = []
    for t in targets:
        p = (REPO_ROOT / t).resolve()
        if not p.is_file():
            print(f"warning: target not found, skipping: {t}", file=sys.stderr)
            continue
        resolved.append(p)
    if changed_since:
        changed = _changed_paths(changed_since)
        resolved = [p for p in resolved if p.relative_to(REPO_ROOT).as_posix() in changed]
    return resolved


def cmd_report(reports: list[ModuleReport], as_json: bool) -> int:
    if as_json:
        import json

        payload = [
            {
                "path": r.path,
                "parse_error": r.parse_error,
                "reachable": [{"qualname": v.qualname, "lineno": v.lineno} for v in r.reachable],
                "blind": [
                    {
                        "qualname": v.qualname,
                        "lineno": v.lineno,
                        "first_import": v.first_import,
                    }
                    for v in r.blind
                ],
            }
            for r in reports
        ]
        print(json.dumps(payload, indent=2))
        return 0

    total_reachable = total_blind = 0
    for r in reports:
        print(f"\n=== {r.path} ===")
        if r.parse_error:
            print(f"  PARSE ERROR: {r.parse_error}")
            continue
        total_reachable += len(r.reachable)
        total_blind += len(r.blind)
        print(f"  reachable (mutmut can mutate): {len(r.reachable)}")
        print(f"  BLIND (lazy import blocks mutmut): {len(r.blind)}")
        if r.blind:
            print("  extraction shortlist -- extract these imports module-scope:")
            for v in r.blind:
                print(f"    L{v.lineno:>5}  {v.qualname}   [{v.first_import}]")
    print(
        f"\nSUMMARY: {total_reachable} reachable, {total_blind} blind across "
        f"{len(reports)} module(s)."
    )
    if total_reachable == 0:
        print(
            "NOTE: zero reachable functions -- a scoped mutmut sweep would "
            "produce no mutants until extractions land."
        )
    return 0


def _replace_mutmut_array(text: str, key: str, values: list[str]) -> str:
    """Rewrite the ``<key> = [ ... ]`` array inside ``[tool.mutmut]`` in place.

    Minimal line-based rewrite (no tomlkit dep): find the ``key`` assignment
    inside the ``[tool.mutmut]`` table and replace through its closing ``]``,
    preserving every sibling key (``also_copy`` / ``do_not_mutate`` /
    ``pytest_add_cli_args``). Raises if the key is absent so a silent no-scope
    can never slip through. Mirrors ``run_mutation.py._replace_named_array``.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_mutmut = False
    i = 0
    replaced = False
    new_block = [f"{key} = ["] + [f'    "{v}",' for v in values] + ["]"]
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mutmut = stripped == "[tool.mutmut]"
        if in_mutmut and stripped.startswith(key):
            # Consume through the closing bracket of the array.
            while i < len(lines) and "]" not in lines[i]:
                i += 1
            i += 1  # skip the closing-bracket line
            out.extend(new_block)
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        raise SystemExit(f"could not find [tool.mutmut].{key} in the pyproject")
    return "\n".join(out) + "\n"


def _resolve_tests_dir() -> list[str]:
    """The scoped cluster-verb ``tests_dir`` -- existing entries only.

    Warns-and-skips any missing test path so a rename degrades gracefully.
    Refuses to return an empty list (scoping to nothing would abort mutmut).
    """
    present: list[str] = []
    for t in CLUSTER_VERB_TESTS:
        if (REPO_ROOT / t).is_file():
            present.append(t)
        else:
            print(f"warning: cluster-verb test not found, skipping: {t}", file=sys.stderr)
    if not present:
        raise SystemExit("no cluster-verb tests_dir entries exist -- cannot scope the sweep")
    return present


def _apply_to_pyproject(pyproject: Path, paths: list[str], *, scope_tests_dir: bool) -> None:
    """Rewrite ``[tool.mutmut].paths_to_mutate`` (and optionally ``tests_dir``).

    ``scope_tests_dir`` additionally narrows ``tests_dir`` from the committed
    whole-suite default to :data:`CLUSTER_VERB_TESTS` -- the memo Unit A fix that
    lets the sweep actually check mutants instead of leaving all 6076 ``not
    checked``. Ephemeral-checkout only (never committed), same as the paths
    rewrite.
    """
    text = pyproject.read_text(encoding="utf-8")
    text = _replace_mutmut_array(text, "paths_to_mutate", paths)
    if scope_tests_dir:
        text = _replace_mutmut_array(text, "tests_dir", _resolve_tests_dir())
    pyproject.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mutmut reachability shortlist for the cluster verbs."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="report",
        choices=["report", "paths", "tripwire"],
        help="report (default): blind/reachable breakdown; "
        "paths: emit scoped paths_to_mutate list; "
        "tripwire: FAIL (exit 1) if zero mutants were checked in --mutants-dir.",
    )
    parser.add_argument(
        "--targets",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="override the target module list (repo-relative paths).",
    )
    parser.add_argument(
        "--changed-since",
        metavar="REF",
        default=None,
        help="intersect targets with files changed vs REF (git diff).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="report mode: emit machine-readable JSON.",
    )
    parser.add_argument(
        "--apply-to-pyproject",
        metavar="PATH",
        default=None,
        help="paths mode: rewrite [tool.mutmut].paths_to_mutate in this "
        "pyproject.toml (ephemeral CI checkout only -- never commit).",
    )
    parser.add_argument(
        "--apply-tests-dir",
        action="store_true",
        help="paths mode with --apply-to-pyproject: ALSO narrow "
        "[tool.mutmut].tests_dir to the cluster-verb covering set so the sweep "
        "actually checks mutants (memo Unit A). Never commit the result.",
    )
    parser.add_argument(
        "--mutants-dir",
        metavar="DIR",
        default="mutants",
        help="tripwire mode: the mutmut output dir to scan for *.meta (default: mutants).",
    )
    args = parser.parse_args(argv)

    if args.mode == "tripwire":
        mutants_dir = Path(args.mutants_dir)
        if not mutants_dir.is_absolute():
            mutants_dir = REPO_ROOT / mutants_dir
        signal, checked, total = _tally_mutants(mutants_dir)
        print(
            f"mutation tripwire: {signal} with-signal (killed/survived) / "
            f"{checked} checked / {total} generated mutant(s)."
        )
        if signal == 0:
            print(
                "TRIPWIRE FAILED: not one mutant was killed or survived -- the sweep "
                "produced no signal (every mutant 'not checked', or ALL 'no tests'). "
                "A green run must mean signal. This is the run-29560911639 zero-signal "
                "failure (refined past the exit-33 'no tests' loophole); do NOT trust a "
                "green sweep. Check the scoped tests_dir + baseline (memo Unit A).",
                file=sys.stderr,
            )
            return 1
        print("tripwire OK: the sweep killed or survived at least one mutant.")
        return 0

    paths = resolve_targets(args.targets, args.changed_since)
    rel_paths = [p.relative_to(REPO_ROOT).as_posix() for p in paths]

    if args.mode == "paths":
        if args.apply_to_pyproject:
            if not rel_paths:
                print(
                    "no targets after filtering; leaving pyproject untouched.",
                    file=sys.stderr,
                )
                return 0
            _apply_to_pyproject(
                Path(args.apply_to_pyproject), rel_paths, scope_tests_dir=args.apply_tests_dir
            )
            print(f"wrote {len(rel_paths)} path(s) to {args.apply_to_pyproject}")
            for p in rel_paths:
                print(f"  {p}")
            if args.apply_tests_dir:
                print(f"scoped tests_dir to {len(_resolve_tests_dir())} cluster-verb test file(s)")
            return 0
        for p in rel_paths:
            print(p)
        return 0

    reports = [analyze_module(p) for p in paths]
    return cmd_report(reports, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
