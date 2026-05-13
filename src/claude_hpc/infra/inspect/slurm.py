"""SLURM-specific cluster inspection.

Parses ``scontrol show node`` and ``sacct -P`` output into the shared
:class:`~claude_hpc.infra.inspect._common.NodeSnapshot` /
:class:`~claude_hpc.infra.inspect._common.ClusterSnapshot` shapes.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from claude_hpc._internal.time import utcnow_iso
from claude_hpc.infra.parsing import (
    parse_mem_to_gb as _parse_mem_to_gb,
)
from claude_hpc.infra.parsing import (
    parse_sacct_pipe_row,
)
from claude_hpc.infra.parsing import (
    parse_walltime_to_sec as _parse_elapsed_to_sec,
)
from claude_hpc.infra.parsing import (
    to_float_or_none as _to_float_or_none,
)
from claude_hpc.infra.parsing import (
    to_int_or_none as _to_int_or_none,
)

from ._common import (
    ClusterSnapshot,
    NodeSnapshot,
    _CommandRunner,
    _hours_since,
    _is_stressed,
    _parse_gpu_count_from_tres,
)

__all__ = [
    "parse_scontrol_show_node",
    "parse_sacct_node_jobs",
    "_slurm_inspect",
    "_bucket_tenants_by_node",
    "_expand_slurm_nodelist",
]


# sacct ``--format=`` lists, kept here so the parser and the command
# string never drift out of sync.
_SACCT_NODE_JOBS_FORMAT: list[str] = [
    "JobID",
    "User",
    "State",
    "ReqCPUS",
    "ReqMem",
    "Start",
    "Elapsed",
    "AllocTRES",
]
_SACCT_BUCKET_FORMAT: list[str] = [*_SACCT_NODE_JOBS_FORMAT, "NodeList"]


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
        row = parse_sacct_pipe_row(parts, _SACCT_NODE_JOBS_FORMAT)
        # Drop step rows (12345.batch / 12345.extern) — keep only top-level.
        base_job = row["JobID"].split(".", 1)[0]
        if base_job in seen_jobs:
            continue
        seen_jobs.add(base_job)
        state = row["State"].split()[0] if row["State"] else ""
        if recent_only and state in terminal:
            continue
        cpus = _to_int_or_none(row["ReqCPUS"]) or 0
        mem_gb = _parse_mem_to_gb(row["ReqMem"], cpus=cpus)
        started_h_ago = _hours_since(row["Start"])
        elapsed = _parse_elapsed_to_sec(row["Elapsed"])
        gpus = _parse_gpu_count_from_tres(row["AllocTRES"])
        rows.append(
            {
                "user": row["User"],
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
        # Quote each name before joining — runner.run goes through the
        # shell, so a hypothetical node name containing ';' would
        # otherwise execute follow-on commands.
        nodelist = shlex.quote(",".join(node_names))
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
        # _SACCT_BUCKET_FORMAT has 9 fields ending in NodeList; a row
        # with exactly 8 would silently strip NodeList and drop the
        # row from co-tenant attribution.
        if len(parts) < 9:
            continue
        row = parse_sacct_pipe_row(parts, _SACCT_BUCKET_FORMAT)
        base_job = row["JobID"].split(".", 1)[0]
        if base_job in seen_jobs:
            continue
        seen_jobs.add(base_job)
        state = row["State"].split()[0] if row["State"] else ""
        if state in terminal:
            continue
        cpus = _to_int_or_none(row["ReqCPUS"]) or 0
        mem_gb = _parse_mem_to_gb(row["ReqMem"], cpus=cpus)
        started_h_ago = _hours_since(row["Start"])
        elapsed = _parse_elapsed_to_sec(row["Elapsed"])
        nodes = _expand_slurm_nodelist(row["NodeList"])
        gpus = _parse_gpu_count_from_tres(row["AllocTRES"])
        record = {
            "user": row["User"],
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

    Supports multi-group specs like ``cn[01-02],cn[10]`` by splitting on
    top-level commas (commas outside brackets) and recursing per chunk.
    Permissive: if a chunk doesn't match, the chunk itself is appended so
    the caller still has something to attribute the row to.
    """
    if not spec:
        return []
    # Split on top-level commas only (commas inside ``[...]`` are part of
    # a range body and must be preserved).
    chunks: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            chunks.append(spec[start:i])
            start = i + 1
    chunks.append(spec[start:])

    out: list[str] = []
    for raw in chunks:
        chunk = raw.strip()
        if not chunk:
            continue
        if "[" not in chunk:
            out.append(chunk)
            continue
        m = re.match(r"^([^\[]+)\[([^\]]+)\](.*)$", chunk)
        if not m:
            out.append(chunk)
            continue
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        for sub in body.split(","):
            sub = sub.strip()
            if "-" in sub:
                lo, _, hi = sub.partition("-")
                try:
                    width = max(len(lo), len(hi))
                    for i in range(int(lo), int(hi) + 1):
                        out.append(f"{prefix}{str(i).zfill(width)}{suffix}")
                except ValueError:
                    out.append(f"{prefix}{sub}{suffix}")
            else:
                out.append(f"{prefix}{sub}{suffix}")
    return out
