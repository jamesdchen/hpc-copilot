"""Cluster node inspection — read-only snapshot of node states for planning.

For each cluster, query the scheduler (SLURM ``scontrol``/``sacct`` or SGE
``qhost``/``qstat``) to assemble a structured per-node view that captures
the ingredients for resource-quality-aware submission decisions:

- Allocation pressure (host RAM, CPU load).
- GPU advertisements (advertised GRES vs. allocated GRES).
- Co-tenant context — other users' jobs running on the node, with their
  resource shares and how long they've been running.
- Drain / down state.

The resulting JSON is fed into :mod:`hpc_mapreduce.job.planner` (Phase 4)
which combines it with runtime priors to score candidate constraints.
It is also useful standalone for ad-hoc cluster
debugging via ``hpc-mapreduce inspect-cluster --cluster <c>``.

This module is intentionally permissive: scheduler outputs vary between
versions and configurations. Parsing failures degrade to "unknown" /
zero-valued fields rather than raising — better to deliver a partial
snapshot than to refuse to plan at all.
"""

from __future__ import annotations

__all__ = [
    "NodeSnapshot",
    "ClusterSnapshot",
    "inspect_cluster",
    "parse_scontrol_show_node",
    "parse_sacct_node_jobs",
    "persist_snapshot",
    "read_cluster_history",
    "MAX_HISTORY_SNAPSHOTS",
]

import dataclasses
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from hpc_mapreduce._primitive import SideEffect, primitive
from claude_hpc._internal._time import parse_iso_utc_or_none, utcnow, utcnow_iso

from hpc_mapreduce.infra.cache import TTLCache
from hpc_mapreduce.infra.clusters import load_clusters_config
from slash_commands import errors

# In-process cache so a single submit cycle that calls inspect-cluster
# multiple times pays the SSH cost once. Keyed by (cluster, scheduler).
# Stores the dict-form of :class:`ClusterSnapshot` (so re-reads survive
# even if the snapshot dataclass shape evolves between writes).
#
# Migrated to :class:`TTLCache` (B6) — same 60-second horizon as the
# pre-refactor module-level dict; gain is bounded LRU eviction + a
# ``clear_all()`` test hook shared with backfill's probe cache.
_CACHE: TTLCache[tuple[str, str], dict[str, Any]] = TTLCache(
    "infra.inspect", ttl_sec=60.0, max_size=64
)


@dataclasses.dataclass
class NodeSnapshot:
    """Per-node view used by the planner.

    Fields are best-effort: any of the numeric fields may be ``None`` when
    the scheduler did not report them. The planner treats ``None`` as
    "unknown, do not score against this signal".
    """

    name: str
    state: str = ""  # SLURM state string: IDLE / MIXED / ALLOCATED / DRAIN ...
    real_mem_mb: int | None = None
    alloc_mem_mb: int | None = None
    alloc_mem_pct: float | None = None
    cpu_tot: int | None = None
    cpu_alloc: int | None = None
    cpu_load: float | None = None  # 1-min load avg from scontrol
    cpu_load_frac: float | None = None  # cpu_load / cpu_tot, capped at None when unknown
    gres: str = ""  # advertised GRES, e.g. "gpu:a100:2"
    gres_used: str = ""  # allocated GRES, e.g. "gpu:a100:1"
    active_features: list[str] = dataclasses.field(default_factory=list)
    co_tenants: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    is_stressed: bool = False
    is_drained: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        return d


@dataclasses.dataclass
class ClusterSnapshot:
    cluster: str
    scheduler_kind: str
    now_iso: str
    nodes: list[NodeSnapshot]
    errors: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "scheduler_kind": self.scheduler_kind,
            "now_iso": self.now_iso,
            "nodes": [n.to_dict() for n in self.nodes],
            "errors": list(self.errors),
        }


# --- SLURM ----------------------------------------------------------------


def _parse_scontrol_kv_block(block: str) -> dict[str, str]:
    """Parse a scontrol show node block of ``Key=Value Key=Value`` pairs.

    Values may contain commas, slashes, and parentheses; only whitespace
    separates pairs at the top level. We split on whitespace then re-join
    fragments that lack ``=`` (a few SLURM versions print
    ``ActiveFeatures=foo, bar,baz`` with embedded spaces — defensive
    re-merge avoids losing the tail).
    """
    fields: dict[str, str] = {}
    tokens = block.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok:
            key, _, val = tok.partition("=")
            # Greedy continuation: if the next token has no '=' it's a tail.
            j = i + 1
            while j < len(tokens) and "=" not in tokens[j]:
                val = val + " " + tokens[j]
                j += 1
            fields[key] = val
            i = j
        else:
            i += 1
    return fields


