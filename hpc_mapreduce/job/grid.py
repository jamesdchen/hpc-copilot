"""Grid expansion and task manifest generation.

Pure computation — no I/O, only stdlib imports.

Manifest schema history:

* v1: initial format — ``schema_version``, ``total_tasks``, ``grid_size``,
  ``grid_keys``, and per-task ``cmd`` / ``result_dir`` / ``params``.
* v2: adds ``cmd_sha`` on every task (first 16 hex chars of the SHA-256 of
  the task's ``cmd`` string).  Provides a stable identifier for each task's
  command that observers (``/status``, status tools) can use to detect
  drift between the manifest and what actually ran.  The on-cluster
  dispatcher accepts both v1 and v2 for back-compat.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import shlex
import subprocess
from datetime import datetime, timezone
from math import prod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "expand_grid",
    "run_id",
    "build_task_manifest",
    "total_tasks",
    "attach_wave_map",
    "resolve_git_sha",
    "validate_result_dir_template",
    "validate_grid_keys",
]

# Placeholder names in ``result_dir`` templates that are resolved per-run
# (constant across every task in a manifest).  Grid-point keys vary per task.
_RUN_LEVEL_PLACEHOLDERS: frozenset[str] = frozenset({"run_id", "date", "git_sha"})

# Shape of a valid Python-style identifier.  Grid keys must match this so they
# render as well-formed CLI flags (``--{key} <value>``); ``result_dir`` template
# placeholders use the same shape so the two stay aligned.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Regex used to extract ``{name}`` placeholders from ``result_dir`` templates.
# Matches simple ``{identifier}`` — no format specs, no nested braces.  This is
# deliberately strict so users get a clear error for unsupported template
# features rather than silent behaviour.
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Version marker embedded in every manifest produced by ``build_task_manifest``.
# Bump whenever the manifest shape changes in a way that on-cluster dispatch
# code must reject.  The dispatcher (hpc_mapreduce/map/dispatch.py) hardcodes
# its own expected value as a literal; keep the two in sync.
MANIFEST_SCHEMA_VERSION = 2


def expand_grid(grid: dict[str, list]) -> list[dict[str, str]]:
    """Cartesian product of all grid values, preserving key insertion order."""
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    return [
        {k: str(v) for k, v in zip(keys, combo, strict=True)}
        for combo in itertools.product(*values)
    ]


def run_id(params: dict[str, str]) -> str:
    """Deterministic string ID from param values, joined by ``_``."""
    raw = "_".join(params.values())
    return re.sub(r"[^a-zA-Z0-9.\-]", "_", raw)


def resolve_git_sha(repo_path: str | Path | None = None) -> str:
    """Return the short (7-char) git SHA of ``HEAD`` in *repo_path*.

    Falls back to the literal string ``"nogit"`` when ``git`` is unavailable,
    the path is not a git repository, or the subprocess fails for any other
    reason.  This is intentionally permissive — ``result_dir`` templating
    should never hard-fail because the experiment lives outside a git repo.

    Parameters
    ----------
    repo_path:
        Directory in which to run ``git rev-parse HEAD``.  Defaults to the
        current working directory.
    """
    cwd = str(repo_path) if repo_path is not None else None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return "nogit"
    if result.returncode != 0:
        return "nogit"
    sha = result.stdout.strip()
    if not sha:
        return "nogit"
    return sha[:7]


def _extract_placeholders(template: str) -> list[str]:
    """Return the names of every ``{name}`` placeholder in *template*.

    Preserves source order and keeps duplicates; callers that need a unique
    set can wrap the result in :class:`set`.
    """
    return _PLACEHOLDER_RE.findall(template)


def validate_grid_keys(grid: dict[str, list]) -> None:
    """Validate that every key in *grid* is a well-formed identifier.

    Grid keys become CLI flags (``--{key} <value>``) in each task's command,
    so a key like ``"foo-bar"`` or ``"123key"`` would produce a malformed flag
    that fails only at runtime on the cluster.  This check rejects such keys
    at manifest-build time, where the error is actionable.

    An empty grid is a no-op: there are no keys to check.

    Raises
    ------
    ValueError
        If any key fails to match ``^[A-Za-z_][A-Za-z0-9_]*$``.  The error
        message lists every offending key and shows the required pattern.
    """
    invalid = [k for k in grid if not _IDENTIFIER_RE.match(k)]
    if invalid:
        raise ValueError(
            f"grid contains invalid key(s) {invalid!r}; grid keys must match "
            f"the regex ^[A-Za-z_][A-Za-z0-9_]*$ so they render as well-formed "
            f"CLI flags (--key <value>)."
        )


def validate_result_dir_template(
    template: str,
    grid: dict[str, list],
) -> None:
    """Validate that every ``{name}`` in *template* can be resolved per-task.

    A placeholder is valid iff it is one of the run-level names
    (``run_id``, ``date``, ``git_sha``) or a grid key that appears in every
    grid point.  Since :func:`expand_grid` produces grid points with keys
    equal to ``grid.keys()``, this reduces to membership in ``grid``.

    Raises
    ------
    ValueError
        If any referenced placeholder is missing from both the run-level
        set and *grid*.  The error message lists the valid names and the
        missing one.
    """
    referenced = _extract_placeholders(template)
    valid_grid_keys = set(grid.keys())
    missing: list[str] = []
    for name in referenced:
        if name in _RUN_LEVEL_PLACEHOLDERS:
            continue
        if name in valid_grid_keys:
            continue
        if name not in missing:
            missing.append(name)
    if missing:
        valid = sorted(_RUN_LEVEL_PLACEHOLDERS | valid_grid_keys)
        raise ValueError(
            f"result_dir template {template!r} references unknown "
            f"placeholder(s) {missing!r}. Valid placeholders are: {valid}"
        )


# Runtime profiles accepted by ``build_task_manifest``. Currently only
# ``"uv"`` (which prefixes every task ``cmd`` with ``"uv run "`` and
# expects the cluster-side template's ``uv sync`` preamble to fire).
# Reserve the field for future entries (``"pixi"``, …); the dispatcher
# itself doesn't consult ``runtime`` — it just shells out the cmd.
_SUPPORTED_RUNTIMES: frozenset[str] = frozenset({"uv"})


def build_task_manifest(
    run_cmd: str,
    grid: dict[str, list],
    result_dir_template: str,
    max_tasks: int | None = 10_000,
    repo_path: str | Path | None = None,
    runtime: str | None = None,
) -> dict:
    """Build a task manifest from a grid.

    Parameters
    ----------
    run_cmd:
        Base command string (e.g. ``"python3 -m my_experiment.train"``).
    grid:
        ``param_name -> list_of_values``.
    result_dir_template:
        Template string for the per-task ``result_dir``.  Supports the
        run-level placeholders ``{run_id}`` (deterministic ID derived from
        a task's grid-point values), ``{date}`` (UTC ``YYYY-MM-DD`` at
        manifest-build time), ``{git_sha}`` (7-char ``HEAD`` SHA of the
        experiment repo, or ``"nogit"`` on failure), plus any grid key
        (e.g. ``{model}``, ``{dataset}``) present in *grid*.
    max_tasks:
        Pre-flight ceiling on the number of tasks that will be materialized.
        If the computed total exceeds this value, a :class:`ValueError` is
        raised before any tasks are built.  Pass ``None`` to disable the
        check.  Defaults to ``10_000`` — large enough for typical grids but
        small enough to catch accidental explosion.
    repo_path:
        Directory used to resolve ``{git_sha}``.  Defaults to the current
        working directory.
    runtime:
        Optional runtime profile that the cluster-side dispatcher will
        execute each task under. ``"uv"`` prefixes every task ``cmd`` with
        ``"uv run "`` so the on-cluster ``uv``-managed venv is used —
        honors MARs's #1 invariant ("ALWAYS ``uv run`` … NEVER ``pip``").
        ``None`` (the default) leaves task commands untouched.
        The chosen runtime is also recorded at the manifest top level so
        observers can confirm it; the dispatcher itself does not consult
        the field.

    Raises
    ------
    ValueError
        If ``max_tasks`` is not ``None`` and the computed total exceeds it,
        if ``result_dir_template`` references an unknown placeholder, or
        if ``runtime`` is set to an unsupported value.
    """
    validate_grid_keys(grid)
    validate_result_dir_template(result_dir_template, grid)

    if runtime is not None and runtime not in _SUPPORTED_RUNTIMES:
        raise ValueError(
            f"runtime={runtime!r} is not supported; "
            f"expected one of {sorted(_SUPPORTED_RUNTIMES)} or None."
        )

    if max_tasks is not None:
        projected = total_tasks(grid)
        if projected > max_tasks:
            raise ValueError(
                f"build_task_manifest would produce {projected} tasks "
                f"(> max_tasks={max_tasks}). Pass max_tasks=None to disable "
                f"or raise the threshold."
            )

    points = expand_grid(grid)

    # Resolve run-level placeholders once — they are constant for every
    # task in this manifest.
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    git_sha = resolve_git_sha(repo_path)

    cmd_prefix = "uv run " if runtime == "uv" else ""

    tasks: dict[str, dict] = {}
    for task_idx, params in enumerate(points):
        parts = [run_cmd]
        for k, v in params.items():
            parts.append(f"--{k} {shlex.quote(str(v))}")

        format_kwargs: dict[str, str] = {
            "run_id": run_id(params),
            "date": run_date,
            "git_sha": git_sha,
            **params,
        }
        entry: dict = {
            "cmd": cmd_prefix + " ".join(parts),
            "result_dir": result_dir_template.format(**format_kwargs),
            "params": dict(params),
        }
        entry["cmd_sha"] = hashlib.sha256(entry["cmd"].encode()).hexdigest()[:16]

        tasks[str(task_idx)] = entry

    n_tasks = len(tasks)

    manifest: dict = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "total_tasks": n_tasks,
        "grid_size": len(points),
        "grid_keys": list(grid.keys()),
        "tasks": tasks,
    }
    if runtime is not None:
        manifest["runtime"] = runtime
    return manifest


def attach_wave_map(
    manifest: dict,
    wave_map: dict[int, list[int]],
) -> dict:
    """Return a *new* manifest dict with ``wave_map`` embedded.

    Keys in the wave map are converted to strings (JSON compatibility),
    and task IDs within each wave are also converted to strings so they
    match the string-keyed ``tasks`` dict in the manifest.

    The original *manifest* dict is **not** mutated.
    """
    # Convert int keys/values to strings for JSON round-tripping
    str_map: dict[str, list[str]] = {
        str(wave): [str(tid) for tid in tids] for wave, tids in wave_map.items()
    }
    return {**manifest, "wave_map": str_map}


def total_tasks(grid: dict[str, list]) -> int:
    """Product of all grid dimension sizes."""
    return prod(len(v) for v in grid.values()) if grid else 1
