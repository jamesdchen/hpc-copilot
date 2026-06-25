"""``resolve-resources``: composite primitive — hpc-submit Step 6.

WS5 #5. Collapses ``hpc-submit`` SKILL.md Step 6's silent multi-step
resource resolution — runtime-prior walltime + cluster gpu default +
partition recommendation — into ONE CLI verb so the agent's role shrinks
to a single tool call.

Three fields are resolved, each from a caller override first, then an
auto-resolution rule:

* ``walltime_sec`` — caller, else the optional ``read-runtime-prior``
  verb's p95 × ``safety_mult``. ``read-runtime-prior`` is an
  **optional-plugin-only** verb: on a core install it is NOT a registered
  subcommand, and on the very first submit there is no prior anyway. A
  missing/erroring verb, or a present verb that reports ``needs_canary``
  (no samples yet), is treated as **cold-start** — ``walltime_sec`` stays
  ``null`` and the caller (Step 6 prose) falls back to the cluster's
  ``get_default_walltime_sec``. A missing prior is NEVER an error.
* ``gpu_type`` — caller, else ``clusters.<cluster>.gpu_types[0]`` (the
  first declared GPU). ``null`` when the cluster declares none.
* ``partition`` — delegated verbatim to the existing
  :func:`hpc_agent.ops.submit.recommend_partition.recommend_partition`
  primitive when the caller supplies the cluster's partition list;
  ``null`` (with a ``no_partitions_supplied`` provenance note) when no
  partition config is available. Partition logic is NOT reimplemented
  here.

The ``read-runtime-prior`` probe is the only subprocess call and it is
local (it reads the on-disk runtime-prior store), so this verb does NOT
touch the cluster — ``requires_ssh`` is ``False``.

I/O contracts:

* Input: see ``hpc_agent/schemas/resolve_resources.input.json``.
* Output: a ``dict`` matching ``schemas/resolve_resources.output.json``.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.recommend_partition import (
    PartitionInfo,
    RecommendPartitionSpec,
)
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.ops.recommend_pe import recommend_pe
from hpc_agent.ops.submit.field_partition import AUTO_RESOLVABLE_FIELDS
from hpc_agent.ops.submit.recommend_partition import recommend_partition

__all__ = [
    "DEFAULT_SAFETY_MULT",
    "resolve_resources",
]

# Drift guard: every field this verb auto-resolves MUST be declared
# auto-resolvable in the single field partition (ops/submit/field_partition).
# If a future edit teaches resolve-resources a new field but forgets to
# register it as auto-resolvable, the partition and the resolver have
# drifted — and walk-submit-ambiguities (which reuses this verb) would then
# put a value into `resolved` for a field the partition thinks needs the
# caller. Fail at import, not at the demo. (The reverse — partition fields
# this verb doesn't touch, e.g. data_axis — is fine; they resolve elsewhere.)
_RESOLVE_RESOURCES_FIELDS: frozenset[str] = frozenset(
    {"walltime_sec", "gpu_type", "partition", "mpi_pe"}
)
_resource_drift = _RESOLVE_RESOURCES_FIELDS - AUTO_RESOLVABLE_FIELDS
if _resource_drift:
    raise RuntimeError(
        "resolve-resources auto-resolves fields the partition does not mark "
        f"auto-resolvable: {sorted(_resource_drift)}. Add them to "
        "hpc_agent.ops.submit.field_partition.AUTO_RESOLVABLE_FIELDS."
    )

# p95 → walltime ask safety multiplier. Mirrors the Step 6 prose default
# (``prior.p95_sec * 1.30``): a 30% headroom over the historical p95 so a
# tail-heavy run doesn't get killed at the walltime ceiling.
DEFAULT_SAFETY_MULT = 1.30


def _read_runtime_prior_p95(
    *,
    experiment_dir: str,
    profile: str,
    cluster: str,
    cmd_sha: str | None,
    gpu_type: str | None,
    timeout_sec: float,
) -> tuple[float | None, str]:
    """Probe the optional ``read-runtime-prior`` verb for a p95 runtime.

    Returns ``(p95_sec, note)``. ``p95_sec`` is ``None`` on every
    cold-start path — the verb is unregistered (core install: argparse
    "invalid choice", exit 2), the call errors/times out, the envelope is
    not ``ok``, the verb reports ``needs_canary`` (no samples yet), or no
    quantile row matches the resolved ``gpu_type``. A missing prior is a
    normal cold-start, never an error — the caller falls back to the
    cluster cold-start walltime.

    When ``gpu_type`` is known the matching per-gpu quantile row is used;
    otherwise the first available row is taken (a single-GPU cluster has
    exactly one).
    """
    argv = [
        "hpc-agent",
        "read-runtime-prior",
        "--experiment-dir",
        experiment_dir,
        "--profile",
        profile,
        "--cluster",
        cluster,
    ]
    if cmd_sha is not None:
        argv += ["--cmd-sha", cmd_sha]

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "cold_start_prior_verb_unavailable"

    # argparse "invalid choice" (core install: verb not registered) exits
    # 2 with no JSON on stdout — indistinguishable from a missing plugin,
    # and equally a cold-start. Parse failure ⇒ cold-start, never an error.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, "cold_start_prior_verb_unavailable"

    if not envelope.get("ok"):
        return None, "cold_start_prior_verb_unavailable"

    data = envelope.get("data") or {}
    if data.get("needs_canary"):
        return None, "cold_start_no_samples"

    quantiles = data.get("quantiles") or {}
    if not isinstance(quantiles, dict) or not quantiles:
        return None, "cold_start_no_samples"

    row = quantiles.get(gpu_type) if gpu_type is not None else None
    if row is None:
        # No gpu_type match (or none supplied): take the first row — a
        # single-GPU cluster has exactly one, and any prior beats none.
        row = next(iter(quantiles.values()), None)
    if not isinstance(row, dict) or row.get("p95") is None:
        return None, "cold_start_no_samples"

    return float(row["p95"]), "prior_p95"


def _resolve_gpu_type(
    *, caller_gpu_type: str | None, cluster: str, clusters_path: str | None
) -> tuple[str | None, str]:
    """Resolve gpu_type: caller override, else ``clusters.<cluster>.gpu_types[0]``."""
    if caller_gpu_type is not None:
        return caller_gpu_type, "caller"
    cluster_path = Path(clusters_path) if clusters_path is not None else None
    cfg = load_clusters_config(path=cluster_path).get(cluster) or {}
    gpu_types = cfg.get("gpu_types") or []
    if gpu_types:
        return str(gpu_types[0]), "cluster_default"
    return None, "cluster_declares_none"


def _resolve_partition(
    *,
    caller_partition: str | None,
    partitions: list[dict[str, Any]] | None,
    user_preferred_partition: str | None,
    walltime_sec: int | None,
    experiment_dir: str,
) -> tuple[str | None, str]:
    """Resolve partition by REUSING the ``recommend-partition`` primitive.

    Caller override wins. Otherwise, when the caller supplied the
    cluster's partition list, delegate to :func:`recommend_partition` with
    the resolved walltime. When no partition config is available, return
    ``null`` rather than inventing one — partition logic is NOT
    reimplemented here.
    """
    if caller_partition is not None:
        return caller_partition, "caller"
    if not partitions:
        return None, "no_partitions_supplied"

    spec = RecommendPartitionSpec(
        # recommend-partition needs a positive walltime to route debug vs
        # normal; on cold-start (walltime unknown) use 1h, which lands a
        # short job on a debug partition where one exists — the routing the
        # planner would pick once a prior is established.
        requested_walltime_sec=walltime_sec if walltime_sec is not None else 3600,
        partitions=[PartitionInfo(**p) for p in partitions],
        user_preferred_partition=user_preferred_partition,
    )
    result = recommend_partition(Path(experiment_dir), spec=spec)
    return result.recommended_partition or None, f"recommend_partition:{result.rationale}"


def _resolve_mpi_pe(
    *,
    caller_mpi_pe: str | None,
    parallel_environments: list[dict[str, Any]] | None,
    mpi_ranks: int | None,
) -> tuple[str | None, str]:
    """Resolve the SGE parallel environment for a multi-rank job (#293).

    Caller override wins. Otherwise, when this is an MPI submit (``mpi_ranks``
    set) and the caller supplied the cluster's ``parallel_environments`` (from
    ``inspect-cluster``), delegate to :func:`recommend_pe` to pick a ``kind=mpi``
    PE with the slot capacity for ``mpi_ranks``. ``mpi_ranks`` absent ⇒ not an
    MPI submit ⇒ ``None`` (no PE). No enumeration supplied ⇒ ``None`` rather than
    inventing a name — the build-submit-spec SGE guard then asks for one.
    """
    if mpi_ranks is None:
        return None, "not_mpi"
    if caller_mpi_pe is not None:
        return caller_mpi_pe, "caller"
    if not parallel_environments:
        return None, "no_parallel_environments_supplied"
    pe, rationale = recommend_pe(parallel_environments, int(mpi_ranks))
    return pe, f"recommend_pe:{rationale}"


@primitive(
    name="resolve-resources",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Resolve hpc-submit Step 6 resources in one call: walltime_sec "
            "(caller, else read-runtime-prior p95 × safety_mult, else "
            "cold-start null), gpu_type (caller, else clusters.<cluster>."
            "gpu_types[0]), partition (caller, else recommend-partition), and "
            "mpi_pe (caller, else recommend-pe from parallel_environments when "
            "mpi_ranks is set). A missing/erroring read-runtime-prior is "
            "cold-start, not an error."
        ),
        verb="resolve-resources",
        args=(
            CliArg(
                "--cluster",
                type=str,
                required=True,
                help="Cluster name; gpu_type + prior lookup key into clusters.yaml.",
            ),
            CliArg(
                "--experiment-dir",
                type=str,
                default=".",
                help="Experiment directory (default cwd). Passed to read-runtime-prior.",
            ),
            CliArg(
                "--profile",
                type=str,
                default=None,
                help="Run profile (run_name); the read-runtime-prior lookup key.",
            ),
            CliArg(
                "--cmd-sha",
                type=str,
                default=None,
                help="Optional cmd_sha to filter the runtime prior to this code version.",
            ),
            CliArg(
                "--walltime-sec",
                type=int,
                default=None,
                help="Caller override for walltime_sec; skips the runtime-prior probe.",
            ),
            CliArg(
                "--gpu-type",
                type=str,
                default=None,
                help="Caller override for gpu_type; skips the cluster gpu_types[0] default.",
            ),
            CliArg(
                "--safety-mult",
                type=float,
                default=DEFAULT_SAFETY_MULT,
                help="Multiplier applied to the prior p95 to size walltime (default 1.30).",
            ),
            CliArg(
                "--partition",
                type=str,
                default=None,
                help="Caller override for partition; skips recommend-partition.",
            ),
            CliArg(
                "--user-preferred-partition",
                type=str,
                default=None,
                help="Soft partition preference forwarded to recommend-partition.",
            ),
            CliArg(
                "--mpi-pe",
                type=str,
                default=None,
                help="Caller override for the SGE parallel environment (#293); skips recommend-pe.",
            ),
            CliArg(
                "--mpi-ranks",
                type=int,
                default=None,
                help=(
                    "Total MPI ranks for a multi-rank submit (#293). When set, "
                    "mpi_pe is auto-derived from the cluster's parallel_environments."
                ),
            ),
        ),
        # Local-only: the read-runtime-prior probe reads the on-disk
        # runtime-prior store and clusters.yaml is a local file. No SSH.
        requires_ssh=False,
    ),
    agent_facing=True,
)
def resolve_resources(
    *,
    cluster: str,
    experiment_dir: str = ".",
    profile: str | None = None,
    cmd_sha: str | None = None,
    walltime_sec: int | None = None,
    gpu_type: str | None = None,
    safety_mult: float = DEFAULT_SAFETY_MULT,
    partition: str | None = None,
    user_preferred_partition: str | None = None,
    partitions: list[dict[str, Any]] | None = None,
    mpi_pe: str | None = None,
    mpi_ranks: int | None = None,
    parallel_environments: list[dict[str, Any]] | None = None,
    clusters_path: str | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """Resolve ``{walltime_sec, gpu_type, partition, mpi_pe}`` with provenance.

    ``mpi_pe`` (#293) is the SGE parallel environment for a multi-rank submit:
    caller override, else auto-derived by :func:`recommend_pe` from the
    cluster's ``parallel_environments`` when ``mpi_ranks`` is set, else ``null``
    (not an MPI submit / no enumeration supplied).

    Returns a dict matching ``schemas/resolve_resources.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. ``provenance`` maps
    each resolved field to how it was resolved (``caller`` /
    ``cluster_default`` / ``prior_p95`` / a ``cold_start_*`` reason /
    ``recommend_partition:<rationale>``), so a caller can audit which
    values were auto-resolved vs. supplied.

    gpu_type is resolved BEFORE walltime so the runtime-prior probe can
    select the matching per-gpu quantile row.
    """
    started = time.monotonic()

    # gpu_type first — it keys the per-gpu runtime-prior quantile row.
    resolved_gpu, gpu_prov = _resolve_gpu_type(
        caller_gpu_type=gpu_type, cluster=cluster, clusters_path=clusters_path
    )

    if walltime_sec is not None:
        resolved_walltime: int | None = walltime_sec
        walltime_prov = "caller"
    elif profile is None:
        # No profile ⇒ no key to look up a prior; pure cold-start.
        resolved_walltime = None
        walltime_prov = "cold_start_no_profile"
    else:
        p95, walltime_prov = _read_runtime_prior_p95(
            experiment_dir=experiment_dir,
            profile=profile,
            cluster=cluster,
            cmd_sha=cmd_sha,
            gpu_type=resolved_gpu,
            timeout_sec=timeout_sec,
        )
        resolved_walltime = round(p95 * safety_mult) if p95 is not None else None

    resolved_partition, partition_prov = _resolve_partition(
        caller_partition=partition,
        partitions=partitions,
        user_preferred_partition=user_preferred_partition,
        walltime_sec=resolved_walltime,
        experiment_dir=experiment_dir,
    )

    resolved_mpi_pe, mpi_pe_prov = _resolve_mpi_pe(
        caller_mpi_pe=mpi_pe,
        parallel_environments=parallel_environments,
        mpi_ranks=mpi_ranks,
    )

    return {
        "walltime_sec": resolved_walltime,
        "gpu_type": resolved_gpu,
        "partition": resolved_partition,
        "mpi_pe": resolved_mpi_pe,
        "provenance": {
            "walltime_sec": walltime_prov,
            "gpu_type": gpu_prov,
            "partition": partition_prov,
            "mpi_pe": mpi_pe_prov,
        },
        "elapsed_total_sec": time.monotonic() - started,
    }
