"""PBS (PBS Pro / OpenPBS + TORQUE) cluster inspection.

Populates a per-node :class:`ClusterSnapshot` from ``pbsnodes`` output so
the planner gets the same backfill / throughput signals (cpu/mem/alloc/
state per node) it gets from SLURM ``scontrol``. The grammar diverges
between PBS Pro and TORQUE, so the parser is keyed on ``family``
(``pbspro`` vs ``torque``), mirroring the per-family split already used
for the submit grammar and the history query:

- **PBS Pro** (``pbsnodes -av``): one stanza per node, with
  ``resources_available.ncpus`` / ``resources_available.mem`` /
  ``resources_assigned.*`` and a ``state = free|job-busy|offline|down``
  line.
- **TORQUE** (``pbsnodes -a``): one stanza per node, with ``np = N``, a
  packed ``status = ...,ncpus=...,physmem=...kb,availmem=...kb,...`` line,
  and a ``state = free|job-exclusive|down|offline`` line.

When ``pbsnodes`` is unavailable, errors, or yields nothing parseable,
the inspect path degrades to a structurally-valid *minimal* snapshot
(``nodes=[]`` plus a single ``pbs_inspect_minimal`` diagnostic note)
rather than raising — the planner treats the missing node fields as
"unknown" (conservative), so submit + live monitoring still proceed.
That fallback is what makes a PBS cluster's ``inspect`` / planning path
degrade safely even when node enumeration is impossible.
"""

from __future__ import annotations

import re
from typing import Any

from hpc_agent.infra.parsing import (
    parse_mem_to_mb as _parse_mem_to_mb,
)
from hpc_agent.infra.parsing import (
    to_float_or_none as _to_float_or_none,
)
from hpc_agent.infra.parsing import (
    to_int_or_none as _to_int_or_none,
)
from hpc_agent.infra.time import utcnow_iso

from ._common import (
    ClusterSnapshot,
    NodeSnapshot,
    _CommandRunner,
    _is_stressed,
    _parse_max_nodes,
    _split_section,
)

__all__ = ["_pbs_inspect", "parse_pbsnodes", "parse_qstat_co_tenants", "parse_qstat_queues"]


# PBS node states that mean the node is not usable capacity (down / drained
# equivalent). Anything else — ``free``, ``job-busy``, ``job-exclusive``,
# ``busy``, ``resv-exclusive`` — is "up"; how *full* it is is expressed
# through the alloc fields, not this flag (mirrors how SLURM only flags
# DRAIN/DOWN as ``is_drained`` and lets AllocMem/CPUAlloc carry busy-ness).
_PBS_UNAVAILABLE_STATES = frozenset(
    {
        "down",
        "offline",
        "unknown",
        "state-unknown",
        "stale",
        "provisioning",
        "wait-provisioning",
        "maintenance",
    }
)


def _state_is_unavailable(state: str) -> bool:
    """True if a (possibly comma-joined) PBS state marks the node unusable."""
    tokens = {t.strip().lower() for t in state.replace(";", ",").split(",")}
    return bool(tokens & _PBS_UNAVAILABLE_STATES)


