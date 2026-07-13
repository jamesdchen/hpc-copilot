"""Allowlist-style enforcement of the hpc-agent boundary contract.

Each test below declares what IS permitted (an allowlist) and asserts that
reality matches in both directions. Failures print actionable diffs that
point back to ``docs/reference/boundary-contract.md`` — the single source of truth for
the boundary between the framework and experiment repos.

Stdlib only (``ast``, ``pathlib``) plus ``yaml`` (already a project
dependency, see ``pyproject.toml``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

import hpc_agent
from hpc_agent.state.discover import _SKIP_BASENAMES, _SKIP_DIRS
from tests._paths import REPO_ROOT

CONTRACT_DOC = "docs/reference/boundary-contract.md"


# ---------------------------------------------------------------------------
# Allowlists — keep in sync with docs/reference/boundary-contract.md
# ---------------------------------------------------------------------------

ALLOWED_EXPORTS = frozenset(
    {
        # Package root
        "_PACKAGE_ROOT",
        "__version__",
        # Path resolution — canonical home for the .hpc/ layout
        "JournalLayout",
        "RepoLayout",
        # Config & discovery
        "get_template_path",
        "load_clusters_config",
        # Framework subdirectory layout (the .hpc/tasks.py model)
        "RUNS_SUBDIR",
        "TASKS_FILENAME",
        "load_tasks_module",
        # Primitive registry — the agent-extension surface
        "PrimitiveMeta",
        "SideEffect",
        "get_meta",
        "get_registry",
        "primitive",
        "register_primitives",
        # Researcher-facing experiment API
        "register_run",
    }
)

RESERVED_FILES = frozenset(
    {
        # Python package convention; not a framework reservation but the
        # discovery scanner skips it so it stays out of executor candidates.
        "__init__.py",
    }
)

# Reserved DIRECTORIES inside experiment repos. The discovery scanner
# skips these wholesale, and ``deploy_runtime`` populates the cluster's
# ``.hpc/`` with framework artifacts.
RESERVED_DIRS = frozenset({".hpc"})

# Derive the allowlist from the ClusterConfig Pydantic model so the
# boundary test, the prose manifest, and the validator all stay in sync
# from one SoT. v2 audit BUG-7V2-7 reported the three sources had
# drifted; the model is now the canonical surface.
from hpc_agent.infra.clusters import _allowed_cluster_keys  # noqa: E402

ALLOWED_CLUSTER_KEYS = _allowed_cluster_keys()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _diff_message(label: str, actual: set[str], expected: set[str]) -> str:
    """Format an actionable diff for failed allowlist comparisons."""
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    parts = [f"{label} drift detected. See {CONTRACT_DOC}."]
    if missing:
        parts.append(f"  Missing (declared in contract, absent from code): {missing}")
    if extra:
        parts.append(f"  Unexpected (present in code, absent from contract): {extra}")
    return "\n".join(parts)


def _walk_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root`` (recursively)."""
    return sorted(p for p in root.rglob("*.py") if p.is_file())


