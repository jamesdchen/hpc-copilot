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


def _apply_to_pyproject(pyproject: Path, paths: list[str]) -> None:
    """Rewrite ``[tool.mutmut].paths_to_mutate`` in place. Minimal line-based
    rewrite (no tomlkit dep): find the ``paths_to_mutate = [`` line inside the
    ``[tool.mutmut]`` table and replace through its closing ``]``."""
    lines = pyproject.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    in_mutmut = False
    i = 0
    replaced = False
    new_block = ["paths_to_mutate = ["]
    for p in paths:
        new_block.append(f'    "{p}",')
    new_block.append("]")
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mutmut = stripped == "[tool.mutmut]"
        if in_mutmut and stripped.startswith("paths_to_mutate"):
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
        raise SystemExit("could not find [tool.mutmut].paths_to_mutate in " + str(pyproject))
    pyproject.write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mutmut reachability shortlist for the cluster verbs."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="report",
        choices=["report", "paths"],
        help="report (default): blind/reachable breakdown; "
        "paths: emit scoped paths_to_mutate list.",
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
    args = parser.parse_args(argv)

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
            _apply_to_pyproject(Path(args.apply_to_pyproject), rel_paths)
            print(f"wrote {len(rel_paths)} path(s) to {args.apply_to_pyproject}")
            for p in rel_paths:
                print(f"  {p}")
            return 0
        for p in rel_paths:
            print(p)
        return 0

    reports = [analyze_module(p) for p in paths]
    return cmd_report(reports, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