def _pbs_inspect(
    cluster_name: str,
    cluster_cfg: dict[str, Any],
    *,
    scheduler_kind: str = "pbspro",
    stress_alloc_mem_pct: float,
    stress_cpu_load_frac: float,
    runner: _CommandRunner,
) -> ClusterSnapshot:
    """Return a :class:`ClusterSnapshot` for a PBS cluster.

    Probes ``pbsnodes`` (family-shaped) and populates per-node capacity;
    on any failure path — no runner, non-zero exit, empty/unparseable
    output — degrades to the minimal snapshot with a ``pbs_inspect_minimal``
    note (see module docstring) rather than raising.
    """
    family = scheduler_kind if scheduler_kind in ("pbspro", "torque") else "pbspro"

    if runner is None:
        return _minimal_snapshot(
            cluster_name,
            scheduler_kind,
            "no command runner available to probe pbsnodes",
        )

    # PBS Pro needs ``-av`` (attributes, all nodes); TORQUE's ``pbsnodes``
    # prints the same per-node stanzas with ``-a``. Co-tenant context (which
    # other users' jobs share a node) comes from ``qstat -an1`` — a SECOND probe
    # PBS inspect previously lacked entirely (it only ran pbsnodes), so a PBS
    # cluster got none of the co-tenant signal SLURM/SGE surface. The two are
    # independent reads, so they ride ONE merged ssh round-trip (echo-delimited
    # sections) — applying the #295 batching lesson from the start. ``qstat``'s
    # output is best-effort: a failure leaves co_tenants empty, never raises.
    pbsnodes_cmd = "pbsnodes -av" if family == "pbspro" else "pbsnodes -a"
    combined = (
        f"echo __HPC_PBSNODES__; {pbsnodes_cmd}; echo __HPC_PBSNODES_RC__=$?; "
        "echo __HPC_QSTAT__; qstat -an1 2>/dev/null; echo __HPC_QSTAT_RC__=$?; "
        "echo __HPC_QUEUES__; qstat -Qf 2>/dev/null; echo __HPC_QUEUES_RC__=$?"
    )
    rc_all, out_all, err = runner.run(combined)
    rc, out = _split_section(out_all, "__HPC_PBSNODES__", "__HPC_PBSNODES_RC__")
    _qstat_rc, qstat_out = _split_section(out_all, "__HPC_QSTAT__", "__HPC_QSTAT_RC__")
    _queues_rc, queues_out = _split_section(out_all, "__HPC_QUEUES__", "__HPC_QUEUES_RC__")
    # Markers absent → round-trip died before the shell ran; fall back so the
    # pbsnodes-failure branch still fires on the combined result.
    if rc is None:
        rc, out = rc_all, out_all

    if rc != 0:
        return _minimal_snapshot(
            cluster_name,
            scheduler_kind,
            f"`{pbsnodes_cmd}` failed (rc={rc}): {err.strip()[:300]}",
        )

    nodes = parse_pbsnodes(out, family=family)
    if not nodes:
        return _minimal_snapshot(
            cluster_name,
            scheduler_kind,
            f"`{pbsnodes_cmd}` returned no parseable node stanzas",
        )

    tenants_by_node = parse_qstat_co_tenants(qstat_out)
    for n in nodes:
        # Match on the bare hostname: pbsnodes may report FQDNs while qstat's
        # exec_host uses short names (or vice versa); normalize both sides.
        n.co_tenants = tenants_by_node.get(n.name.split(".")[0], [])
        n.is_stressed = _is_stressed(n, stress_alloc_mem_pct, stress_cpu_load_frac)

    return ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind=scheduler_kind,
        now_iso=utcnow_iso(),
        nodes=nodes,
        errors=[],
        parallel_environments=parse_qstat_queues(queues_out, family=family),
    )


# A qstat -an1 data row opens with a job id: ``123``, ``123.server``,
# ``123[].server`` (array parent) or ``123[4].server`` (array subjob).
_QSTAT_JOBID_RE = re.compile(r"^\d+(\[\d*\])?(\.\S+)?$")


def _exec_host_cpus(exec_host: str) -> dict[str, int]:
    """Map each node in a PBS ``exec_host`` spec to the core count placed there.

    ``exec_host`` is ``host/range[*count]`` segments joined by ``+`` —
    ``node01/0*4`` (4 cores on node01), ``node02/0*4+node03/0*4`` (4 each), or
    ``node01/0+node01/1`` (1+1 → 2 on node01). Hosts are normalized to their
    bare first label so they match pbsnodes' node names.
    """
    counts: dict[str, int] = {}
    for seg in exec_host.split("+"):
        seg = seg.strip()
        if "/" not in seg:
            continue
        host = seg.split("/", 1)[0].split(".")[0]
        if not host:
            continue
        # cores on this host: PBS Pro ``/0*4`` → 4; a TORQUE-style range
        # ``/0-3`` → 4; otherwise a single core (``/0``) → 1.
        star = re.search(r"\*(\d+)", seg)
        if star:
            cpus = int(star.group(1))
        else:
            rng = re.search(r"/(\d+)-(\d+)", seg)
            cpus = (int(rng.group(2)) - int(rng.group(1)) + 1) if rng else 1
        counts[host] = counts.get(host, 0) + cpus
    return counts