def parse_scontrol_show_node(text: str) -> list[NodeSnapshot]:
    """Parse ``scontrol show node`` output into NodeSnapshot rows.

    SLURM separates nodes with a blank line. Each node's fields appear as
    ``Key=Value`` whitespace-separated tokens, possibly across multiple
    lines.
    """
    snapshots: list[NodeSnapshot] = []
    if not text:
        return snapshots
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        if not block.strip():
            continue
        fields = _parse_scontrol_kv_block(block)
        name = fields.get("NodeName", "").strip()
        if not name:
            continue
        snap = NodeSnapshot(name=name)
        snap.state = fields.get("State", "")
        snap.real_mem_mb = _to_int_or_none(fields.get("RealMemory"))
        snap.alloc_mem_mb = _to_int_or_none(fields.get("AllocMem"))
        if snap.real_mem_mb and snap.alloc_mem_mb is not None and snap.real_mem_mb > 0:
            snap.alloc_mem_pct = round(snap.alloc_mem_mb / snap.real_mem_mb, 4)
        snap.cpu_tot = _to_int_or_none(fields.get("CPUTot"))
        snap.cpu_alloc = _to_int_or_none(fields.get("CPUAlloc"))
        load = _to_float_or_none(fields.get("CPULoad"))
        snap.cpu_load = load
        if load is not None and snap.cpu_tot:
            snap.cpu_load_frac = round(load / max(snap.cpu_tot, 1), 4)
        snap.gres = fields.get("Gres", "")
        snap.gres_used = fields.get("GresUsed", "")
        af = fields.get("ActiveFeatures", "")
        snap.active_features = [f.strip() for f in af.split(",") if f.strip()]
        snap.is_drained = "DRAIN" in snap.state.upper() or "DOWN" in snap.state.upper()
        snapshots.append(snap)
    return snapshots


def parse_sacct_node_jobs(text: str, *, recent_only: bool = True) -> list[dict[str, Any]]:
    """Parse ``sacct -N <node> -P --noheader`` output into a co-tenant list.

    Expected format::

        JobID|User|State|ReqCPUS|ReqMem|Start|Elapsed|AllocTRES

    We surface ``{user, job_id, cpus, mem_gb, started_h_ago, elapsed_s, gpus, state}``
    so the planner can reason about who's there and how long they've been
    running.

    *recent_only*: when True (default), drop rows already in a terminal
    state — the planner only cares about live contention. Pass False when
    using sacct history for ``p_fail`` calculations.
    """
    rows: list[dict[str, Any]] = []
    if not text:
        return rows
    terminal = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL"}
    seen_jobs: set[str] = set()
    for line in text.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 7:
            continue
        job_field = parts[0]
        # Drop step rows (12345.batch / 12345.extern) — keep only top-level.
        base_job = job_field.split(".", 1)[0]
        # Strip array-task suffix for de-dup of co-tenant listing.
        if "_" in base_job:
            dedup_key = base_job
        else:
            dedup_key = base_job
        if dedup_key in seen_jobs:
            continue
        seen_jobs.add(dedup_key)
        user = parts[1].strip()
        state = parts[2].strip().split()[0] if parts[2].strip() else ""
        if recent_only and state in terminal:
            continue
        cpus = _to_int_or_none(parts[3]) or 0
        mem_text = parts[4].strip()
        mem_gb = _parse_mem_to_gb(mem_text)
        start_text = parts[5].strip()
        started_h_ago = _hours_since(start_text)
        elapsed = _parse_elapsed_to_sec(parts[6].strip()) if len(parts) > 6 else 0
        alloc_tres = parts[7].strip() if len(parts) > 7 else ""
        gpus = _parse_gpu_count_from_tres(alloc_tres)
        rows.append(
            {
                "user": user,
                "job_id": base_job,
                "state": state,
                "cpus": cpus,
                "mem_gb": mem_gb,
                "started_h_ago": started_h_ago,
                "elapsed_s": elapsed,
                "gpus": gpus,
            }
        )
    return rows


