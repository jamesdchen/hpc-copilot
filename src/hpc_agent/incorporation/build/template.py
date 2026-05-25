"""``build-template`` — inject the experiment-template scaffold.

The experiment-template scaffold lives *inside* hpc-agent
(``hpc_agent/incorporation/build/scaffolds/``); there is no separate template repo
to clone. ``build-template`` injects it into a target repo — extending
what hpc-agent already does with single files (``build-executor``,
``build-tasks-py``, the ``.hpc/cli.py`` copy) to a whole-project
scaffold.

Deliberately **not a wire primitive.** It is a human-facing CLI command
(``hpc-agent build-template``), not part of the integrator-agnostic
primitive catalog that headless orchestrators compose against. The
experiment-template flow is built around researcher-authored notebooks;
a headless agent does not write notebooks, so this scaffold step is
exclusive to the human entry point.

Two tiers, with different overwrite discipline:

- **Framework-owned** (``.hpc/template.mk``, ``.hpc/scaffold.py``) —
  re-injected verbatim on every run. Self-healing, the same way
  ``.hpc/cli.py`` self-heals; never refused.
- **Repo root** (``Makefile``, ``.pre-commit-config.yaml``,
  ``.github/workflows/ci.yml``, ``conftest.py``, ``pyproject.toml``) —
  paths fixed by make / pip / pre-commit / GitHub Actions, so they
  *cannot* live under ``.hpc/``. Refused without ``--force`` when they
  already exist, except where a non-destructive merge is trivial
  (``Makefile`` gains one ``include`` line; an existing
  ``pyproject.toml`` is left untouched and the fragment is dropped
  under ``.hpc/`` for a hand-merge).

The ``hpc_agent.experiment_kit`` library itself is **not** injected — it is a
``pip install hpc-agent`` dependency, imported, never vendored.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hpc_agent
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

# (scaffold asset, destination relative to repo root).
# Framework-owned: rewritten verbatim every run.
_FRAMEWORK_ASSETS: tuple[tuple[str, str], ...] = (
    ("template.mk.tmpl", ".hpc/template.mk"),
    ("scaffold.py.tmpl", ".hpc/scaffold.py"),
)
# Repo-root: refuse-without-force.
_ROOT_ASSETS: tuple[tuple[str, str], ...] = (
    ("ci.yml.tmpl", ".github/workflows/ci.yml"),
    ("pre-commit-config.yaml.tmpl", ".pre-commit-config.yaml"),
    ("conftest.py.tmpl", "conftest.py"),
)

# The single line a root Makefile needs; appended non-destructively.
_MAKEFILE_INCLUDE = "include .hpc/template.mk"


@primitive(
    name="build-template",
    verb="scaffold",
    side_effects=[
        SideEffect(
            "writes-file",
            "<repo_dir>/{.hpc/template.mk,.hpc/scaffold.py} (self-healing); "
            "<repo_dir>/{Makefile,.gitignore,pyproject.toml,.pre-commit-config.yaml,"
            "conftest.py,.github/workflows/ci.yml} (refuse-without-force at repo root)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="repo_dir",
    cli=CliShape(
        help="Inject the experiment-template scaffold into a repo.",
        args=(
            CliArg(
                "--repo-dir",
                type=Path,
                default=Path.cwd(),
                help="Target repository root (default: CWD).",
            ),
            CliArg(
                "--force",
                action="store_true",
                help=(
                    "Overwrite repo-root files that already exist. The "
                    "framework-owned .hpc/ assets are re-injected regardless."
                ),
            ),
        ),
    ),
    # Registered with the verb=scaffold convention that the contract
    # test ``test_scaffolds_are_agent_facing`` enforces. Even though
    # build-template is primarily a human entry point, the agent walks
    # users through scaffold flows and needs visibility.
    agent_facing=True,
)
def build_template(*, repo_dir: Path, force: bool = False) -> dict[str, Any]:
    """Inject the experiment-template scaffold into ``repo_dir``.

    Parameters
    ----------
    repo_dir:
        Target repository root. Must already exist.
    force:
        Overwrite repo-root files that already exist. The framework-owned
        ``.hpc/`` assets are re-injected regardless of this flag.

    Returns
    -------
    ``{repo_dir, framework_files, written, skipped, merged,
    needs_manual_merge}`` — ``framework_files`` are the self-healing
    ``.hpc/`` assets; ``written`` are root files newly created (or
    overwritten under ``--force``); ``skipped`` already existed and were
    refused; ``merged`` were updated non-destructively;
    ``needs_manual_merge`` flags a pre-existing ``pyproject.toml`` whose
    fragment was dropped under ``.hpc/`` instead.

    Raises
    ------
    errors.SpecInvalid
        If ``repo_dir`` does not exist or is not a directory.
    """
    if not repo_dir.is_dir():
        raise errors.SpecInvalid(f"repo-dir {repo_dir} does not exist or is not a directory")

    scaffold_dir = hpc_agent._PACKAGE_ROOT / "incorporation" / "build" / "scaffolds"

    def _asset(name: str) -> str:
        return (scaffold_dir / name).read_text(encoding="utf-8")

    framework: list[str] = []
    written: list[str] = []
    skipped: list[str] = []
    merged: list[str] = []
    needs_manual_merge: list[str] = []

    # 1. Framework-owned .hpc/ assets — always (re-)written; self-healing.
    for asset, rel in _FRAMEWORK_ASSETS:
        dest = repo_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_asset(asset), encoding="utf-8")
        framework.append(rel)

    # 2. Repo-root assets — refuse-without-force.
    for asset, rel in _ROOT_ASSETS:
        dest = repo_dir / rel
        if dest.exists() and not force:
            skipped.append(rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_asset(asset), encoding="utf-8")
        written.append(rel)

    # 3. Makefile — non-destructive merge via the `include` indirection.
    makefile = repo_dir / "Makefile"
    if not makefile.exists():
        makefile.write_text(_asset("Makefile.tmpl"), encoding="utf-8")
        written.append("Makefile")
    else:
        text = makefile.read_text(encoding="utf-8")
        if _MAKEFILE_INCLUDE in text:
            skipped.append("Makefile")
        else:
            sep = "" if text.endswith("\n") else "\n"
            makefile.write_text(
                f"{text}{sep}\n{_MAKEFILE_INCLUDE}  # hpc-agent experiment template\n",
                encoding="utf-8",
            )
            merged.append("Makefile")

    # 4. pyproject.toml — never blind-clobbered. Absent: write the
    #    starter. Present: drop the fragment under .hpc/ for a hand-merge.
    pyproject = repo_dir / "pyproject.toml"
    if not pyproject.exists():
        pyproject.write_text(_asset("pyproject.toml.tmpl"), encoding="utf-8")
        written.append("pyproject.toml")
    else:
        fragment = repo_dir / ".hpc" / "pyproject-fragment.toml"
        fragment.parent.mkdir(parents=True, exist_ok=True)
        fragment.write_text(_asset("pyproject.toml.tmpl"), encoding="utf-8")
        framework.append(".hpc/pyproject-fragment.toml")
        needs_manual_merge.append("pyproject.toml")

    # 5. .gitignore — non-destructive merge. The generated set (src/,
    #    .hpc/tasks.py, .hpc/cli.py, .hpc/.build-cache.json) must not be
    #    committed; absent .gitignore gets the starter, an existing one
    #    gains the hpc-agent block if it isn't already there.
    gitignore = repo_dir / ".gitignore"
    gitignore_block = _asset("gitignore.tmpl")
    if not gitignore.exists():
        gitignore.write_text(gitignore_block, encoding="utf-8")
        written.append(".gitignore")
    else:
        text = gitignore.read_text(encoding="utf-8")
        if "hpc-agent experiment-template" in text:
            skipped.append(".gitignore")
        else:
            sep = "" if text.endswith("\n") else "\n"
            gitignore.write_text(f"{text}{sep}\n{gitignore_block}", encoding="utf-8")
            merged.append(".gitignore")

    return {
        "repo_dir": str(repo_dir),
        "framework_files": framework,
        "written": written,
        "skipped": skipped,
        "merged": merged,
        "needs_manual_merge": needs_manual_merge,
    }


__all__ = ["build_template"]