def _imported_top_level_modules(path: Path) -> set[str]:
    """Parse ``path`` with ``ast`` and return the set of imported top-level modules.

    Walks the entire tree (not just module-level) so nested ``import`` statements
    inside functions or conditionals are also caught.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"Failed to parse {path}: {exc}") from exc

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # Skip relative imports (node.level > 0); they cannot reach across
            # the boundary by construction.
            modules.add(node.module.split(".", 1)[0])
    return modules


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_public_api_matches_contract() -> None:
    """``hpc_agent.__all__`` must match the allowlist exactly."""
    actual = set(hpc_agent.__all__)
    expected = set(ALLOWED_EXPORTS)
    assert actual == expected, _diff_message("hpc_agent public API", actual, expected)


def test_reserved_filenames_match_contract() -> None:
    """``_SKIP_BASENAMES`` in discover.py must match the reserved-filenames allowlist."""
    actual = set(_SKIP_BASENAMES)
    expected = set(RESERVED_FILES)
    assert actual == expected, _diff_message(
        "Reserved filenames (_SKIP_BASENAMES)", actual, expected
    )


def test_reserved_dirs_match_contract() -> None:
    """``_SKIP_DIRS`` in discover.py must include the reserved-dirs allowlist."""
    # _SKIP_DIRS may include build/cache dirs (.git, __pycache__, .mypy_cache)
    # in addition to the framework-reserved .hpc; only require the latter is
    # present.
    actual = set(_SKIP_DIRS)
    missing = set(RESERVED_DIRS) - actual
    assert not missing, _diff_message("Reserved dirs (_SKIP_DIRS)", actual, set(RESERVED_DIRS))


def test_core_does_not_import_templates() -> None:
    """No file under ``hpc_agent/`` may import from ``templates``."""
    core_root = REPO_ROOT / "src" / "hpc_agent"
    offenders: list[str] = []
    for path in _walk_python_files(core_root):
        # Skip the templates directory itself — it lives inside the
        # hpc_agent package now (hpc_agent/execution/mapreduce/templates/) but
        # is data, not framework code.
        if "mapreduce/templates" in str(path):
            continue
        if "templates" in _imported_top_level_modules(path):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"hpc_agent/** must not import from templates/. See {CONTRACT_DOC}.\n"
        f"  Offending files: {offenders}"
    )


# Submodules of ``hpc_agent`` that are intentionally deployed alongside
# user executors on the cluster by ``deploy_runtime`` (see
# ``hpc_agent/infra/remote.py``). Templates may import from these because
# they are guaranteed to be present at execution time on the compute node.
# Keep this list narrow; new entries require a matching update to
# ``docs/reference/boundary-contract.md``.
RUNTIME_MODULES_ALLOWED_IN_TEMPLATES = frozenset(
    {
        "hpc_agent.execution.mapreduce.metrics_io",
        "hpc_agent.executor_cli",
    }
)


# The ``templates/scaffolds/`` files are a *different* boundary than the
# deployed runtime. Scaffolds are reference strategy/executor examples that
# the researcher copies into their experiment repo and runs host-side (the
# orchestrator ask/tell "propose" path), where a real ``hpc-agent`` install
# is present — they are not shipped verbatim to a python-only compute node by
# ``deploy_runtime``. So they carry the runtime allowlist PLUS the closed-loop
# campaign warm-start reader they legitimately need. Keep this list narrow;
# new entries require a matching update to ``docs/reference/boundary-contract.md``.
SCAFFOLD_MODULES_ALLOWED_IN_TEMPLATES = RUNTIME_MODULES_ALLOWED_IN_TEMPLATES | frozenset(
    {
        # ``prior_records`` / ``prior`` — read-only, oldest-first reduced
        # metrics for a campaign, used by the optuna/pbt strategy scaffolds
        # to warm-start their optimizer from prior iterations. Part of the
        # documented closed-loop campaign API (boundary-contract.md).
        "hpc_agent.execution.mapreduce.reduce.history",
    }
)


def _imported_dotted_modules(path: Path) -> set[str]:
    """Return the set of fully-qualified imported module names in ``path``.

    Like :func:`_imported_top_level_modules` but returns the full dotted name
    so callers can distinguish ``hpc_agent.execution.mapreduce.metrics_io`` (a deployed
    runtime module) from ``hpc_agent.state.runs`` (framework-internal).
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"Failed to parse {path}: {exc}") from exc

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _bad_core_imports(path: Path, allowed: frozenset[str]) -> list[str]:
    """Return sorted ``hpc_agent[.*]`` imports in ``path`` not on ``allowed``.

    The shared boundary-scan primitive: a file is an offender iff it imports
    the core package (bare ``hpc_agent`` or a ``hpc_agent.`` submodule) via a
    dotted name that is not an explicitly allowlisted runtime/scaffold module.
    """
    imported = _imported_dotted_modules(path)
    return sorted(
        m
        for m in imported
        if (m == "hpc_agent" or m.startswith("hpc_agent.")) and m not in allowed
    )


