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

from functools import lru_cache as _lru_cache
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


def rendered_templates_dir() -> Path:
    """Materialise a ``templates``-shaped tree with the rendered runtime scripts.

    Phase 2 (Option C) deleted the static per-scheduler ``cpu_array`` /
    ``gpu_array`` files — they are now *rendered* from the scheduler
    profile by ``render_script``. Tests that historically read those files
    off disk call this helper instead: it renders the four array scripts
    into a temp dir laid out exactly like the old on-disk tree
    (``runtime/<sched>/<name>``) and copies the still-shipped
    ``runtime/common/`` preambles alongside, so a test can keep using
    ``dir / "runtime/sge/cpu_array.sh"`` semantics unchanged. Cached so the
    render happens once per test session.
    """
    return _render_templates_once()


@_lru_cache(maxsize=1)
def _render_templates_once() -> Path:
    import shutil
    import tempfile

    from hpc_agent.infra.backends import get_backend_class, template_ext_for

    root = Path(tempfile.mkdtemp(prefix="hpc_rendered_templates_"))
    runtime = root / "runtime"
    for sched in ("sge", "slurm"):
        ext = template_ext_for(sched).lstrip(".")
        sub = runtime / sched
        sub.mkdir(parents=True, exist_ok=True)
        backend_cls = get_backend_class(sched)
        for basename, kind in (("cpu_array", "cpu"), ("gpu_array", "gpu")):
            (sub / f"{basename}.{ext}").write_text(
                backend_cls.render_script(kind=kind), encoding="utf-8", newline=""
            )
    # The shared preambles are NOT rendered — they still ship as static
    # files; copy them so the rendered tree mirrors the full runtime/ layout.
    common_src = TEMPLATES_DIR / "runtime" / "common"
    if common_src.is_dir():
        shutil.copytree(common_src, runtime / "common")
    return root
