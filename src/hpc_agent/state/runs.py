"""Per-run sidecars and ``cmd_sha`` computation.

Each ``/submit`` writes a JSON sidecar to
``$EXPERIMENT/.hpc/runs/<run_id>.json`` carrying audit-trail metadata for
the run: identity, executor command, result-dir template, materialized
task count, and the wave map computed by the throughput optimizer.

The user's per-task definition lives in ``$EXPERIMENT/.hpc/tasks.py``
exposing ``total()`` and ``resolve(task_id)``. ``cmd_sha`` is derived from
materializing ``[resolve(i) for i in range(total())]`` and hashing the
sorted-keys JSON line-joined form — every task's full kwargs dict
contributes to the digest, so any change to ``tasks.py`` that affects
task content also changes the run's identity.
"""

from __future__ import annotations

import json
import os
import re
import warnings
from collections import OrderedDict
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.state.run_sha import compute_cmd_sha, compute_tasks_py_sha

__all__ = [
    "MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION",
    "compute_cmd_sha",
    "compute_tasks_py_sha",
    "find_existing_runs",
    "find_run_by_cmd_sha",
    "is_orphan_sidecar",
    "prune_old_runs",
    "prune_orphan_sidecars",
    "read_run_sidecar",
    "run_sidecar_path",
    "update_run_sidecar_job_ids",
    "write_run_sidecar",
]

# Maximum number of per-experiment run sidecars retained on disk.
# Oldest-first eviction by mtime. Module-level so callers (and tests) can
# monkeypatch. Default raised from 10 to 500 (long campaigns); ``HPC_MAX_RUNS``
# env var overrides at module load.
MAX_RUNS: int = int(os.environ.get("HPC_MAX_RUNS", "500"))

# Sidecar JSON schema version. v2 adds first-class config-snapshot fields
# (resources/env/env_group/constraints/cluster/profile/campaign_id/...) so
# every successful submit captures the full config it ran under and
# subsequent commands have no need for a separate experiment-config file.
# v1 sidecars on disk continue to load via ``read_run_sidecar`` backfill.
SIDECAR_SCHEMA_VERSION: int = 2

# A run_id is a timestamp-prefixed identifier produced by the slash command
# layer. Format: ``YYYYMMDD-HHMMSS-<short_sha>``. We only validate loosely
# — anything filesystem-safe that doesn't contain a path separator works.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _runs_dir(experiment_dir: Path) -> Path:
    """Deprecated alias for ``RepoLayout(experiment_dir).runs``.

    Kept as an internal forwarder for module-internal callers. Note:
    unlike :attr:`RepoLayout.runs` this does NOT mkdir the directory —
    callers (``find_existing_runs``, ``prune_old_runs``) already handle
    the absent-directory case by returning ``[]``.
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    return RepoLayout(experiment_dir).hpc / "runs"


def run_sidecar_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the canonical path to a run's sidecar (file may not exist).

    Forwarder for ``RepoLayout(experiment_dir).run_sidecar(run_id)`` plus
    the run_id format validation that ``RepoLayout`` deliberately omits
    (``RepoLayout`` is purely about path arithmetic; the format check is
    a submit-time guard kept here).
    """
    from hpc_agent._kernel.contract.layout import RepoLayout

    if not _RUN_ID_RE.fullmatch(run_id):
        raise errors.SpecInvalid(f"invalid run_id: {run_id!r}")
    return RepoLayout(experiment_dir).run_sidecar(run_id)


# ``compute_cmd_sha`` and ``compute_tasks_py_sha`` live in
# :mod:`hpc_agent.state.run_sha` so this module can stay focused on the
# sidecar lifecycle. The names re-export above via the top-level
# ``from hpc_agent.state.run_sha import ...``; ``__all__`` already lists
# both names so star-imports keep working unchanged.