def parse_qstat_co_tenants(text: str) -> dict[str, list[dict[str, Any]]]:
    """Bucket ``qstat -an1`` rows into co-tenants per (bare) node name.

    The ``-an1`` layout puts each job on one line ending in its ``exec_host``
    (``node01/0*4+...``); column order is JobID, Username, Queue, Jobname,
    SessID, NDS, TSK, Memory, Req'd-Time, S(tate), Elap-Time, exec_host. We
    anchor on the job-id-shaped first token and the trailing host-bearing token,
    so header/separator/``server:`` lines are skipped. Queued/held jobs (no
    exec_host) carry no node placement and are omitted. Permissive: an
    unparseable row is skipped, never raised — the same posture as parse_pbsnodes.

    Returns ``{bare_node: [{user, job_id, state, cpus, mem_gb, started_h_ago,
    elapsed_s, gpus}]}`` (None for fields qstat -an1 doesn't expose per-node),
    matching the co_tenant shape SLURM/SGE emit.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        cols = line.split()
        if len(cols) < 4 or not _QSTAT_JOBID_RE.match(cols[0]):
            continue
        exec_host = cols[-1]
        if "/" not in exec_host:
            continue  # queued/held — not placed on a node yet
        user = cols[1]
        # State is the single-char column just before Elap-Time + exec_host.
        state = cols[-3] if len(cols) >= 3 and len(cols[-3]) == 1 else ""
        for node, cpus in _exec_host_cpus(exec_host).items():
            out.setdefault(node, []).append(
                {
                    "user": user,
                    "job_id": cols[0],
                    "state": state,
                    "cpus": cpus,
                    "mem_gb": None,
                    "started_h_ago": None,
                    "elapsed_s": None,
                    "gpus": None,
                }
            )
    return out


def _queue_max_nodes(fields: dict[str, str], *, family: str) -> int | None:
    """Per-job node ceiling for a PBS queue, family-aware (#293).

    PBS Pro states it as ``resources_max.nodect`` (a plain int). TORQUE uses
    ``resources_max.nodes``, which can be a *nodespec* (``4`` or ``4:ppn=8``) —
    take the leading node count. TORQUE builds that emit ``nodect`` too are
    honored as a fallback.
    """
    if family == "torque":
        raw = (
            fields.get("resources_max.nodes") or fields.get("resources_max.nodect") or ""
        ).strip()
        m = re.match(r"(\d+)", raw)
        return int(m.group(1)) if m else None
    return _parse_max_nodes(fields.get("resources_max.nodect", ""))


def _queue_entry(name: str, fields: dict[str, str], *, family: str) -> dict[str, Any] | None:
    """Build the normalized parallel_environment entry for one PBS queue, or None.

    Route queues forward jobs to execution queues rather than running them, so
    they aren't a place you'd target a multi-rank job — skipped. ``kind`` is
    ``smp`` only when the per-job node ceiling is 1, else ``mpi`` (PBS allows
    multi-node by default). The per-job ``slots`` ceiling is PBS-specific → raw.
    """
    if not name or fields.get("queue_type", "").lower() == "route":
        return None
    max_nodes = _queue_max_nodes(fields, family=family)
    return {
        "name": name,
        "source": "queue",
        "kind": "smp" if max_nodes == 1 else "mpi",
        "max_nodes": max_nodes,
        "raw": {"slots": _to_int_or_none(fields.get("resources_max.ncpus"))},
    }


def parse_qstat_queues(text: str, *, family: str = "pbspro") -> list[dict[str, Any]]:
    """Parse ``qstat -Qf`` execution queues into normalized PE entries (#293).

    PBS's analog to an SGE PE / SLURM partition is the *queue* you submit to.
    ``qstat -Qf`` prints per-queue ``Queue: <name>`` blocks of ``key = value``
    lines. *family* (``pbspro`` | ``torque``) gates how the per-job node ceiling
    is read (see :func:`_queue_max_nodes`). Returns the normalized
    ``{name, source="queue", kind, max_nodes, raw={slots}}`` for each non-Route
    queue. Permissive — unparseable lines skipped, never raises.
    """
    out: list[dict[str, Any]] = []
    name: str | None = None
    fields: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Queue:"):
            if name is not None:
                entry = _queue_entry(name, fields, family=family)
                if entry is not None:
                    out.append(entry)
            name = line.split(":", 1)[1].strip()
            fields = {}
            continue
        if name is None:
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            fields[key.strip()] = val.strip()
    if name is not None:
        entry = _queue_entry(name, fields, family=family)
        if entry is not None:
            out.append(entry)
    return out


def _minimal_snapshot(
    cluster_name: str,
    scheduler_kind: str,
    reason: str,
) -> ClusterSnapshot:
    """Structurally-valid, node-less snapshot used as the safe fallback.

    The single diagnostic note makes the absence of node data explicit
    (so it doesn't look like a zero-capacity cluster) and records *why*;
    the planner falls back to conservative defaults and submit / live
    monitoring are unaffected.
    """
    return ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind=scheduler_kind,
        now_iso=utcnow_iso(),
        nodes=[],
        errors=[
            {
                "code": "pbs_inspect_minimal",
                "detail": (
                    "PBS node-level snapshot unpopulated "
                    f"({reason}); planner uses conservative defaults. "
                    "Submit and live monitoring are unaffected."
                ),
            }
        ],
    )


def parse_pbsnodes(text: str, *, family: str) -> list[NodeSnapshot]:
    """Parse ``pbsnodes`` output into :class:`NodeSnapshot` rows.

    Keyed on *family* (``pbspro`` vs ``torque``) because the two emit
    different attribute grammars for the same conceptual fields. Permissive
    throughout — an unrecognised stanza yields a minimal node rather than
    raising, matching the rest of the inspect package's posture.
    """
    if family == "torque":
        return _parse_pbsnodes_torque(text)
    return _parse_pbsnodes_pbspro(text)


def _split_node_stanzas(text: str) -> list[list[str]]:
    """Split ``pbsnodes`` output into per-node blocks.

    Both families print a bare node-name line at column 0 followed by
    indented ``key = value`` attribute lines; stanzas are blank-line
    separated. Returns each block as its list of non-blank lines.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _stanza_fields(block: list[str]) -> tuple[str, dict[str, str]]:
    """Return ``(node_name, {attr: value})`` for one stanza, or ``("", {})``.

    The first line is the bare node name; remaining ``key = value`` lines
    become the attribute map. Values may contain ``=`` (e.g. TORQUE's
    packed ``status`` line), so we partition on the *first* ``=`` only.

    A stanza with **no** attribute lines is rejected (returns ``("", {})``):
    real ``pbsnodes`` output always carries at least a ``state`` line, so a
    lone non-attribute line is junk (header noise, an error message) rather
    than a node — distinguishing it lets the driver fall back to the safe
    minimal snapshot instead of inventing a phantom node.
    """
    if not block:
        return "", {}
    name = block[0].strip()
    # A well-formed stanza opens with the bare node name (no ``=``); a
    # leading attribute line means the header is missing — skip the block.
    if not name or "=" in block[0]:
        return "", {}
    fields: dict[str, str] = {}
    for line in block[1:]:
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        fields[key.strip()] = val.strip()
    if not fields:
        return "", {}
    return name, fields


def _parse_pbsnodes_pbspro(text: str) -> list[NodeSnapshot]:
    """Parse ``pbsnodes -av`` (PBS Pro / OpenPBS) stanzas.

    Capacity comes from ``resources_available.*`` (falling back to
    ``pcpus`` for the core count) and allocation from
    ``resources_assigned.*``. GPU advertisements map to the ``gpu:N``
    GRES shape the planner already understands.
    """
    nodes: list[NodeSnapshot] = []
    for block in _split_node_stanzas(text):
        name, f = _stanza_fields(block)
        if not name:
            continue
        snap = NodeSnapshot(name=name)
        snap.state = f.get("state", "")
        cpu_tot = _to_int_or_none(f.get("resources_available.ncpus"))
        if cpu_tot is None:
            cpu_tot = _to_int_or_none(f.get("pcpus"))
        snap.cpu_tot = cpu_tot
        snap.cpu_alloc = _to_int_or_none(f.get("resources_assigned.ncpus"))
        snap.real_mem_mb = _parse_mem_to_mb(f.get("resources_available.mem"))
        snap.alloc_mem_mb = _parse_mem_to_mb(f.get("resources_assigned.mem"))
        if snap.real_mem_mb and snap.alloc_mem_mb is not None and snap.real_mem_mb > 0:
            # ``resources_available.mem`` and ``resources_assigned.mem`` are
            # independently-reported values (unlike SLURM's AllocMem ≤
            # RealMemory invariant), so an over-committed node can report
            # assigned > available. Clamp to 1.0: the snapshot schema bounds
            # ``alloc_mem_pct`` to [0, 1] and validate_output would reject a
            # higher value, breaking the whole inspect emit.
            snap.alloc_mem_pct = round(min(snap.alloc_mem_mb / snap.real_mem_mb, 1.0), 4)
        ngpus = _to_int_or_none(f.get("resources_available.ngpus"))
        if ngpus:
            snap.gres = f"gpu:{ngpus}"
        ngpus_used = _to_int_or_none(f.get("resources_assigned.ngpus"))
        if ngpus_used:
            snap.gres_used = f"gpu:{ngpus_used}"
        snap.is_drained = _state_is_unavailable(snap.state)
        nodes.append(snap)
    return nodes


def _parse_torque_status(status: str) -> dict[str, str]:
    """Parse TORQUE's packed ``status = k=v,k=v,...`` line into a dict."""
    out: dict[str, str] = {}
    for part in status.split(","):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        out[key.strip()] = val.strip()
    return out


def _parse_pbsnodes_torque(text: str) -> list[NodeSnapshot]:
    """Parse ``pbsnodes -a`` (TORQUE) stanzas.

    Core count comes from ``np`` (overridden by ``status.ncpus`` when the
    MOM reports it); memory and load come from the packed ``status`` line
    (``physmem``/``totmem`` total, ``availmem`` free → used = total − free,
    ``loadave`` → 1-min load). ``gpus = N`` maps to the ``gpu:N`` GRES shape.
    """
    nodes: list[NodeSnapshot] = []
    for block in _split_node_stanzas(text):
        name, f = _stanza_fields(block)
        if not name:
            continue
        snap = NodeSnapshot(name=name)
        snap.state = f.get("state", "")
        snap.cpu_tot = _to_int_or_none(f.get("np"))
        status = _parse_torque_status(f.get("status", ""))
        ncpus = _to_int_or_none(status.get("ncpus"))
        if ncpus is not None:
            snap.cpu_tot = ncpus
        total_mem = _parse_mem_to_mb(status.get("physmem") or status.get("totmem"))
        avail_mem = _parse_mem_to_mb(status.get("availmem"))
        snap.real_mem_mb = total_mem
        if total_mem and avail_mem is not None:
            # used is clamped into [0, total_mem] (availmem can momentarily
            # read above physmem), so the ratio stays in [0, 1] as the
            # snapshot schema requires.
            used = min(max(total_mem - avail_mem, 0), total_mem)
            snap.alloc_mem_mb = used
            snap.alloc_mem_pct = round(used / total_mem, 4)
        load = _to_float_or_none(status.get("loadave"))
        snap.cpu_load = load
        if load is not None and snap.cpu_tot:
            snap.cpu_load_frac = round(load / max(snap.cpu_tot, 1), 4)
        ngpus = _to_int_or_none(f.get("gpus"))
        if ngpus:
            snap.gres = f"gpu:{ngpus}"
        snap.is_drained = _state_is_unavailable(snap.state)
        nodes.append(snap)
    return nodes
