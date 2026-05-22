"""Per-campaign history accessor.

Closed-loop campaigns tag every ``/submit`` with a ``campaign_id`` (a
first-class field on the v2 sidecar). ``prior(experiment_dir, campaign_id)``
walks every matching sidecar oldest-first, runs :func:`reduce_metrics`
on each iteration's result directories, and returns the per-iteration
reduced-metric dicts. The user's ``tasks.py`` calls this at module load
to get the history of finished iterations and decide what to run next.

Result-directory resolution avoids importing ``.hpc/tasks.py`` (which
is the calling module in the closed-loop case — re-importing it from
inside its own load would either deadlock or recurse). Instead, the
sidecar's ``result_dir_template`` is treated as a glob pattern: known
substitutions (``{run_id}``, ``{task_id}``) are filled in, remaining
``{var}`` placeholders are replaced with ``*``, and the filesystem is
walked for matching directories that contain a ``metrics.json``. This is
robust across template shapes and never touches user code.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Any

from hpc_agent.mapreduce.reduce.metrics import reduce_metrics
from hpc_agent.state.runs import find_existing_runs, read_run_sidecar

__all__ = [
    "find_sidecars_by_campaign",
    "prior",
    "result_dirs_for_sidecar",
]


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _read_sidecar_safe(path: Path) -> dict[str, Any] | None:
    """Read a sidecar via the canonical hardened reader.

    The sidecar's directory layout is ``<experiment>/.hpc/runs/<run_id>.json``;
    we recover the experiment dir and run_id from *path* and route the
    read through :func:`read_run_sidecar` so the returned dict has the
    full backfilled v2 shape (``wave_map``/``task_count``/...). Missing
    or malformed files yield ``None``.
    """
    try:
        experiment_dir = path.parent.parent.parent
        run_id = path.stem
        return read_run_sidecar(experiment_dir, run_id)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None


def find_sidecars_by_campaign(
    experiment_dir: Path,
    campaign_id: str,
) -> list[dict[str, Any]]:
    """Return every sidecar tagged with *campaign_id*, oldest-first.

    Sidecars are filtered by the v2 ``campaign_id`` field. Sidecars
    written before campaign tagging existed (no ``campaign_id`` key, or
    ``campaign_id`` is ``None``) are skipped.

    Order: oldest-first by sidecar mtime, suitable for treating as
    iteration history.
    """
    if not campaign_id:
        return []
    matched: list[tuple[float, str, dict[str, Any]]] = []
    for path in find_existing_runs(experiment_dir):  # newest-first by mtime
        data = _read_sidecar_safe(path)
        if data is None:
            continue
        if data.get("campaign_id") != campaign_id:
            continue
        # path.stat() can race with deletion between the safe-read above
        # and this call; skip the entry rather than letting the
        # FileNotFoundError propagate through every caller of
        # find_sidecars_by_campaign.
        try:
            mtime = path.stat().st_mtime
        except (FileNotFoundError, OSError):
            continue
        matched.append((mtime, path.stem, data))
    # Secondary key: run_id (path.stem) is ``YYYYMMDD-HHMMSS-<sha>`` — its ISO
    # prefix is monotonic, so it's a stable tiebreaker when two sidecars share
    # the same coarse-FS mtime.
    matched.sort(key=lambda item: (item[0], item[1]))  # oldest-first
    return [data for _, _, data in matched]


def result_dirs_for_sidecar(
    experiment_dir: Path,
    sidecar: dict[str, Any],
) -> list[Path]:
    """Resolve every per-task ``result_dir`` for *sidecar* without
    importing ``tasks.py``.

    Substitutes ``{run_id}`` and ``{task_id}`` in the sidecar's
    ``result_dir_template`` from sidecar fields; replaces every other
    ``{name}`` placeholder with a glob ``*`` and walks the filesystem.
    Each returned path is the directory containing a ``metrics.json``.
    """
    template = sidecar.get("result_dir_template")
    if not template:
        return []
    run_id = sidecar.get("run_id", "")
    task_count = int(sidecar.get("task_count") or 0)
    base = Path(experiment_dir)

    found: list[Path] = []
    for task_id in range(task_count):
        # Substitute the framework-known names (honouring any format spec
        # such as ``{task_id:03d}``); any other ``{name}`` placeholder is
        # a per-task kwarg (seed, lr, …) whose value we cannot know
        # without tasks.py, so it becomes a glob wildcard and the
        # presence of metrics.json narrows the over-match.
        def _expand(m: re.Match[str], _rid: str = run_id, _tid: int = task_id) -> str:
            name, spec = m.group(1), m.group(2)
            if name == "run_id":
                return format(_rid, spec) if spec else _rid
            if name == "task_id":
                return format(_tid, spec) if spec else str(_tid)
            return "*"

        pattern = _PLACEHOLDER_RE.sub(_expand, template)
        candidate = base / pattern if not Path(pattern).is_absolute() else Path(pattern)
        for hit in glob.glob(str(candidate)):
            p = Path(hit)
            if (p / "metrics.json").is_file():
                found.append(p)
    # De-duplicate while preserving order (one task may match multiple
    # globs when wildcards overlap, e.g. nested dirs).
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def prior(
    experiment_dir: Path,
    campaign_id: str,
) -> list[dict[str, Any]]:
    """Return per-iteration reduced metrics for *campaign_id*, oldest-first.

    For each sidecar tagged with *campaign_id*:
      1. Resolve per-task ``result_dir``\\ s via
         :func:`result_dirs_for_sidecar`.
      2. Run :func:`reduce_metrics` over them.
      3. Append the resulting dict to the history.

    Iterations whose result directories don't exist yet (still
    in-flight) contribute an empty dict; the user's ``tasks.py`` can
    filter these by checking for ``not entry`` if it cares.
    """
    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    history: list[dict[str, Any]] = []
    for sidecar in sidecars:
        dirs = result_dirs_for_sidecar(experiment_dir, sidecar)
        history.append(reduce_metrics(dirs))
    return history
