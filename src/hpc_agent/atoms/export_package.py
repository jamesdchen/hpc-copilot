"""``export-package`` primitive — build the experiment's ``src/`` package.

The experiment repo commits **nothing generated**: the notebook → ``.py``
export is a submit-time (and CI / local-repro / elision-gate) step. This
primitive globs the experiment's notebooks, derives each output path by
convention, auto-picks the exporter, content-hash-caches unchanged
notebooks, and writes the whole ``src/`` package.

**Convention, not a manifest.** Notebooks under ``notebooks/pipeline/``,
``notebooks/executors/``, and ``notebooks/scripts/`` export; nothing else
does. The output module name is the notebook stem with a leading
``\\d+[a-z]?_`` ordering prefix stripped — ``01_loading.ipynb`` →
``src/loading.py``. The exporter is auto-picked: a notebook that
*applies* the ``@register_run`` decorator is a runnable executor →
strict-AST :func:`~hpc_agent.template.export_notebook` (runtime inlined);
everything else — including pipeline-library notebooks that merely
import the runtime seam — uses the ``# export``-marker
:func:`~hpc_agent.template.export_notebook_markers`. Detection is the
``@register_run`` decorator, not the ``hpc_agent.template`` import:
library notebooks import the runtime (``current_slice`` /
``load_series``) without being experiments.

**One cached build.** Each notebook's concatenated code-cell sources are
hashed against ``.hpc/.build-cache.json``; an unchanged notebook whose
output still exists is skipped. Export is pure AST extraction plus a
``ruff`` post-pass — sub-second per file, no notebook execution.

Output lands at the repo-root ``src/`` (which the experiment repo
``.gitignore``\\ s) so ``src.<module>`` imports and ``PYTHONPATH`` are
unchanged — zero module-path churn.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent._schema_models.actions.export_package import ExportPackageInput

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["export_package"]

# Notebook subdirectories whose ``.ipynb`` files are exported to ``src/``.
_EXPORT_SUBDIRS = ("pipeline", "executors", "scripts")

# Leading ordering prefix stripped from a notebook stem to form the
# output module name: digits, an optional single letter, an underscore —
# ``01_``, ``06b_``.
_ORDER_PREFIX = re.compile(r"^\d+[a-z]?_")

_CACHE_FILENAME = ".build-cache.json"


def _output_stem(notebook_stem: str) -> str:
    """The ``src/`` module name for a notebook stem (ordering prefix stripped)."""
    return _ORDER_PREFIX.sub("", notebook_stem)


def _cell_hash(ipynb: Path) -> str:
    """SHA-256 over a notebook's concatenated code-cell sources.

    Outputs, ``execution_count``, metadata, and markdown cells are
    excluded — only the code that feeds the exporter affects the hash,
    so merely re-running a cell is not a cache miss.
    """
    data = json.loads(ipynb.read_text(encoding="utf-8"))
    parts: list[str] = []
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source", "")
        if isinstance(src, list):
            src = "".join(src)
        parts.append(src)
    blob = "\x00".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ruff_canonicalise(path: Path) -> None:
    """Best-effort byte-canonicalisation: ``ruff check --fix`` + ``ruff format``.

    Keeps the exported ``.py`` byte-stable so the content-hash cache and
    any diff tooling stay reliable. When ``ruff`` is not on ``PATH`` the
    export is still deterministic (pure AST extraction) — skip silently.
    """
    for argv in (
        ["ruff", "check", "--fix", "--quiet", str(path)],
        ["ruff", "format", "--quiet", str(path)],
    ):
        try:
            subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
        except (FileNotFoundError, OSError):
            return


@primitive(
    name="export-package",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/src/*.py"),
        SideEffect("writes-sidecar", "<experiment>/.hpc/.build-cache.json"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli="hpc-agent export-package",
    agent_facing=True,
)
def export_package(
    experiment_dir: Path,
    *,
    spec: ExportPackageInput | None = None,
) -> dict[str, Any]:
    """Build ``<experiment>/src/`` from the experiment's notebooks.

    Globs ``notebooks/{pipeline,executors,scripts}/*.ipynb``, derives
    each output path by the module convention, auto-picks the exporter
    by content, content-hash-caches against ``.hpc/.build-cache.json``,
    and writes ``src/``. An ``src/__init__.py`` is created so the output
    is an importable package.

    Returns ``{src_dir, built, cache_hits, n_notebooks, cache_path}`` —
    ``built`` lists the modules (re)exported this call, ``cache_hits``
    the unchanged ones skipped. A second call with no notebook edits is
    therefore all ``cache_hits`` and byte-stable.

    Raises ``errors.SpecInvalid`` when two notebooks map to the same
    ``src/`` module name, or a stem is not a valid Python identifier.
    """
    from hpc_agent.template.discover import discover_runs
    from hpc_agent.template.notebook import export_notebook, export_notebook_markers

    if spec is None:
        spec = ExportPackageInput()

    notebooks_root = experiment_dir / spec.notebooks_dir
    src_dir = experiment_dir / "src"
    cache_path = experiment_dir / ".hpc" / _CACHE_FILENAME

    notebooks: list[Path] = []
    for sub in _EXPORT_SUBDIRS:
        subdir = notebooks_root / sub
        if subdir.is_dir():
            notebooks.extend(sorted(subdir.glob("*.ipynb")))

    if not notebooks:
        return {
            "src_dir": str(src_dir),
            "built": [],
            "cache_hits": [],
            "n_notebooks": 0,
            "cache_path": str(cache_path),
        }

    # Detect output-path collisions before touching disk — two notebooks
    # whose stems strip to the same module name would silently clobber.
    by_output: dict[str, list[Path]] = {}
    for nb in notebooks:
        out_stem = _output_stem(nb.stem)
        if not out_stem.isidentifier():
            raise errors.SpecInvalid(
                f"notebook {nb.name} maps to src/{out_stem}.py, which is not a "
                "valid Python module name — rename the notebook"
            )
        by_output.setdefault(out_stem, []).append(nb)
    collisions = {k: v for k, v in by_output.items() if len(v) > 1}
    if collisions:
        detail = "; ".join(
            f"src/{stem}.py ← {', '.join(p.name for p in paths)}"
            for stem, paths in sorted(collisions.items())
        )
        raise errors.SpecInvalid(f"notebook output-path collisions in src/: {detail}")

    # Template-vs-marker is decided by the @register_run *decorator*, not
    # by importing hpc_agent.template — pipeline library notebooks import
    # the runtime seam (current_slice / load_series) without being
    # runnable experiments, and the strict-AST exporter would mangle them.
    # discover_runs resolves every decorator spelling (bare/aliased/attr).
    register_run_paths = {ri.path for ri in discover_runs(notebooks_root)}

    cache: dict[str, Any] = {}
    if cache_path.is_file() and not spec.force:
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            cache = loaded

    built: list[str] = []
    cache_hits: list[str] = []
    new_cache: dict[str, Any] = {}

    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "__init__.py").touch(exist_ok=True)

    for nb in notebooks:
        rel = nb.relative_to(experiment_dir).as_posix()
        out_stem = _output_stem(nb.stem)
        out_rel = f"src/{out_stem}.py"
        out_path = src_dir / f"{out_stem}.py"
        digest = _cell_hash(nb)

        prior = cache.get(rel)
        if (
            not spec.force
            and isinstance(prior, dict)
            and prior.get("hash") == digest
            and out_path.is_file()
        ):
            cache_hits.append(rel)
            new_cache[rel] = {"hash": digest, "output": out_rel}
            continue

        if nb.resolve() in register_run_paths:
            export_notebook(nb, out_path)
        else:
            export_notebook_markers(nb, out_path)
        _ruff_canonicalise(out_path)
        built.append(out_rel)
        new_cache[rel] = {"hash": digest, "output": out_rel}

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(new_cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return {
        "src_dir": str(src_dir),
        "built": sorted(built),
        "cache_hits": sorted(cache_hits),
        "n_notebooks": len(notebooks),
        "cache_path": str(cache_path),
    }