def _slurm_inspect(
    cluster_name: str,
    cluster_cfg: dict[str, Any],
    *,
    sacct_window_hours: int,
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
    runner: _CommandRunner,
) -> ClusterSnapshot:
    errors: list[dict[str, str]] = []
    snap = ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind="slurm",
        now_iso=utcnow_iso(),
        nodes=[],
    )
    # Step 1: scontrol show node (all nodes; planner filters by candidate
    # pool downstream rather than us pre-filtering here).
    scontrol_rc, scontrol_out, scontrol_err = runner.run("scontrol show node")
    if scontrol_rc != 0:
        errors.append({"code": "scontrol_failed", "detail": scontrol_err.strip()[:500]})
        snap.errors = errors
        return snap
    snap.nodes = parse_scontrol_show_node(scontrol_out)

    # Step 2: per-node sacct co-tenant lookup. Single batched call:
    # `sacct -N node1,node2,... -S -<H>hours -P --noheader -X` with one row
    # per allocation.
    node_names = [n.name for n in snap.nodes if not n.is_drained]
    if node_names:
        nodelist = ",".join(node_names)
        cmd = (
            f"sacct -N {nodelist} -S now-{sacct_window_hours}hours "
            "-P --noheader -X "
            "--format=JobID,User,State,ReqCPUS,ReqMem,Start,Elapsed,AllocTRES,NodeList"
        )
        sacct_rc, sacct_out, sacct_err = runner.run(cmd)
        if sacct_rc == 0:
            tenants_by_node = _bucket_tenants_by_node(sacct_out)
            for n in snap.nodes:
                n.co_tenants = tenants_by_node.get(n.name, [])
        else:
            errors.append({"code": "sacct_failed", "detail": sacct_err.strip()[:500]})

    # Step 3: stress flag.
    for n in snap.nodes:
        n.is_stressed = _is_stressed(n, stress_alloc_mem_pct, stress_cpu_load_frac)

    snap.errors = errors
    return snap


