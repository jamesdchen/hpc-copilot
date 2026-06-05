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

from ._common import ClusterSnapshot, NodeSnapshot, _CommandRunner, _is_stressed

__all__ = ["_pbs_inspect", "parse_pbsnodes"]


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
    # prints the same per-node stanzas with ``-a``.
    cmd = "pbsnodes -av" if family == "pbspro" else "pbsnodes -a"
    rc, out, err = runner.run(cmd)
    if rc != 0:
        return _minimal_snapshot(
            cluster_name,
            scheduler_kind,
            f"`{cmd}` failed (rc={rc}): {err.strip()[:300]}",
        )

    nodes = parse_pbsnodes(out, family=family)
    if not nodes:
        return _minimal_snapshot(
            cluster_name,
            scheduler_kind,
            f"`{cmd}` returned no parseable node stanzas",
        )

    for n in nodes:
        n.is_stressed = _is_stressed(n, stress_alloc_mem_pct, stress_cpu_load_frac)

    return ClusterSnapshot(
        cluster=cluster_name,
        scheduler_kind=scheduler_kind,
        now_iso=utcnow_iso(),
        nodes=nodes,
        errors=[],
    )


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