def test_templates_do_not_import_core() -> None:
    """No deployed file under ``hpc_agent/execution/mapreduce/templates/`` may import ``hpc_agent``.

    Exception: a small allowlist of runtime modules deployed alongside the
    executor by ``deploy_runtime`` (see
    ``RUNTIME_MODULES_ALLOWED_IN_TEMPLATES``). New entries require a matching
    update to ``docs/reference/boundary-contract.md``.

    Two boundaries are scanned, each against its own allowlist:

    * ``templates/runtime/`` — files ``deploy_runtime`` ships verbatim to a
      (possibly python-only) compute node, so they may only import modules
      ``deploy_runtime`` actually deploys (``RUNTIME_MODULES_ALLOWED_IN_TEMPLATES``).
    * ``templates/scaffolds/`` — reference strategy/executor examples the
      researcher copies into their experiment repo and runs host-side, where a
      real ``hpc-agent`` install is present. These carry the runtime allowlist
      PLUS the closed-loop warm-start reader (``reduce.history``) that the
      optuna/pbt strategy scaffolds legitimately need
      (``SCAFFOLD_MODULES_ALLOWED_IN_TEMPLATES``). Preferring an
      allowlist-with-rationale over rewriting the scaffolds keeps the worked
      examples faithful; the wider allowance is documented in
      ``docs/reference/boundary-contract.md``.

    Phase 2 (Option C): the per-scheduler ``cpu_array`` / ``gpu_array``
    scripts are no longer static files under ``runtime/{sge,slurm}/`` —
    they are *rendered* from the scheduler profile by ``render_script``.
    So the on-disk scan now covers the still-shipped ``runtime/common/``
    preambles, and the boundary is additionally enforced on the rendered
    array-script bodies (which, being bash, must likewise never reference
    the core package).
    """
    templates_root = (
        REPO_ROOT / "src" / "hpc_agent" / "execution" / "mapreduce" / "templates" / "runtime"
    )
    # ``common`` still ships as static files; ``sge`` / ``slurm`` array
    # scripts are now rendered (see below), so they have no on-disk dir.
    deployed_subdirs = ("common",)
    offenders: list[tuple[str, list[str]]] = []
    for subdir in deployed_subdirs:
        subdir_path = templates_root / subdir
        if not subdir_path.is_dir():
            raise AssertionError(
                f"expected deployed-runtime subdir {subdir_path} to exist; "
                f"the boundary scanner has nothing to check. See {CONTRACT_DOC}."
            )
        for path in _walk_python_files(subdir_path):
            bad = _bad_core_imports(path, RUNTIME_MODULES_ALLOWED_IN_TEMPLATES)
            if bad:
                offenders.append((str(path.relative_to(REPO_ROOT)), bad))

    # Scaffolds live one directory over and are scanned against their own
    # (wider) allowlist — see SCAFFOLD_MODULES_ALLOWED_IN_TEMPLATES.
    scaffolds_root = (
        REPO_ROOT
        / "src"
        / "hpc_agent"
        / "execution"
        / "mapreduce"
        / "templates"
        / "scaffolds"
    )
    if not scaffolds_root.is_dir():
        raise AssertionError(
            f"expected scaffolds dir {scaffolds_root} to exist; the boundary "
            f"scanner has nothing to check. See {CONTRACT_DOC}."
        )
    for path in _walk_python_files(scaffolds_root):
        bad = _bad_core_imports(path, SCAFFOLD_MODULES_ALLOWED_IN_TEMPLATES)
        if bad:
            offenders.append((str(path.relative_to(REPO_ROOT)), bad))

    assert not offenders, (
        f"templates/** must not import from hpc_agent (except deployed "
        f"runtime / allowlisted scaffold modules). See {CONTRACT_DOC}.\n"
        + "\n".join(f"  {p}: {mods}" for p, mods in offenders)
    )

    # Fire-path: prove the scanner actually flags a non-allowlisted core
    # import rather than passing vacuously. A probe file importing a
    # framework-internal (``hpc_agent.state.runs`` — the module reduce.history
    # itself pulls in, and precisely what the scaffold allowlist must NOT
    # cover) must be caught, while its allowlisted sibling import is ignored.
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe_boundary_violation.py"
        probe.write_text(
            "from hpc_agent.state.runs import find_existing_runs\n"
            "from hpc_agent.executor_cli import flag\n",
            encoding="utf-8",
        )
        caught = _bad_core_imports(probe, SCAFFOLD_MODULES_ALLOWED_IN_TEMPLATES)
        assert caught == ["hpc_agent.state.runs"], (
            "boundary scanner must flag a non-allowlisted core import "
            "(hpc_agent.state.runs) while ignoring the allowlisted "
            f"hpc_agent.executor_cli; got {caught!r}"
        )

    # The rendered array scripts are deployed too — assert they never
    # reference the core package (no ``import hpc_agent`` / ``from
    # hpc_agent`` smuggled into a rendered body).
    from hpc_agent.infra.backends import get_backend_class

    rendered_offenders: list[str] = []
    for sched in ("sge", "slurm"):
        backend_cls = get_backend_class(sched)
        for kind in ("cpu", "gpu"):
            body = backend_cls.render_script(kind=kind)
            if "import hpc_agent" in body or "from hpc_agent" in body:
                rendered_offenders.append(f"{sched}/{kind}_array")
    assert not rendered_offenders, (
        "rendered runtime array scripts must not reference the core "
        f"package. See {CONTRACT_DOC}. Offenders: {rendered_offenders}"
    )


def test_clusters_yaml_is_infra_only() -> None:
    """Each cluster entry in ``hpc_agent/config/clusters.yaml`` must use only infra keys."""
    clusters_path = REPO_ROOT / "src" / "hpc_agent" / "config" / "clusters.yaml"
    with clusters_path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    assert isinstance(data, dict), (
        "config/clusters.yaml must be a mapping of cluster_name -> config; "
        f"got {type(data).__name__}."
    )

    violations: list[str] = []
    for cluster_name, cluster_cfg in data.items():
        if not isinstance(cluster_cfg, dict):
            violations.append(
                f"  {cluster_name!r}: expected mapping, got {type(cluster_cfg).__name__}"
            )
            continue
        unexpected = sorted(set(cluster_cfg.keys()) - ALLOWED_CLUSTER_KEYS)
        if unexpected:
            violations.append(f"  {cluster_name!r}: unexpected keys {unexpected}")

    assert not violations, (
        f"config/clusters.yaml contains non-infra keys. See {CONTRACT_DOC}.\n"
        + "\n".join(violations)
    )
