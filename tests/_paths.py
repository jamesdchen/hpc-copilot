"""Repo-anchor paths for tests.

Tests that need the repo root (to point at ``src/hpc_agent/schemas/``,
the templates dir, etc.) historically used ``Path(__file__).parents[N]``
with a hardcoded ``N``. That pattern is depth-fragile: a test move
silently relocates what the path resolves to. This module climbs the
filesystem once at import time looking for ``pyproject.toml`` and
exposes the result as a constant, so every test references the same
anchor and depth changes can't break it.

Import as::

    from tests._paths import REPO_ROOT, SRC_DIR, SCHEMAS_DIR

Or, since pytest's collection adds ``tests/`` to ``sys.path``::

    from _paths import REPO_ROOT
"""

from __future__ import annotations

from pathlib import Path


def _find_repo_root() -> Path:
    """Climb until a ``pyproject.toml`` is found. Raises if absent."""
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(
        "tests/_paths.py: no pyproject.toml found by climbing from "
        f"{here} — repo root cannot be determined"
    )


#: Absolute path to the repository root (the directory containing
#: ``pyproject.toml``).
REPO_ROOT: Path = _find_repo_root()

#: Source tree root: ``<repo>/src``.
SRC_DIR: Path = REPO_ROOT / "src"

#: Generated JSON schemas: ``<repo>/src/hpc_agent/schemas``.
SCHEMAS_DIR: Path = SRC_DIR / "hpc_agent" / "schemas"

#: Executor / template scaffolds:
#: ``<repo>/src/hpc_agent/models/mapreduce/templates``.
TEMPLATES_DIR: Path = SRC_DIR / "hpc_agent" / "models" / "mapreduce" / "templates"
