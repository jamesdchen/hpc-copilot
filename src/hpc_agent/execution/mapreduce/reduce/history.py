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

from hpc_agent import errors
from hpc_agent.execution.mapreduce.reduce.metrics import reduce_metrics
from hpc_agent.state.runs import (
    find_existing_runs,
    read_run_sidecar,
    resolved_summary_artifact,
)

__all__ = [
    "find_sidecars_by_campaign",
    "parent_records",
    "prior",
    "prior_records",
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
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError, errors.HpcError):
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
    Each returned path is the directory containing the run's declared
    per-task summary file — ``sidecar.summary_artifact`` (F-J), defaulting to
    ``metrics.json`` when the run never declared one.
    """
    template = sidecar.get("result_dir_template")
    if not template:
        return []
    run_id = sidecar.get("run_id", "")
    task_count = int(sidecar.get("task_count") or 0)
    summary_name = resolved_summary_artifact(sidecar)
    base = Path(experiment_dir)

    found: list[Path] = []
    for task_id in range(task_count):
        # Substitute the framework-known names (honouring any format spec
        # such as ``{task_id:03d}``); any other ``{name}`` placeholder is
        # a per-task kwarg (seed, lr, …) whose value we cannot know
        # without tasks.py, so it becomes a glob wildcard and the
        # presence of the declared summary file narrows the over-match.
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
            if (p / summary_name).is_file():
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
        history.append(reduce_metrics(dirs, filename=resolved_summary_artifact(sidecar)))
    return history


def prior_records(
    experiment_dir: Path,
    campaign_id: str,
) -> list[dict[str, Any]]:
    """Rich per-iteration history for *campaign_id*, oldest-first.

    The strategy-agnostic companion to :func:`prior`. Where ``prior``
    returns only the reduced-metric dict per iteration (a deliberately
    minimal shape kept stable for existing ``tasks.py`` callers), this
    returns a record carrying everything a closed-loop strategy needs to
    decide the next iteration — without the framework interpreting any of
    it. Each record is::

        {
            "run_id": str,            # the iteration's run_id
            "campaign_id": str | None,
            "trial_tokens": list | None,  # opaque, round-tripped verbatim
            "trial_params": list | None,  # opaque per-task resolved params (provenance)
            "result_dirs": [str, ...],    # per-task output dirs (artifact lineage)
            "metrics": {...},             # reduce_metrics(result_dirs) — same as prior()
            "complete": bool,             # at least one result_dir has a metrics.json
        }

    The additions over ``prior`` are the seam (see
    ``docs/design/campaign-seam.md``):

    * ``result_dirs`` — **artifact lineage**. PBT clones a checkpoint from
      a survivor's dir; RL self-play reads the prior generation's replay
      buffer; active learning extends the prior label set. Far more general
      than a scalar objective.
    * ``trial_tokens`` — the opaque reconciliation token(s) a strategy
      round-tripped through ``resolve()`` (recorded on the sidecar by
      :func:`hpc_agent.state.runs.write_run_sidecar`). ``None`` for
      iterations submitted without one.
    * ``trial_params`` — the resolved per-task params recorded on the sidecar
      (the ``cmd_sha`` pre-image; ``None`` for iterations submitted before this
      was wired). Pairing ``(trial_params, metrics)`` per completed trial is
      the data a strategy needs to **warm-start** a fresh study from a prior
      corpus — but the framework hands back only the bytes; whether a prior
      trial is *relevant* (same data regime, comparable objective scale) is
      the strategy's call, not the framework's. Opaque, never interpreted.
    * ``complete`` — a filesystem-derived readiness flag (does any task
      have a ``metrics.json`` yet). This is NOT the authoritative
      lifecycle state — ``failed`` vs ``timeout`` vs ``abandoned`` live in
      the journal and are reported by ``hpc-agent status``. ``prior_records``
      stays a pure sidecar+filesystem read (no SSH, no journal) so it is
      safe to call from ``tasks.py`` at module load.

    The objective, if any, is just a key inside ``metrics`` — the framework
    never privileges one. A 1-ask-per-iteration optimizer can reconcile by
    oldest-first index (record ``i`` == trial ``i``); ``trial_tokens`` is
    for the concurrent / out-of-order case.
    """
    sidecars = find_sidecars_by_campaign(experiment_dir, campaign_id)
    records: list[dict[str, Any]] = []
    for sidecar in sidecars:
        dirs = result_dirs_for_sidecar(experiment_dir, sidecar)
        records.append(
            {
                "run_id": sidecar.get("run_id", ""),
                "campaign_id": sidecar.get("campaign_id"),
                "trial_tokens": sidecar.get("trial_tokens"),
                "trial_params": sidecar.get("trial_params"),
                "result_dirs": [str(d) for d in dirs],
                "metrics": reduce_metrics(dirs, filename=resolved_summary_artifact(sidecar)),
                "complete": bool(dirs),
            }
        )
    return records


def parent_records(
    experiment_dir: Path,
    parent_run_ids: list[str],
) -> list[dict[str, Any]]:
    """Per-parent records for an explicitly-declared dependency set.

    The lineage accessor of the DAG kernel
    (``docs/design/dag-kernel.md``): where :func:`prior_records` walks
    a campaign's iterations (an implicit linear order), this resolves the
    exact runs a child declared as ``parents`` on its submit spec —
    typically read by the child's ``tasks.py`` at module load to locate
    its inputs. Records carry the same keys as :func:`prior_records`
    (``run_id`` / ``campaign_id`` / ``trial_tokens`` / ``trial_params`` /
    ``result_dirs`` / ``metrics`` / ``complete``), in *parent_run_ids* order,
    one per distinct run_id (duplicates collapse — parents are a set).

    Opacity is the contract: the framework hands back paths and reduced
    metrics; what crosses the edge — which files, what format, whether
    the contents are usable — is the caller's to decide.

    Unlike ``prior_records`` (which tolerates unreadable sidecars because
    a campaign walk is best-effort), a missing parent sidecar raises
    :class:`FileNotFoundError`: the caller named this exact dependency,
    so its absence is an error to surface, not a record to skip. Pure
    sidecar+filesystem read (no SSH, no journal) — safe at ``tasks.py``
    module load; for *readiness* (did the parent finish?), compose the
    ``validate-parents-ready`` primitive, which consults the journal.
    """
    records: list[dict[str, Any]] = []
    for run_id in dict.fromkeys(parent_run_ids):
        sidecar = read_run_sidecar(experiment_dir, run_id)
        dirs = result_dirs_for_sidecar(experiment_dir, sidecar)
        records.append(
            {
                "run_id": sidecar.get("run_id", run_id),
                "campaign_id": sidecar.get("campaign_id"),
                "trial_tokens": sidecar.get("trial_tokens"),
                "trial_params": sidecar.get("trial_params"),
                "result_dirs": [str(d) for d in dirs],
                "metrics": reduce_metrics(dirs, filename=resolved_summary_artifact(sidecar)),
                "complete": bool(dirs),
            }
        )
    return records