# v2 first-class config-snapshot fields. All optional; absent keys are
# omitted from the written sidecar and backfilled to ``None`` (or the
# empty container) on read.
_V2_CONFIG_FIELDS: tuple[str, ...] = (
    "cluster",  # str — cluster key from clusters.yaml
    "profile",  # str — label distinguishing this submission shape
    "campaign_id",  # str — closed-loop campaign tag
    "project",  # str — short project name (paths, logs)
    "remote_path",  # str — absolute path on the remote cluster
    "resources",  # dict — cpus/mem/walltime/gpus/gpu_type
    "env",  # dict — modules/conda_env
    "env_group",  # str — clusters.yaml env_group key
    "constraints",  # dict — overrides on clusters.yaml constraints
    "gpu_fallback",  # list — ordered GPU types to try
    "max_retries",  # int — auto-resubmission cap
    "runtime",  # str — "uv" or omitted
    "auto_retry",  # dict — per-category retry policy
    "aggregate_defaults",  # dict — require_outputs/expect_output/aggregate_cmd
    "results",  # dict — declared result-file schema (see _RESULTS_BLOCK_KEYS)
)

# Keys recognised inside the optional ``results`` sidecar block. Declaring
# them lets the post-aggregate column gate
# (``check_result_columns`` / ``verify-aggregation-complete``) verify each
# task's result file deterministically — no LLM. All optional; an absent
# ``results`` block (or empty fields) means the gate is a clean no-op.
#
#   summary_pattern  : str        — glob for the per-task result file
#   expected_columns : list[str]  — column names every result CSV must carry
#   metric_column    : str | None — column that must hold a non-NaN value
_RESULTS_BLOCK_KEYS: tuple[str, ...] = (
    "summary_pattern",
    "expected_columns",
    "metric_column",
)

# Backfill defaults for v1→v2 read. Containers default to empty so callers
# can use ``or {}`` patterns; scalars default to ``None``.
_V2_BACKFILL_DEFAULTS: dict[str, Any] = {
    "cluster": None,
    "profile": None,
    "campaign_id": None,
    "project": None,
    "remote_path": None,
    "resources": None,
    "env": None,
    "env_group": None,
    "constraints": None,
    "gpu_fallback": None,
    "max_retries": None,
    "runtime": None,
    "auto_retry": None,
    "aggregate_defaults": None,
    "results": None,
    # job_ids lands AFTER qsub via :func:`update_run_sidecar_job_ids`. A
    # sidecar without job_ids (and without a journal record) is the half-
    # baked signal :func:`is_orphan_sidecar` keys on. Default `None` (not
    # `[]`) so the backfill stays distinguishable from a deliberate
    # "no job ids yet" empty list at write time.
    "job_ids": None,
}

# Hardened return-shape defaults. ``read_run_sidecar`` always fills these
# so callers can read ``data["wave_map"]`` etc. without a presence check
# regardless of which sidecar version wrote the file.
_HARDENED_DEFAULTS: dict[str, Any] = {
    "wave_map": dict,  # callable factory — produces a fresh empty dict
    "task_count": int,  # 0
    "result_dir_template": str,  # ""
}

# Module-level dedup for the version-mismatch warning. Keyed on
# (run_id, sidecar_version) so a long-running monitor that re-reads the
# same sidecar 1000 times only emits one warning per (run, version).
#
# Bounded LRU: a long-running monitor that watches a 10k-task campaign
# would otherwise accumulate a 10k-entry set with no eviction. The
# warning is best-effort dedup, not a correctness contract — falling
# off the LRU after _WARNED_VERSION_MISMATCH_CAP entries just means an
# old (run, version) pair could re-warn, which is fine.
_WARNED_VERSION_MISMATCH_CAP: int = 1024
_warned_version_mismatch: OrderedDict[tuple[str, str], None] = OrderedDict()


