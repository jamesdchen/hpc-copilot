"""CI lint: cluster-deployed ``.py`` files stay under the Python version floor.

The runtime deploy ships a small stdlib-only subset of ``hpc_agent`` to the
cluster (see :func:`hpc_agent.infra.transport._build_deploy_items`) and runs
three of those files under whatever ``python3`` the cluster happens to have —
``.hpc/_hpc_combiner.py``, ``.hpc/_hpc_dispatch.py``, and
``python -m hpc_agent.execution.mapreduce.reduce.status`` — plus imports
``metrics_io`` / ``executor_cli`` from inside user jobs. Those interpreters are
frequently OLD: RHEL/Rocky 8 ships ``python3`` = 3.6.8, and torch-1.x conda
envs commonly pin 3.8/3.9.

Nothing in CI executes the shipped files on an old interpreter, so a routine
modernization pass can silently raise their floor and the break only surfaces
*after* a campaign has burned its cluster hours (F18: a ruff ``B905`` pass
added ``zip(strict=True)`` — 3.10+ — to the combiner, crashing every wave
combine on <3.10 clusters; ``str.removeprefix`` — 3.9+ — did the same to
``read_kw_env`` on <=3.8).

This lint AST-scans every ``.py`` file the deploy actually ships (enumerated
from ``_build_deploy_items``, so the check can't drift from the ship list) for
syntax/attribute features newer than :data:`DEPLOY_PYTHON_FLOOR` and fails if
any are present. It is a curated, stdlib-only feature scan (no third-party
``vermin`` dependency): it catches the modernization families a lint would
introduce — ``zip(strict=)``, ``str.removeprefix``/``removesuffix``, ``match``
statements, ``except*``, and PEP 695 ``type`` aliases — not every conceivable
version-gated construct. The shared scanner is importable so the contract test
``tests/contracts/test_cluster_runtime_self_contained.py`` exercises its
fire path.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The oldest interpreter the deployed files must import + run under. Deliberately
# BELOW 3.9 so a re-introduced ``str.removeprefix`` (3.9) is caught: the whole
# point of F18's fix is to keep these files working on the <3.9 clusters the
# module docstrings promise ("stdlib-only, any cluster python3"). Kept at 3.8
# (torch-1.x conda floor); dispatch.py's ``time.time_ns`` already needs 3.7.
DEPLOY_PYTHON_FLOOR = (3, 8)


def scan_source(source: str, filename: str = "<deploy>") -> list[tuple[str, tuple[int, int]]]:
    """Return ``[(feature, min_version), ...]`` for features newer than the floor.

    A curated AST scan for the modernization families that would raise the
    deployed files' Python floor. Empty list means *source* is within
    :data:`DEPLOY_PYTHON_FLOOR`. Raises ``SyntaxError`` if *source* does not
    parse under the running interpreter.
    """
    tree = ast.parse(source, filename=filename)
    hits: list[tuple[str, tuple[int, int]]] = []

    def flag(feature: str, min_version: tuple[int, int]) -> None:
        if min_version > DEPLOY_PYTHON_FLOOR:
            hits.append((feature, min_version))

    # ``ast.Match`` / ``ast.TryStar`` / ``ast.TypeAlias`` exist only on the
    # interpreters that introduced them; guard with getattr so this scanner
    # itself runs on any 3.8+ CI interpreter.
    match_cls = getattr(ast, "Match", ())
    trystar_cls = getattr(ast, "TryStar", ())
    typealias_cls = getattr(ast, "TypeAlias", ())

    for node in ast.walk(tree):
        if match_cls and isinstance(node, match_cls):
            flag("match statement", (3, 10))
        elif trystar_cls and isinstance(node, trystar_cls):
            flag("except* (exception groups)", (3, 11))
        elif typealias_cls and isinstance(node, typealias_cls):
            flag("PEP 695 'type' alias", (3, 12))
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "zip"
                and any(kw.arg == "strict" for kw in node.keywords)
            ):
                flag("zip(strict=...)", (3, 10))
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                "removeprefix",
                "removesuffix",
            ):
                flag(f"str.{node.func.attr}", (3, 9))
    return hits


def _deployed_py_files() -> list[tuple[str, Path]]:
    """``[(dst_rel, src_path), ...]`` for every ``.py`` file the deploy ships.

    Unions both scheduler families so a single-family deploy can't hide a
    file; deduped by ``dst_rel``. Only ``src_path``-backed items are Python
    modules (the rendered array scripts are shell), so ``content``-only items
    are skipped.
    """
    from hpc_agent.infra import transport

    seen: dict[str, Path] = {}
    for scheduler in ("sge", "slurm"):
        for it in transport._build_deploy_items(scheduler=scheduler):
            if it.dst_rel.endswith(".py") and it.src_path is not None:
                seen.setdefault(it.dst_rel, it.src_path)
    return sorted(seen.items())


def main() -> int:
    try:
        files = _deployed_py_files()
    except Exception as exc:  # noqa: BLE001 - a broken import must fail the lint loudly
        print(f"ERROR: could not enumerate the deploy ship list: {exc}")
        return 1

    floor = ".".join(str(x) for x in DEPLOY_PYTHON_FLOOR)
    violations: list[str] = []
    for dst_rel, src_path in files:
        try:
            source = src_path.read_text(encoding="utf-8")
        except OSError as exc:
            violations.append(f"{dst_rel}: unreadable ({exc})")
            continue
        for feature, min_version in scan_source(source, filename=dst_rel):
            ver = ".".join(str(x) for x in min_version)
            violations.append(f"{dst_rel}: uses {feature} (Python {ver}+, deploy floor is {floor})")

    if violations:
        print(f"ERROR: cluster-deployed files use features above the Python {floor} floor:")
        for v in violations:
            print(f"  {v}")
        print(
            "\nWhy: these files are scp'd standalone and run under whatever ``python3`` "
            "the cluster has (RHEL/Rocky 8 = 3.6.8; torch-1.x conda = 3.8/3.9). A feature "
            "newer than the floor crashes the wave combine / dispatcher / status reporter "
            "AFTER the campaign has burned its hours. Rewrite the construct to stdlib that "
            f"runs on Python {floor} (e.g. bare ``zip`` + ``# noqa: B905`` instead of "
            "``zip(strict=)``; ``s[len(prefix):]`` instead of ``str.removeprefix``), or, if "
            "the floor genuinely must move, raise DEPLOY_PYTHON_FLOOR here deliberately."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