def _bucket_tenants_by_node(sacct_out: str) -> dict[str, list[dict[str, Any]]]:
    """Bucket sacct rows by node from the trailing NodeList column.

    Expects ``sacct -X ... --format=...,NodeList`` so each row carries the
    nodes it ran on in column 9. Step rows (``.batch``/``.extern``) and
    terminal-state rows are filtered out — only live contention matters
    for the planner. NodeList values like ``d11-[03,07]`` are expanded
    defensively; on parse failure the raw string is used as the node
    name so the row is still attributed somewhere.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    if not sacct_out:
        return out
    seen_jobs: set[str] = set()
    terminal = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL"}
    for line in sacct_out.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 8:
            continue
        job_field = parts[0]
        base_job = job_field.split(".", 1)[0]
        if base_job in seen_jobs:
            continue
        seen_jobs.add(base_job)
        state = parts[2].strip().split()[0] if parts[2].strip() else ""
        if state in terminal:
            continue
        cpus = _to_int_or_none(parts[3]) or 0
        mem_gb = _parse_mem_to_gb(parts[4].strip())
        started_h_ago = _hours_since(parts[5].strip())
        elapsed = _parse_elapsed_to_sec(parts[6].strip())
        alloc_tres = parts[7].strip() if len(parts) > 7 else ""
        node_list_raw = parts[8].strip() if len(parts) > 8 else ""
        nodes = _expand_slurm_nodelist(node_list_raw)
        gpus = _parse_gpu_count_from_tres(alloc_tres)
        record = {
            "user": parts[1].strip(),
            "job_id": base_job,
            "state": state,
            "cpus": cpus,
            "mem_gb": mem_gb,
            "started_h_ago": started_h_ago,
            "elapsed_s": elapsed,
            "gpus": gpus,
        }
        for node in nodes:
            out.setdefault(node, []).append(record)
    return out


def _expand_slurm_nodelist(spec: str) -> list[str]:
    """Expand a SLURM hostlist spec like ``d11-[03,07-09]`` into names.

    Permissive: if the spec doesn't match, returns ``[spec]`` so the
    caller still has something to attribute the row to.
    """
    if not spec:
        return []
    if "[" not in spec:
        return [s.strip() for s in spec.split(",") if s.strip()]
    m = re.match(r"^([^\[]+)\[([^\]]+)\](.*)$", spec)
    if not m:
        return [spec]
    prefix, body, suffix = m.group(1), m.group(2), m.group(3)
    out: list[str] = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                width = max(len(lo), len(hi))
                for i in range(int(lo), int(hi) + 1):
                    out.append(f"{prefix}{str(i).zfill(width)}{suffix}")
            except ValueError:
                out.append(f"{prefix}{chunk}{suffix}")
        else:
            out.append(f"{prefix}{chunk}{suffix}")
    return out


# --- SGE ------------------------------------------------------------------


def _sge_inspect(
    cluster_name: str,
    cluster_cfg: dict[str, Any],
    *,
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
    runner: _CommandRunner,
) -> ClusterSnapshot:
    """SGE inspection via qhost (resource state) + qstat (co-tenants).

    SGE has weaker structured output than SLURM; this implementation
    captures the same shape but populates fewer fields. The planner
    treats missing fields as "unknown" rather than "fine".
    """
    errors: list[dict[str, str]] = []
    snap = ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind="sge",
        now_iso=utcnow_iso(),
        nodes=[],
    )
    rc, out, err = runner.run("qhost -F gpu -q")
    if rc != 0:
        errors.append({"code": "qhost_failed", "detail": err.strip()[:500]})
        snap.errors = errors
        return snap
    snap.nodes = _parse_qhost(out)

    # qstat for live co-tenants.
    rc2, out2, err2 = runner.run("qstat -u '*' -F gpu")
    if rc2 == 0:
        tenants_by_node = _parse_qstat_full(out2)
        for n in snap.nodes:
            n.co_tenants = tenants_by_node.get(n.name, [])
    else:
        errors.append({"code": "qstat_failed", "detail": err2.strip()[:500]})

    for n in snap.nodes:
        n.is_stressed = _is_stressed(n, stress_alloc_mem_pct, stress_cpu_load_frac)
    snap.errors = errors
    return snap


def _parse_qhost(text: str) -> list[NodeSnapshot]:
    """Parse ``qhost -F`` columnar output.

    Output format::

        HOSTNAME  ARCH  NCPU NSOC NCOR NTHR  LOAD  MEMTOT  MEMUSE  SWAPTO  SWAPUS

    Lines starting with whitespace describe queue / resource attributes
    of the most recent host. Permissive parser — unrecognized hosts get
    minimal fields, never raises.
    """
    nodes: list[NodeSnapshot] = []
    current: NodeSnapshot | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("HOSTNAME") or line.startswith("---") or line.startswith("global"):
            current = None
            continue
        if not line[0].isspace():
            cols = line.split()
            if len(cols) < 8:
                continue
            host = cols[0]
            current = NodeSnapshot(name=host)
            current.cpu_tot = _to_int_or_none(cols[2])
            current.cpu_load = _to_float_or_none(cols[6])
            if current.cpu_load is not None and current.cpu_tot:
                current.cpu_load_frac = round(current.cpu_load / max(current.cpu_tot, 1), 4)
            current.real_mem_mb = _parse_mem_to_mb(cols[7])
            mem_used = _parse_mem_to_mb(cols[8])
            if current.real_mem_mb and mem_used is not None and current.real_mem_mb > 0:
                current.alloc_mem_mb = mem_used
                current.alloc_mem_pct = round(mem_used / current.real_mem_mb, 4)
            nodes.append(current)
        else:
            # Resource attribute line for the current host. Accepts both
            # the prefixed form (e.g. ``hl:gpu=2``, ``gl:gpu_used=1``)
            # used by most SGE installs and the bare form (``gpu=2``)
            # some clusters emit. Two scoped searches so a line that
            # carries both routes each value to the correct field —
            # substring-checking the whole line miscategorizes one of
            # the two values.
            if current is None:
                continue
            text_line = line.strip()
            m_used = re.search(r"(?:[A-Za-z]+:)?gpu_used=(\S+)", text_line)
            if m_used:
                current.gres_used = f"gpu:{m_used.group(1)}"
            m_free = re.search(r"(?<![A-Za-z_])(?:[A-Za-z]+:)?gpu=(\S+)", text_line)
            if m_free:
                current.gres = f"gpu:{m_free.group(1)}"
    return nodes


def _parse_qstat_full(text: str) -> dict[str, list[dict[str, Any]]]:
    """Best-effort parse of ``qstat -u '*' -F`` for co-tenants per host.

    SGE's qstat output here is queue-instance-oriented, not host-oriented;
    we map ``queue@host`` → host. Returns minimal info — SGE does not
    expose start time in this view.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    current_host: str | None = None
    seen_jobs_per_host: dict[str, set[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first_token = stripped.split()[0]
        # Queue-instance header lines look like:
        # all.q@compute-001.local             BIP   0/4/16         1.23     ...
        if "@" in first_token:
            current_host = first_token.split("@", 1)[1].split(".", 1)[0]
            seen_jobs_per_host.setdefault(current_host, set())
            continue
        # Job lines look like (after the queue-instance header):
        # 12345 0.50 jobname  user  r  ...
        if current_host and re.match(r"^\d+\s+", stripped):
            cols = stripped.split()
            if len(cols) < 5:
                continue
            jid = cols[0]
            if jid in seen_jobs_per_host[current_host]:
                continue
            seen_jobs_per_host[current_host].add(jid)
            user = cols[3] if len(cols) > 3 else ""
            state = cols[4] if len(cols) > 4 else ""
            cpus = 0
            if len(cols) > 8:
                cpus = _to_int_or_none(cols[-1]) or 0
            out.setdefault(current_host, []).append(
                {
                    "user": user,
                    "job_id": jid,
                    "state": state,
                    "cpus": cpus,
                    "mem_gb": None,
                    "started_h_ago": None,
                    "elapsed_s": None,
                    "gpus": None,
                }
            )
    return out


# --- runner abstraction (testable) ----------------------------------------


class _CommandRunner:
    """Minimal abstraction over ``ssh_run`` for unit testing.

    Tests substitute a fake runner that returns canned stdout/stderr; the
    real one shells out via ssh. We deliberately avoid threading the
    ``hpc_mapreduce.infra.remote`` import through every call site so the
    pure parser tests don't need SSH keys.
    """

    def __init__(self, *, host: str | None, user: str | None, timeout: float = 60.0):
        self.host = host
        self.user = user
        self.timeout = timeout

    def run(self, cmd: str) -> tuple[int, str, str]:
        if self.host is None or self.user is None:
            # Local probe — used by tests in CI that mock subprocess.
            try:
                cp = subprocess.run(
                    cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                return cp.returncode, cp.stdout or "", cp.stderr or ""
            except subprocess.TimeoutExpired as exc:
                return 124, "", f"timeout: {exc}"
            except FileNotFoundError as exc:
                return 127, "", f"missing binary: {exc}"
        from hpc_mapreduce.infra.remote import ssh_run

        try:
            cp = ssh_run(cmd, host=self.host, user=self.user, timeout=self.timeout)
            return cp.returncode, cp.stdout or "", cp.stderr or ""
        except TimeoutError as exc:
            return 124, "", str(exc)
        except FileNotFoundError as exc:
            # ssh binary missing on this host — same shape as a remote
            # `command not found` so callers don't need a separate branch.
            return 127, "", f"missing binary: {exc}"
        except OSError as exc:
            # Other OS-level failures (broken pipe, etc.) — surface as a
            # generic non-zero rather than letting them propagate.
            return 1, "", f"os error: {exc}"


# --- public entry ---------------------------------------------------------


@primitive(
    name="inspect-cluster",
    verb="query",
    side_effects=[SideEffect("ssh", "<cluster>")],
    error_codes=[errors.ClusterUnknown, errors.SshUnreachable],
    idempotent=True,
    idempotency_key="cluster",
)
def inspect_cluster(
    cluster_name: str,
    *,
    config_path: str | Path | None = None,
    sacct_window_hours: int = 24,
    stress_alloc_mem_pct: float = 0.80,
    stress_cpu_load_frac: float = 0.80,
    use_cache: bool = True,
    runner: _CommandRunner | None = None,
    persist_dir: Path | None = None,
) -> ClusterSnapshot:
    """Return a :class:`ClusterSnapshot` for *cluster_name*.

    Reads ``clusters.yaml`` to determine SSH target and scheduler kind.
    Caches the result for 60s in-process so a single submit cycle that
    re-asks (e.g. after a canary run) doesn't pay the SSH cost twice.

    Stress thresholds are tunable so the planner can experiment with
    cost-function knobs without code changes.
    """
    clusters = load_clusters_config(Path(config_path) if config_path is not None else None)
    if cluster_name not in clusters:
        raise errors.ClusterUnknown(
            f"unknown cluster {cluster_name!r}; check clusters.yaml"
        )
    cfg = clusters[cluster_name]
    scheduler = (cfg.get("scheduler") or "slurm").lower()
    cache_key = (cluster_name, scheduler)
    if use_cache:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return _snapshot_from_dict(cached)
    if runner is None:
        runner = _CommandRunner(host=cfg.get("host"), user=cfg.get("user"))
    # B5-PR2: dispatch through the backend registry. Each backend's
    # ``inspect_cluster`` classmethod normalises kwargs for its scheduler
    # (e.g. SGE ignores ``sacct_window_hours``); a missing backend
    # raises ValueError just like the prior ladder did.
    from hpc_mapreduce.infra.backends import get_backend_class
    try:
        backend_cls = get_backend_class(scheduler)
    except ValueError as exc:
        raise ValueError(
            f"unsupported scheduler {scheduler!r} for cluster {cluster_name!r}"
        ) from exc
    snap = backend_cls.inspect_cluster(
        cluster_name,
        cfg,
        sacct_window_hours=sacct_window_hours,
        stress_alloc_mem_pct=stress_alloc_mem_pct,
        stress_cpu_load_frac=stress_cpu_load_frac,
        runner=runner,
    )
    if use_cache:
        _CACHE.put(cache_key, snap.to_dict())
    if persist_dir is not None:
        # Best-effort: a snapshot persistence failure must not blow up
        # the planning pipeline. We only emit the file under a real
        # experiment dir; tests pass tmp_path directly.
        try:
            persist_snapshot(persist_dir, snap)
        except OSError:
            pass
    return snap


# --- history persistence --------------------------------------------------

# Per-cluster snapshot cap. Same bounded-growth pattern as
# `runtime_prior.MAX_SAMPLES`: the history is advisory not audit, so
# trimming oldest-first is fine. Override via HPC_MAX_CLUSTER_HISTORY.
MAX_HISTORY_SNAPSHOTS: int = int(os.environ.get("HPC_MAX_CLUSTER_HISTORY", "10000"))


def _history_dir(experiment_dir: Path, cluster: str) -> Path:
    from claude_hpc._internal.layout import RepoLayout

    return RepoLayout(experiment_dir).cluster_history(cluster)


def persist_snapshot(experiment_dir: Path, snap: ClusterSnapshot) -> Path:
    """Persist *snap* under ``<exp>/.hpc/cluster_history/<cluster>/<unix_ts>.json``.

    Atomic write (``tempfile`` + :func:`os.replace`) so a reader that
    arrives mid-write either sees the previous snapshot list or the new
    one — never a partial JSON document. Returns the file path written.

    Bounded growth: after writing, the directory is trimmed to the
    most-recent :data:`MAX_HISTORY_SNAPSHOTS` files (oldest-first
    eviction). Same pattern as ``runtime_prior``'s sample list cap.

    Filename uses Unix timestamp seconds (sortable, no path-separator
    concerns). When two snapshots arrive in the same second we suffix
    ``-N`` to break ties — this is best-effort and the planner does not
    need second-resolution precision.
    """
    d = _history_dir(experiment_dir, snap.cluster)
    ts = parse_iso_utc_or_none(snap.now_iso)
    if ts is not None:
        unix_ts = int(ts.timestamp())
    else:
        unix_ts = int(utcnow().timestamp())
    base = d / f"{unix_ts}.json"
    target = base
    counter = 1
    while target.exists():
        target = d / f"{unix_ts}-{counter}.json"
        counter += 1
    payload = json.dumps(snap.to_dict(), indent=2, sort_keys=True)
    tmp = tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=str(d),
        prefix=target.name + ".",
        suffix=".tmp",
        encoding="utf-8",
    )
    try:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, target)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    finally:
        if not tmp.closed:
            tmp.close()
    _prune_history(d, MAX_HISTORY_SNAPSHOTS)
    return target


def _prune_history(d: Path, limit: int) -> None:
    """Delete oldest snapshot files until at most *limit* remain.

    Sorts by filename so the embedded unix-ts orders chronologically.
    Best-effort: an unlink that races with another writer is ignored.
    """
    if limit <= 0:
        return
    try:
        files = sorted(p for p in d.iterdir() if p.suffix == ".json" and p.is_file())
    except OSError:
        return
    excess = len(files) - limit
    if excess <= 0:
        return
    for p in files[:excess]:
        try:
            p.unlink()
        except OSError:
            continue


def read_cluster_history(
    experiment_dir: Path,
    cluster: str,
    *,
    since_iso: str | None = None,
    limit: int | None = None,
) -> Iterator[ClusterSnapshot]:
    """Yield persisted snapshots in reverse-chronological order.

    *since_iso* (optional): filter out snapshots whose ``now_iso`` is
    strictly older than *since_iso*. Unparseable timestamps on either
    side fall through (returned).

    *limit* (optional): yield at most this many. Applied after the
    ``since_iso`` filter so callers asking for "the most recent N" get
    the most recent N matching snapshots.

    Files that fail to parse as JSON or lack the expected shape are
    silently skipped — same permissive-read posture as the rest of this
    module.
    """
    d = _history_dir(experiment_dir, cluster)
    try:
        files = sorted(
            (p for p in d.iterdir() if p.suffix == ".json" and p.is_file()),
            reverse=True,
        )
    except OSError:
        return
    since_dt = parse_iso_utc_or_none(since_iso) if since_iso else None
    yielded = 0
    for p in files:
        try:
            text = p.read_text()
        except OSError:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if since_dt is not None:
            ts = parse_iso_utc_or_none(doc.get("now_iso"))
            if ts is not None and ts < since_dt:
                continue
        try:
            snap = _snapshot_from_dict(doc)
        except (KeyError, TypeError):
            continue
        yield snap
        yielded += 1
        if limit is not None and yielded >= limit:
            return


def _snapshot_from_dict(d: dict[str, Any]) -> ClusterSnapshot:
    nodes = [NodeSnapshot(**{**n}) for n in d.get("nodes", [])]
    return ClusterSnapshot(
        cluster=d["cluster"],
        scheduler_kind=d["scheduler_kind"],
        now_iso=d["now_iso"],
        nodes=nodes,
        errors=list(d.get("errors", [])),
    )


# --- helpers --------------------------------------------------------------


def _to_int_or_none(s: Any) -> int | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.match(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _to_float_or_none(s: Any) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_mem_to_gb(s: str) -> float | None:
    """Parse a SLURM/SGE memory token (e.g. ``128G``, ``1024M``) → GB."""
    if not s:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt])?[bB]?", s.strip())
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "M").upper()
    factor = {"K": 1 / (1024 * 1024), "M": 1 / 1024, "G": 1.0, "T": 1024.0}.get(unit, 1 / 1024)
    return round(val * factor, 3)