def _maybe_derive_wave_map(experiment_dir: Path, *, task_count: int) -> dict[str, list[int]] | None:
    """Best-effort axes-driven wave_map derivation. Returns None on any miss.

    Silent on the happy path; emits a :class:`UserWarning` only when
    ``axes.yaml`` is present with a full enumeration but the cartesian
    product of axis sizes disagrees with *task_count* — that's a sign
    of a misconfigured deploy and the user wants to hear about it.
    """
    import jsonschema
    import yaml

    try:
        from hpc_agent.state.axes import (
            compute_wave_map,
            pick_array_axis,
            read_axes,
        )
    except ImportError:
        return None

    try:
        config = read_axes(experiment_dir)
    except (jsonschema.ValidationError, yaml.YAMLError, ValueError, OSError):
        return None
    if config is None or not config.get("axes"):
        return None

    sizes = [int(a["size"]) for a in config["axes"]]
    product = 1
    for s in sizes:
        product *= s
    if product != task_count:
        warnings.warn(
            f"axes.yaml product ({product}) != task_count ({task_count}); "
            "skipping auto-derived wave_map. Re-run /hpc-axes-init or pass "
            "wave_map explicitly.",
            UserWarning,
            stacklevel=3,
        )
        return None

    picked_name, picker_reason = pick_array_axis(experiment_dir)
    if picked_name is None:
        # Picker couldn't choose (no homogeneous_axes hint, no qualifying
        # axis after CV scoring, etc). axes.yaml HAD a multi-axis
        # enumeration; degrading to single-wave aggregation silently
        # surprises the user when they later see per-wave combiner
        # output collapse. Warn loudly so the operator knows what
        # changed; the degraded path still works (downstream treats
        # a missing wave_map as "single implicit wave-0").
        warnings.warn(
            f"axes.yaml declared multi-axis enumeration but pick_array_axis "
            f"returned None ({picker_reason!r}); sidecar will lack wave_map "
            "and downstream auto-combine-waves will degrade to single-wave "
            "aggregation. Add homogeneous_axes to axes.yaml or pass "
            "wave_map explicitly to /submit to enforce a specific shape.",
            UserWarning,
            stacklevel=3,
        )
        return None
    try:
        derived = compute_wave_map(experiment_dir, picked_axis=picked_name)
    except (ValueError, jsonschema.ValidationError):
        return None
    return {str(k): list(v) for k, v in derived.items()}


