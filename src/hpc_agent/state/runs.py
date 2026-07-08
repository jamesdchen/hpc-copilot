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
from collections.abc import Collection
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.state.code_drift import detect_code_drift
from hpc_agent.state.wave_map import derive_wave_map

__all__ = [
    "MAX_RUNS",
    "SIDECAR_SCHEMA_VERSION",
    "find_existing_runs",
    "find_run_by_cmd_sha",
    "is_orphan_sidecar",
    "prune_old_runs",
    "prune_orphan_sidecars",
    "read_run_sidecar",
    "resolve_node_sha",
    "run_sidecar_path",
    "sidecar_effective_identity",
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
# :mod:`hpc_agent.state.run_sha`. Callers import from there directly.


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
    "service_env",  # dict — externally-provisioned service address vars (#231 Tier 1)
    "constraints",  # dict — overrides on clusters.yaml constraints
    "gpu_fallback",  # list — ordered GPU types to try
    "max_retries",  # int — auto-resubmission cap
    "runtime",  # str — "uv" or omitted
    "auto_retry",  # dict — per-category retry policy
    "aggregate_defaults",  # dict — require_outputs/expect_output/aggregate_cmd
    "results",  # dict — declared result-file schema (see _RESULTS_BLOCK_KEYS)
    "trial_tokens",  # list — opaque per-task tokens a closed-loop strategy round-trips
    "trial_params",  # list[dict] — opaque per-task resolved params (cmd_sha pre-image; provenance)
    "parent_run_ids",  # list — run_ids this run consumes outputs from (DAG lineage)
    "node_sha",  # str — compose_node_sha(cmd_sha, parents) when parent_run_ids set
    "data_sha",  # str — data identity of the declared input dataset(s) (#222)
    "data_manifest_sha",  # str — data-manifest identity of declared input roots (amendment 0b)
    "env_hash",  # str — resolved env identity: modules/conda/runtime (#222)
    "scopes",  # list[str] — opaque caller-owned evidence-scope tags; core never interprets them
    "reproduces",  # str — run_id of the ORIGINAL this run is a deliberate reproduction of
    "audited_source",  # dict — opaque caller-owned audit-trail identity; core never interprets it
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
    "service_env": None,
    "constraints": None,
    "gpu_fallback": None,
    "max_retries": None,
    "runtime": None,
    "auto_retry": None,
    "aggregate_defaults": None,
    "results": None,
    "trial_tokens": None,
    "trial_params": None,
    "parent_run_ids": None,
    "node_sha": None,
    "data_sha": None,
    "data_manifest_sha": None,
    "env_hash": None,
    "scopes": None,
    "reproduces": None,
    "audited_source": None,
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


# ``derive_wave_map`` lives in :mod:`hpc_agent.state.wave_map` and is
# imported above for use by :func:`write_run_sidecar`.


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
    service_env: dict[str, Any] | None = None,
    constraints: dict[str, Any] | None = None,
    gpu_fallback: list[str] | None = None,
    max_retries: int | None = None,
    runtime: str | None = None,
    auto_retry: dict[str, Any] | None = None,
    aggregate_defaults: dict[str, Any] | None = None,
    results: dict[str, Any] | None = None,
    trial_tokens: list[Any] | None = None,
    trial_params: list[dict[str, Any]] | None = None,
    parent_run_ids: list[str] | None = None,
    node_sha: str | None = None,
    data_sha: str | None = None,
    data_manifest_sha: str | None = None,
    env_hash: str | None = None,
    job_ids: list[str] | None = None,
    scopes: list[str] | None = None,
    reproduces: str | None = None,
    audited_source: dict[str, Any] | None = None,
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

    *trial_tokens* is an optional list of opaque per-task tokens (one per
    task, ``resolve(i)``-order) that a closed-loop strategy round-trips to
    reconcile a finished iteration's result back to the proposal that
    produced it (e.g. an Optuna trial number, a PBT ``(member, generation)``
    pair). The framework never interprets them — they are recorded verbatim
    and re-surfaced by
    :func:`hpc_agent.execution.mapreduce.reduce.history.prior_records`.

    *trial_params* is an optional list of the resolved per-task params (one
    dict per task, ``resolve(i)``-order, with
    :data:`hpc_agent.state.run_sha.RESERVED_TASK_KEYS` stripped — i.e. the
    exact pre-image of ``cmd_sha``). Persisting it makes a run's params
    recoverable from its sidecar (``cmd_sha`` is a one-way hash), purely for
    provenance / reproducibility; like *trial_tokens* the framework records
    them verbatim and never interprets them, and ``prior_records`` re-surfaces
    them paired with each iteration's metrics. Compute it at submit via
    :func:`hpc_agent.incorporation.build.compute_run_id.compute_run_id`.

    *data_sha* / *env_hash* extend provenance past parameter (``cmd_sha``)
    and code (``tasks_py_sha``) identity to the DATA and ENVIRONMENT a run
    was produced under (#222). Compute them with
    :func:`hpc_agent.state.run_sha.compute_data_sha` (DVC pointer when
    present, else a content-hash of the declared input paths) and
    :func:`hpc_agent.state.run_sha.compute_env_hash` (resolved
    modules/conda_source/conda_envs + ``HPC_RUNTIME``). Both are optional;
    the per-campaign provenance manifest
    (:func:`hpc_agent.ops.provenance_manifest.build_provenance_manifest`)
    reads them back off the sidecars to pair every run with its full
    {code, data, env, params, cluster} fingerprint.

    *scopes* is an optional list of opaque caller-owned evidence-scope tags.
    Like *trial_tokens* / *trial_params* the framework records them verbatim
    and never interprets them — they route as owned-by-S1 and are recovered
    from the sidecar by downstream evidence tooling. Absent (``None``) → the
    key is omitted entirely, so a scope-less run's sidecar is byte-identical
    to one written before this field existed.

    *reproduces* records the ``run_id`` of the ORIGINAL run this submission is
    a deliberate reproduction of (the ``reproduction_of`` dedup lever that
    pierced the same-params guards to let it run). Recorded verbatim, never
    interpreted; :func:`find_run_by_cmd_sha`'s ``reproduction_of`` lever reads
    it back so a *later* reproduction of the SAME original skips this derived
    run too (not just the original). Absent (``None``) → the key is omitted,
    so an ordinary run's sidecar is byte-identical to one written before this
    field existed.

    *audited_source* echoes interview.json's ``audited_source`` opt-in block
    ({``source`` py relpath, ``template`` py relpath, ``audit_id`` slug};
    notebook-audit T14) so ``export-dossier`` can seal the complete audit trail
    (source + template + the ``.hpc/notebooks/<audit_id>.decisions.jsonl``
    attestation journal). Opaque — core never interprets it. Recorded verbatim
    like *scopes* / *reproduces*; absent (``None``) → the key is omitted, so a
    non-audited run's sidecar is byte-identical to one written before the field
    existed.

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
        wave_map = derive_wave_map(experiment_dir, task_count=int(task_count))
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
        "service_env": service_env,
        "constraints": constraints,
        "gpu_fallback": gpu_fallback,
        "max_retries": max_retries,
        "runtime": runtime,
        "auto_retry": auto_retry,
        "aggregate_defaults": aggregate_defaults,
        "results": results,
        "trial_tokens": list(trial_tokens) if trial_tokens is not None else None,
        "trial_params": [dict(p) for p in trial_params] if trial_params is not None else None,
        "parent_run_ids": list(parent_run_ids) if parent_run_ids else None,
        "node_sha": node_sha,
        "data_sha": data_sha,
        # Data-manifest identity of the declared input ROOTS (Phase-3 amendment,
        # ruled 0b) — DISTINCT from data_sha (which is the input_datasets/DVC
        # identity, #222). Same only-write-non-None pattern, so a run with no
        # minted manifest is byte-identical to a pre-amendment sidecar.
        "data_manifest_sha": data_manifest_sha,
        "env_hash": env_hash,
        "job_ids": list(job_ids) if job_ids is not None else None,
        # Opaque caller-owned evidence-scope tags. Recorded verbatim, never
        # interpreted by core (same only-write-non-None-keys pattern as every
        # other v2 field, so a scope-less run's sidecar is byte-identical).
        "scopes": list(scopes) if scopes is not None else None,
        # run_id of the ORIGINAL this run deliberately reproduces (same
        # only-write-non-None pattern; a non-reproduction run's sidecar is
        # byte-identical). find_run_by_cmd_sha's reproduction_of lever reads it.
        "reproduces": reproduces,
        # Opaque caller-owned audit-trail identity — the sidecar echo of
        # interview.json's audited_source block ({source, template, audit_id};
        # notebook-audit T14). Recorded verbatim, never interpreted by core
        # (same only-write-non-None pattern, so a non-audited run's sidecar is
        # byte-identical). export-dossier reads it back to seal the audit trail.
        "audited_source": audited_source,
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
        except ImportError:  # circular/missing import → skip the observability warning
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


def resolve_node_sha(
    experiment_dir: Path,
    *,
    cmd_sha: str,
    parent_run_ids: list[str] | None,
) -> str | None:
    """Compose this run's DAG-node identity from its parents' sidecars.

    The submit-side half of the recursive-identity invariant
    (``docs/design/dag-kernel.md``): a run that declares
    *parent_run_ids* gets identity
    ``compose_node_sha(cmd_sha, [parent identities...])``, where each
    parent's identity is its sidecar's recorded ``node_sha`` — or its
    bare ``cmd_sha`` for a parentless parent (the 0-parent degeneracy).
    Identity is always DERIVED here from on-disk sidecars, never
    asserted by the caller — a caller-supplied node_sha could decouple
    a child from its real ancestry.

    Returns ``None`` when *parent_run_ids* is empty/None: a parentless
    run's identity is its ``cmd_sha`` and the sidecar stays compact
    (no redundant ``node_sha`` key).

    Raises :class:`errors.SpecInvalid` when a parent sidecar is missing
    (the declared dependency does not exist locally — submit the parent
    first, or fix the run_id), when a parent sidecar carries no usable
    identity, or when any identity is not a full 64-hex digest
    (``compose_node_sha`` composes full digests only; 8-char prefixes
    that satisfy some wire patterns cannot participate in a DAG node).
    """
    if not parent_run_ids:
        return None
    from hpc_agent.state.run_sha import compose_node_sha

    parent_identities: list[str] = []
    for parent_id in parent_run_ids:
        try:
            sidecar = read_run_sidecar(experiment_dir, parent_id)
        except FileNotFoundError:
            raise errors.SpecInvalid(
                f"parent run {parent_id!r} has no sidecar under "
                f"{experiment_dir}/.hpc/runs/ — a declared parent must have "
                "been submitted (and its sidecar written) before a child can "
                "compose its identity. Submit the parent first, or remove it "
                "from `parents`."
            ) from None
        identity = sidecar.get("node_sha") or sidecar.get("cmd_sha")
        if not identity:
            raise errors.SpecInvalid(
                f"parent run {parent_id!r}'s sidecar carries neither node_sha "
                "nor cmd_sha — it cannot participate as a DAG parent."
            )
        parent_identities.append(str(identity))
    try:
        return compose_node_sha(cmd_sha, parent_identities)
    except ValueError as exc:
        raise errors.SpecInvalid(
            f"cannot compose node identity for a parented run: {exc}. "
            "DAG identity requires full 64-hex sha256 digests for the run's "
            "cmd_sha and every parent identity."
        ) from exc


def sidecar_effective_identity(sidecar: dict[str, Any]) -> str | None:
    """A run sidecar's EFFECTIVE dedup identity, as ONE definition.

    ``node_sha`` when the run declared parents (params + ancestry,
    ``docs/design/dag-kernel.md``), else its bare ``cmd_sha`` (the 0-parent
    degeneracy). This is the same rule :func:`find_run_by_cmd_sha` matches
    with; the supersession sibling gate (``ops/supersession.py``)
    routes through it too so the "same code identity" predicate cannot drift
    between the two consumers (engineering-principles: one definition per
    identity decision). Returns ``None`` when the sidecar carries neither —
    identity unknown, never a match.
    """
    identity = sidecar.get("node_sha") or sidecar.get("cmd_sha")
    return str(identity) if identity else None


def find_run_by_cmd_sha(
    experiment_dir: Path,
    cmd_sha: str,
    *,
    skip_orphans: bool = False,
    tasks_py_sha: str | None = None,
    current_executor: str | None = None,
    invalidate_on_code_change: bool = False,
    campaign_id: str | None = None,
    node_sha: str | None = None,
    reproduction_of: str | None = None,
) -> Path | None:
    """Return the newest sidecar matching *cmd_sha*, or ``None`` if absent.

    Compares the full cmd_sha string. Iterates newest-first so a fresh
    resume detection picks the most recent matching run.

    Dedup identity is PARAMETER identity, not code identity (#207):
    ``cmd_sha`` is hashed solely from the materialized per-task kwargs
    (see :func:`hpc_agent.state.run_sha.compute_cmd_sha`), so an
    executor-body edit that leaves every swept parameter unchanged
    matches here and dedups against the prior run BY DESIGN. The
    executor's source is provenance, recorded separately on the sidecar
    as ``tasks_py_sha``.

    *skip_orphans* (default False) preserves the journal-wipe recovery
    contract: a sidecar with no journal record is the canonical signal
    that the journal at ``~/.claude/hpc/<repo_hash>/`` was wiped (machine
    swap, rm -rf) and ``submit_and_record`` should reconstruct from the
    sidecar instead of re-qsub'ing a job the cluster already has running.
    Pass ``skip_orphans=True`` only after a known-failed batch where the
    sidecars without journal records are guaranteed to be half-baked
    (e.g. inside the prune primitive) — see :func:`prune_orphan_sidecars`.

    *tasks_py_sha* / *invalidate_on_code_change* are the OPT-IN
    code-iteration safety lever (#207). Both default to off, in which
    case this function behaves EXACTLY as the historical
    match-on-cmd_sha-string version — the dedup key is unchanged.

    * When *tasks_py_sha* is supplied (the drift sha of the
      about-to-submit ``tasks.py``, via
      :func:`hpc_agent.state.run_sha.compute_tasks_py_sha`) and the
      matched sidecar recorded a *different* non-empty ``tasks_py_sha``,
      the executor body changed since that run even though the swept
      params did not. We emit a :class:`UserWarning` ("deduping against
      run X, but the code changed since; pass
      --invalidate-on-code-change to force a fresh run") — a safety net
      that NEVER alters the dedup decision on its own.
    * When *invalidate_on_code_change* is also True, a drifted match is
      treated as NOT a match: this folds ``tasks_py_sha`` into the dedup
      key for this one lookup, so the caller proceeds to a fresh submit
      instead of replaying the stale run. Scanning continues to older
      sidecars (an older run whose ``tasks_py_sha`` matches the current
      code is still a legitimate dedup target); ``None`` is returned when
      no param-and-code match remains.

    *current_executor* (#351 sub-bug #5) rides the SAME code-drift lane as
    *tasks_py_sha* — it is the per-task ``executor`` command of the
    about-to-submit run (what ``_hpc_dispatch.py`` actually invokes; read
    from the run's own sidecar, which submit-flow ensures before rsync).
    ``cmd_sha`` stays PURE PARAMETER identity (#207) — the executor is
    NOT folded into it — but the executor command participates in NO
    identity sha at all (not ``cmd_sha``, ``tasks_py_sha``, or
    ``node_sha``), so an entry-point / executor change with unchanged
    swept params was previously an INVISIBLE silent replay on the PRE-change
    executor. When *current_executor* differs from the matched sidecar's
    recorded ``executor`` we treat it EXACTLY as a ``tasks_py_sha``
    mismatch: warn-and-dedup by default (the change is now VISIBLE), and
    ``continue`` past the match under *invalidate_on_code_change* (fresh
    run). A matched sidecar with an empty/absent recorded ``executor`` is
    NOT treated as drift (same tolerance as the empty-``tasks_py_sha``
    fallback) — we cannot prove the command changed.

    A sidecar with an empty/absent recorded ``tasks_py_sha`` (drift
    detection was disabled for that run, e.g. ``tasks.py`` was unreadable
    at submit; see ``ops/submit_flow.py``) is NOT treated as drift — we
    cannot prove the code changed, so we fall back to the param-only
    dedup and neither warn nor invalidate.

    *campaign_id* is the closed-loop campaign-iteration rejection lever.
    When set, a matched sidecar that belongs to the SAME campaign is NOT a
    dedup target: campaign iterations deliberately re-run — a stochastic
    strategy (Optuna, PBT, random search) may propose identical params in
    two different iterations, and deduping the later one against the earlier
    would silently drop a trial the strategy meant to run (the cmd_sha
    collision footgun documented in ``docs/workflows/campaign.md``). The
    scan continues past same-campaign matches, so a non-campaign or
    different-campaign run with the same params is still a valid dedup
    target. This is orthogonal to the #207 code-drift lever above — a
    campaign-iteration repeat and a code edit are decided independently.
    A same-machine resume of the in-progress iteration is handled earlier
    by the journal ``run_id`` path, not here.

    *node_sha* is the DAG-lineage identity lever
    (``docs/design/dag-kernel.md``). Both sides of the comparison use
    the run's EFFECTIVE identity — its recorded ``node_sha`` when it
    declared parents, its bare ``cmd_sha`` otherwise (the 0-parent
    degeneracy makes these the same thing for parentless runs):

    * ``node_sha=None`` (default) queries by bare ``cmd_sha`` —
      byte-for-byte the historical behaviour for every sidecar that
      predates DAG lineage. A sidecar that DID record a ``node_sha``
      no longer matches a bare-cmd_sha query: same params consuming
      different inputs are different experiments.
    * ``node_sha=<composed>`` (from :func:`resolve_node_sha`) queries by
      the composed identity, so a parented re-submit dedups only against
      a prior run with the same params AND the same ancestry — never
      against a stale run computed from a since-changed parent.

    *reproduction_of* is the reproduction-receipt rejection lever — the
    same-campaign lever's sibling for a DELIBERATE one-off re-run of
    identical params (a reproduction, not a campaign iteration). When set to
    the ORIGINAL run's ``run_id``, a matched sidecar is NOT a dedup target
    when EITHER its own ``run_id`` equals *reproduction_of* (the original
    itself) OR its recorded ``reproduces`` equals *reproduction_of* (a prior
    reproduction of the same original) — the scan continues past both. So a
    reproduction never dedups against — and silently replays — the very run
    it means to reproduce, nor against an earlier reproduction of it, while
    an UNRELATED prior with the same params is still a valid dedup target.
    Unset (default) → behaviour is byte-identical to the historical
    match-on-identity version. Orthogonal to the #207 code-drift and the
    campaign-iteration levers — each is decided independently.
    """
    if not cmd_sha:
        return None
    query_identity = node_sha or cmd_sha
    for path in find_existing_runs(experiment_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if sidecar_effective_identity(data) != query_identity:
            continue
        if skip_orphans and is_orphan_sidecar(experiment_dir, path.stem):
            continue
        # Campaign-iteration rejection: a same-campaign match is a prior
        # iteration, not a replay target — keep scanning. Orthogonal to the
        # code-drift logic below.
        if campaign_id and data.get("campaign_id") == campaign_id:
            continue
        # Reproduction-receipt rejection: when this submit is a deliberate
        # reproduction of `reproduction_of`, neither the ORIGINAL (matched by
        # run_id) nor any PRIOR reproduction of it (matched by the recorded
        # `reproduces`) is a replay target — keep scanning so an unrelated
        # same-params prior can still dedup. Orthogonal to the code-drift and
        # campaign-iteration levers.
        if reproduction_of and (
            path.stem == reproduction_of or data.get("reproduces") == reproduction_of
        ):
            continue
        # Param identity matched. Now consider CODE identity — the executor
        # command and the tasks.py bytes — via the single shared drift predicate
        # (also used by the layer-1 run_id gate in ``ops/submit/runner.py``). A
        # dimension fires only when both the matched sidecar AND the caller
        # supply a non-empty value to compare; an absent value disables it (we
        # cannot prove it changed). cmd_sha is pure PARAMETER identity (#207), so
        # a changed executor / entry point (#351 sub-bug #5) or an edited
        # tasks.py on the SAME swept params would otherwise replay PRE-change
        # code silently.
        drift = detect_code_drift(
            recorded_executor=data.get("executor"),
            recorded_tasks_py_sha=data.get("tasks_py_sha"),
            current_executor=current_executor,
            current_tasks_py_sha=tasks_py_sha,
        )
        if drift.drifted:
            if invalidate_on_code_change:
                # Fold the drift signal (tasks_py_sha AND/OR executor) into the
                # dedup key for this lookup: this run's params match but its
                # code/executor differs, so it is NOT a valid replay target —
                # keep scanning for an older run that also matches (returns
                # None if none does).
                continue
            if drift.executor_changed:
                warnings.warn(
                    f"deduping against run {path.stem!r} (same cmd_sha "
                    f"{cmd_sha[:8]}…, i.e. identical swept parameters), but its "
                    f"recorded executor command ({drift.drifted_executor!r}) "
                    f"differs from the current ({str(current_executor)!r}) — the "
                    "entry point / executor changed since that run. The replay "
                    "will run the PRIOR submission's executor (dedup keys on "
                    "parameters by design, #207/#351). Pass "
                    "--invalidate-on-code-change (or set "
                    "invalidate_on_code_change=True) to force a fresh run.",
                    UserWarning,
                    stacklevel=2,
                )
            if drift.code_changed:
                warnings.warn(
                    f"deduping against run {path.stem!r} (same cmd_sha "
                    f"{cmd_sha[:8]}…, i.e. identical swept parameters), but its "
                    f"recorded tasks.py drift sha {str(drift.drifted_tasks_py_sha)[:8]}… "
                    f"differs from the current {str(tasks_py_sha)[:8]}… — the "
                    "executor code changed since that run. The replay will run "
                    "the PRIOR submission's code (dedup keys on parameters by "
                    "design, #207). Pass --invalidate-on-code-change (or set "
                    "invalidate_on_code_change=True) to force a fresh run.",
                    UserWarning,
                    stacklevel=2,
                )
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
    except (OSError, errors.JournalCorrupt):
        # Corrupt/unreadable journal record == treat as orphan. load_run
        # already returns None for the routine missing/torn-file cases, so
        # narrowing here means a *programming* error propagates (fail loud)
        # rather than silently flipping a live run to "orphan" — which the
        # prune primitive would then delete.
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


def backfill_run_sidecar_provenance(
    experiment_dir: Path,
    run_id: str,
    *,
    data_sha: str | None,
    env_hash: str | None,
    data_manifest_sha: str | None = None,
) -> Path:
    """Fill *null* provenance fields on an existing sidecar; return its path.

    The #312 capture seam for sidecars that were pre-written (Step 6d /
    ``resolve-submit-inputs``) before submit-flow could compute ``data_sha``
    / ``env_hash``: strictly **additive** — a field is written only when the
    sidecar's current value is null/absent AND the supplied value is
    non-null, so an explicitly recorded provenance value is never
    overwritten and the write-first invariant (#148/#150: never clobber a
    pre-written sidecar's config) is untouched. Same post-write-update
    precedent as :func:`update_run_sidecar_job_ids`, same lock seam.

    Raises :class:`FileNotFoundError` if no sidecar exists for *run_id*.
    """
    target = run_sidecar_path(experiment_dir, run_id)
    if not target.is_file():
        raise FileNotFoundError(f"run sidecar not found: {target}")
    from hpc_agent.infra.io import atomic_locked_update

    def _mutate(existing: dict[str, Any] | None) -> dict[str, Any]:
        if existing is None:
            raise FileNotFoundError(f"run sidecar not found: {target}")
        for field, value in (
            ("data_sha", data_sha),
            ("env_hash", env_hash),
            ("data_manifest_sha", data_manifest_sha),
        ):
            if value is not None and existing.get(field) is None:
                existing[field] = value
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
    exclude: Collection[str] | None = None,
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

    *exclude* names run_ids that must never be pruned regardless of age
    or orphan status. ``submit_flow_batch`` passes the run_ids it is
    about to submit (and their ``-canary`` siblings): those sidecars are
    written jobless at Step 6d *before* the prune runs inside the lock,
    so they look exactly like a prior failed batch's orphan and would
    otherwise be deleted out from under the in-flight submit.
    """
    import time as _time

    if min_age_seconds < 0:
        raise errors.SpecInvalid("min_age_seconds must be non-negative")
    protected = set(exclude or ())
    cutoff = _time.time() - float(min_age_seconds)
    deleted: list[str] = []
    for path in find_existing_runs(experiment_dir):
        run_id = path.stem
        if run_id in protected:
            continue
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