def _parse_mem_to_mb(s: str) -> int | None:
    gb = _parse_mem_to_gb(s)
    if gb is None:
        return None
    return int(round(gb * 1024))


_ELAPSED_RE = re.compile(
    r"^(?:(?P<d>\d+)-)?(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})(?:\.\d+)?$"
)


def _parse_elapsed_to_sec(s: str) -> int:
    """Parse a SLURM elapsed string (``D-HH:MM:SS`` or ``HH:MM:SS``) → seconds."""
    if not s:
        return 0
    m = _ELAPSED_RE.match(s.strip())
    if not m:
        return 0
    days = int(m.group("d") or 0)
    return days * 86400 + int(m.group("h")) * 3600 + int(m.group("m")) * 60 + int(m.group("s"))


def _hours_since(iso_or_slurm: str) -> float | None:
    """Return hours elapsed since a SLURM-style start timestamp.

    SLURM emits ``2026-01-01T15:23:00`` (no zone). Treat as UTC for
    planning — this is "rough age" not audit-grade timing. Returns
    ``None`` on parse failure.
    """
    if not iso_or_slurm or iso_or_slurm in ("Unknown", "None"):
        return None
    ts = parse_iso_utc_or_none(iso_or_slurm)
    if ts is None:
        return None
    delta = utcnow() - ts
    return round(delta.total_seconds() / 3600.0, 2)


def _parse_gpu_count_from_tres(tres: str) -> int:
    """Re-export from ``backends.query`` to keep the parser single-sourced."""
    from hpc_mapreduce.infra.backends.query import parse_gpu_count_from_tres

    return parse_gpu_count_from_tres(tres)


def _is_stressed(
    n: NodeSnapshot,
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
) -> bool:
    if n.is_drained:
        return False  # drained is reported separately, not as stressed
    if n.alloc_mem_pct is not None and n.alloc_mem_pct >= stress_alloc_mem_pct:
        return True
    if n.cpu_load_frac is not None and n.cpu_load_frac >= stress_cpu_load_frac:
        return True
    return False