def write_run_sidecar(
    experiment_dir: Path,
    *,
    run_id: str,
    cmd_sha: str,
    hpc_agent_version: str,
    submitted_at: str,
    executor: str,
    result_dir_template: str,
    task_count: int,
    tasks_py_sha: str,
    wave_map: dict[str, list[int]] | None = None,
    extra: dict[str, Any] | None = None,
    # ----- v2 config-snapshot fields (all optional) -----
    cluster: str | None = None,
    profile: str | None = None,
    campaign_id: str | None = None,
    project: str | None = None,
    remote_path: str | None = None,
    resources: dict[str, Any] | None = None,
    env: dict[str, Any] | None = None,
    env_group: str | None = None,
    constraints: dict[str, Any] | None = None,
    gpu_fallback: list[str] | None = None,
    max_retries: int | None = None,
    runtime: str | None = None,
    auto_retry: dict[str, Any] | None = None,
    aggregate_defaults: dict[str, Any] | None = None,
    results: dict[str, Any] | None = None,
    job_ids: list[str] | None = None,
) -> Path:
    """Write the per-run sidecar JSON. Returns the path written.

    *wave_map* is optional: when present it carries the throughput
    optimizer's task-id-to-wave assignment (str-keyed for JSON
    round-tripping). *extra* is a free-form pocket for callers that want
    to record additional run-scoped metadata without bumping the schema.

    The remaining kwargs (cluster, profile, resources, …) are the v2
    config-snapshot fields. They are all optional at the call site but
    every successful ``/submit`` should populate the ones that apply, so
    subsequent commands (``/aggregate``, ``/status``, ``/resubmit``) can
    rebuild full context without consulting any external config file.

    *results* is an optional declared-result-file schema block — recognised
    keys are ``summary_pattern`` (glob), ``expected_columns`` (list[str]),
    and ``metric_column`` (str). When present it lets the post-aggregate
    column gate verify each task's result file deterministically; when
    absent the gate is a clean no-op.

    Auto-derived ``wave_map``: when *wave_map* is None and
    ``<experiment>/.hpc/axes.yaml`` carries a full ``axes`` enumeration,
    the picker (warm-then-cold) selects an array axis and
    :func:`compute_wave_map` derives the assignment. The cartesian
    product of axis sizes must equal *task_count*; on mismatch we emit
    a :class:`UserWarning` and fall through (sidecar is still written
    without ``wave_map``). This integration is silent on the happy
    path — callers that already pass *wave_map* are unaffected.
    """
    sidecar: dict[str, Any] = {
        "sidecar_schema_version": SIDECAR_SCHEMA_VERSION,
        "run_id": run_id,
        "cmd_sha": cmd_sha,
        "hpc_agent_version": hpc_agent_version,
        "submitted_at": submitted_at,
        "executor": executor,
        "result_dir_template": result_dir_template,
        "task_count": int(task_count),
        "tasks_py_sha": tasks_py_sha,
    }
    if wave_map is None:
        wave_map = _maybe_derive_wave_map(experiment_dir, task_count=int(task_count))
    if wave_map is not None:
        sidecar["wave_map"] = {str(k): list(v) for k, v in wave_map.items()}
    if extra:
        sidecar["extra"] = extra
    # v2 fields — only write keys with non-None values to keep sidecars compact.
    v2_values: dict[str, Any] = {
        "cluster": cluster,
        "profile": profile,
        "campaign_id": campaign_id,
        "project": project,
        "remote_path": remote_path,
        "resources": resources,
        "env": env,
        "env_group": env_group,
        "constraints": constraints,
        "gpu_fallback": gpu_fallback,
        "max_retries": max_retries,
        "runtime": runtime,
        "auto_retry": auto_retry,
        "aggregate_defaults": aggregate_defaults,
        "results": results,
        "job_ids": list(job_ids) if job_ids is not None else None,
    }
    for k, v in v2_values.items():
        if v is not None:
            sidecar[k] = v
    target = run_sidecar_path(experiment_dir, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically (tempfile + flush + fsync + rename) so a crash
    # mid-write leaves either the previous sidecar or the new one — never
    # a 0-byte or partial-JSON file.
    _atomic_write_json(target, sidecar)
    prune_old_runs(experiment_dir, keep=MAX_RUNS)
    return target


def _atomic_write_json(target: Path, payload: dict) -> None:
    """Forwarder to :func:`hpc_agent.infra.io.atomic_write_json`.

    The canonical helper handles tempfile creation, fsync, replace, and
    parent-dir fsync. Kept as a local alias so the existing call sites
    in this module don't need to change.
    """
    from hpc_agent.infra.io import atomic_write_json

    atomic_write_json(target, payload)


def read_run_sidecar(experiment_dir: Path, run_id: str) -> dict:
    """Load and return a run's sidecar dict.

    v1 sidecars are backfilled with v2 config-snapshot keys defaulting to
    ``None`` so callers can rely on the v2 shape regardless of when the
    sidecar was written.

    Hardened return shape — the dict is guaranteed to contain:

    - ``wave_map: dict[str, list[int]]`` — empty dict when unset
    - ``task_count: int`` — ``0`` when unset
    - ``result_dir_template: str`` — empty string when unset

    Callers can therefore read these keys directly without falling back
    to ``.get(...)`` with a default, regardless of sidecar version.

    Raises
    ------
    FileNotFoundError
        If no sidecar exists for *run_id*.
    """
    target = run_sidecar_path(experiment_dir, run_id)
    if not target.is_file():
        raise FileNotFoundError(f"run sidecar not found: {target}")
    data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    # B8: route the schema-version check through the cross-domain
    # manifest in hpc_agent._kernel.extension.version. Strict here (raises) because
    # the sidecar shape is critical to the dispatcher / aggregator —
    # mis-reading a future v3 with a v2 reader would silently corrupt
    # the run. Writer keeps SIDECAR_SCHEMA_VERSION as the value emitted.
    sv = data.get("sidecar_schema_version")
    if isinstance(sv, int):
        from hpc_agent._kernel.extension.version import compatibility_check as _compat

        _compat("sidecar", sv)
    # Backfill missing v2 fields so callers see a uniform shape.
    for k, default in _V2_BACKFILL_DEFAULTS.items():
        data.setdefault(k, default)
    # Hardened defaults — callers (monitor_flow, aggregate_flow,
    # reduce.status, reduce.history) used to read these keys with raw
    # json.loads + .get(...) and silently miss them on v1 sidecars or
    # sidecars that omitted wave_map. Pin the shape here so the bug
    # cannot recur.
    for k, factory in _HARDENED_DEFAULTS.items():
        existing = data.get(k)
        if k == "wave_map":
            if not isinstance(existing, dict):
                data[k] = factory()
        elif k == "task_count":
            try:
                data[k] = int(existing or 0)
            except (TypeError, ValueError):
                data[k] = 0
        elif k == "result_dir_template":
            data[k] = existing if isinstance(existing, str) else ""

    # A10: surface a sidecar-vs-package version mismatch once per
    # (run_id, sidecar_version). ``write_run_sidecar`` records
    # ``hpc_agent_version`` from the writer's installed package; readers
    # compare against their own ``hpc_agent.__version__``. Pure
    # observability — the read still succeeds; the warning lets us find
    # old sidecars in the wild.
    sidecar_version = data.get("hpc_agent_version")
    if isinstance(sidecar_version, str) and sidecar_version:
        _pkg_version: str | None
        try:
            from hpc_agent import __version__ as _pkg_version
        except Exception:  # noqa: BLE001 — never let a circular fail the read
            _pkg_version = None
        if _pkg_version and sidecar_version != _pkg_version:
            key = (run_id, sidecar_version)
            if key not in _warned_version_mismatch:
                _warned_version_mismatch[key] = None
                # Evict oldest entries past the cap. The set was
                # previously unbounded; a monitor watching a 10k-task
                # campaign would accumulate a 10k-entry set with no
                # eviction over a multi-day run.
                while len(_warned_version_mismatch) > _WARNED_VERSION_MISMATCH_CAP:
                    _warned_version_mismatch.popitem(last=False)
                warnings.warn(
                    f"sidecar {run_id!r} written by hpc-agent "
                    f"{sidecar_version!r} but reader is {_pkg_version!r}; "
                    "shape backfills apply but consider re-submitting if "
                    "behaviour drifts.",
                    stacklevel=2,
                )
            else:
                # Touch on hit so frequently-seen keys don't get
                # evicted while rarely-seen ones stay.
                _warned_version_mismatch.move_to_end(key)
    return data


def find_existing_runs(experiment_dir: Path) -> list[Path]:
    """Return every ``.hpc/runs/<id>.json`` file, newest-first by mtime."""
    runs = _runs_dir(experiment_dir)
    if not runs.exists():
        return []
    # Secondary key: run_id (path.stem) is ``YYYYMMDD-HHMMSS-<sha>`` — its ISO
    # prefix is monotonic, so it's a stable tiebreaker when two sidecars share
    # the same coarse-FS mtime (e.g. seconds-resolution filesystems).
    # stat() is guarded: a concurrent prune can unlink a sidecar between
    # iterdir() and the sort, which would otherwise raise FileNotFoundError.
    keyed: list[tuple[float, str, Path]] = []
    for p in runs.iterdir():
        if not (p.is_file() and p.suffix == ".json"):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        keyed.append((mtime, p.stem, p))
    keyed.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [p for _, _, p in keyed]


def find_run_by_cmd_sha(
    experiment_dir: Path, cmd_sha: str, *, skip_orphans: bool = False
) -> Path | None:
    """Return the newest sidecar matching *cmd_sha*, or ``None`` if absent.

    Compares the full cmd_sha string. Iterates newest-first so a fresh
    resume detection picks the most recent matching run.

    *skip_orphans* (default False) preserves the journal-wipe recovery
    contract: a sidecar with no journal record is the canonical signal
    that the journal at ``~/.claude/hpc/<repo_hash>/`` was wiped (machine
    swap, rm -rf) and ``submit_and_record`` should reconstruct from the
    sidecar instead of re-qsub'ing a job the cluster already has running.
    Pass ``skip_orphans=True`` only after a known-failed batch where the
    sidecars without journal records are guaranteed to be half-baked
    (e.g. inside the prune primitive) — see :func:`prune_orphan_sidecars`.
    """
    if not cmd_sha:
        return None
    for path in find_existing_runs(experiment_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if data.get("cmd_sha") != cmd_sha:
            continue
        if skip_orphans and is_orphan_sidecar(experiment_dir, path.stem):
            continue
        return path
    return None


def is_orphan_sidecar(experiment_dir: Path, run_id: str) -> bool:
    """Return True if the sidecar for *run_id* never landed a cluster job.

    A sidecar is orphan when BOTH:

    * Its ``job_ids`` field is empty/missing (set by
      :func:`update_run_sidecar_job_ids` after a successful qsub).
    * No live journal record exists for the same ``run_id`` (or the
      journal record's ``job_ids`` is empty).

    Either signal alone is not enough:

    * Sidecar with ``job_ids`` and no journal — that's the journal-wipe
      recovery contract (machine swap / ``rm -rf ~/.claude/hpc/``);
      :func:`runner.submit_and_record` will reconstruct the journal from
      the sidecar's ``job_ids``.
    * Empty sidecar ``job_ids`` but a live journal with ``job_ids`` —
      pre-existing v2 sidecars that predate the post-qsub finalize hook;
      we trust the journal and treat the sidecar as committed.

    Used by :func:`find_run_by_cmd_sha` (opt-in skip during resume
    detection) and :func:`prune_orphan_sidecars` (delete them).
    """
    from hpc_agent.state.journal import load_run

    # Sidecar-side signal: was finalize_run_sidecar_job_ids ever called?
    sidecar_path = run_sidecar_path(experiment_dir, run_id)
    sidecar_job_ids: list[str] | None = None
    try:
        sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        raw = sidecar_data.get("job_ids")
        if isinstance(raw, list):
            sidecar_job_ids = [str(j) for j in raw]
    except (OSError, json.JSONDecodeError):
        sidecar_job_ids = None

    # Journal-side signal: did submit_and_record run to completion?
    try:
        record = load_run(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — journal corruption == treat as orphan
        record = None

    sidecar_committed = bool(sidecar_job_ids)
    journal_committed = record is not None and bool(record.job_ids)
    return not (sidecar_committed or journal_committed)


def update_run_sidecar_job_ids(experiment_dir: Path, run_id: str, job_ids: list[str]) -> Path:
    """Rewrite an existing sidecar with *job_ids* set; return its path.

    Called from :func:`runner.submit_and_record` immediately after qsub
    returns. Loads the sidecar in place, sets ``job_ids``, and atomically
    rewrites — preserving all other v2 config fields (resources, env,
    constraints, …) untouched. This is the post-qsub finalize that
    distinguishes a real run from the half-baked sidecar Step 6d wrote
    before rsync.

    Idempotent: re-running with the same *job_ids* is a no-op rewrite.
    Raises :class:`FileNotFoundError` if no sidecar exists for *run_id*
    (the caller should have written one earlier in the pipeline).
    """
    target = run_sidecar_path(experiment_dir, run_id)
    if not target.is_file():
        raise FileNotFoundError(f"run sidecar not found: {target}")
    # Route through ``atomic_locked_update`` so the read-modify-write
    # serializes against concurrent ``write_run_sidecar`` callers; every
    # sibling state writer (runtime_prior, user_profiles, cursor) uses
    # the same lock seam.
    from hpc_agent.infra.io import atomic_locked_update

    new_job_ids = [str(j) for j in job_ids]

    def _mutate(existing: dict[str, Any] | None) -> dict[str, Any]:
        if existing is None:
            # Sidecar vanished between the existence check and the lock —
            # preserve the documented FileNotFoundError contract.
            raise FileNotFoundError(f"run sidecar not found: {target}")
        existing["job_ids"] = new_job_ids
        return existing

    atomic_locked_update(target, _mutate)
    return target


# Default minimum age before a sidecar is eligible for orphan pruning.
# ``write_run_sidecar`` writes the (jobless) sidecar at Step 6d, then
# ``submit_flow`` runs the rsync + qsub + ``update_run_sidecar_job_ids``
# + journal-write sequence. Between those two points the sidecar IS
# legitimately job-less, which is what :func:`is_orphan_sidecar` keys
# on — pruning during that window deletes a sidecar an in-flight
# submit is about to finalize, then the post-qsub finalize raises
# ``FileNotFoundError`` and orphans the cluster jobs. A 5-minute floor
# is long enough to cover the rsync + canary + qsub of even a
# large-deploy submit while still keeping prune useful after a failed
# batch (the slow path takes seconds, not minutes).
_PRUNE_ORPHAN_MIN_AGE_SECONDS: int = 300


@primitive(
    name="prune-orphan-sidecars",
    verb="mutate",
    side_effects=[
        SideEffect("removes-files", "<experiment>/.hpc/runs/*.json (orphans only)"),
    ],
    idempotent=True,
    idempotency_key="experiment_dir",
    agent_facing=True,
)
def prune_orphan_sidecars(
    experiment_dir: Path,
    *,
    min_age_seconds: int = _PRUNE_ORPHAN_MIN_AGE_SECONDS,
) -> list[str]:
    """Delete every orphan sidecar under ``<exp>/.hpc/runs/``.

    Returns the list of run_ids whose sidecars were removed (for a
    diagnostic banner in the slash-command flow). Idempotent —
    re-invocations after the first pass are no-ops.

    Use case: a campaign batch hit cluster-side ssh rate limits, leaving
    half-baked sidecars from the failed submissions. Those sidecars
    would otherwise show up to :func:`find_run_by_cmd_sha` and the
    runtime-prior aggregator without a corresponding cluster job. Run
    this primitive after the batch finishes (or as part of `/resume-hpc`
    or `/setup-hpc`) to clean them up.

    *min_age_seconds* skips sidecars younger than the cutoff (default
    5 minutes, see :data:`_PRUNE_ORPHAN_MIN_AGE_SECONDS`). Between
    ``write_run_sidecar`` (Step 6d) and the post-qsub finalize that
    populates ``job_ids``, a sidecar is legitimately job-less — pruning
    in that window deletes a sidecar an in-flight submit is about to
    finalize, raising ``FileNotFoundError`` on the finalize and orphaning
    the cluster jobs. Pass ``min_age_seconds=0`` to prune immediately
    (only safe when the caller can guarantee no concurrent
    ``submit_flow`` is mid-pipeline against the same experiment).
    """
    import time as _time

    if min_age_seconds < 0:
        raise errors.SpecInvalid("min_age_seconds must be non-negative")
    cutoff = _time.time() - float(min_age_seconds)
    deleted: list[str] = []
    for path in find_existing_runs(experiment_dir):
        run_id = path.stem
        if not is_orphan_sidecar(experiment_dir, run_id):
            continue
        if min_age_seconds > 0:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                # Too fresh — skip; an in-flight submit may still be
                # mid-pipeline against this sidecar.
                continue
        try:
            path.unlink()
            deleted.append(run_id)
        except OSError:
            continue
    return deleted


def prune_old_runs(experiment_dir: Path, keep: int | None = None) -> list[Path]:
    """Evict oldest sidecars past the retention cap. Returns deleted paths.

    *keep* defaults to the module-level :data:`MAX_RUNS`, resolved at
    call time so a test/caller that monkeypatches ``MAX_RUNS`` is
    honoured — a ``keep=MAX_RUNS`` default argument would freeze the
    value at import time.
    """
    if keep is None:
        keep = MAX_RUNS
    if keep < 0:
        raise errors.SpecInvalid("keep must be non-negative")
    hits = find_existing_runs(experiment_dir)
    if len(hits) <= keep:
        return []
    deleted: list[Path] = []
    for path in hits[keep:]:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            continue
    return deleted
