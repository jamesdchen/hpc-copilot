"""SGE-specific cluster inspection.

Parses ``qhost -F gpu -q`` (resource state) and ``qstat -u '*' -F gpu``
(co-tenants) into the shared snapshot shapes.
"""

from __future__ import annotations

import re
from typing import Any

from hpc_agent.infra.parsing import (
    parse_mem_to_mb as _parse_mem_to_mb,
)
from hpc_agent.infra.parsing import (
    parse_qstat_columns,
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
)

__all__ = ["_sge_inspect", "_parse_qhost", "_parse_qstat_full"]


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
    # ``parse_qstat_columns`` skips blank lines and the standard
    # header/separator/global rows; continuation lines (resource
    # attributes that belong to the most recent host) come back with a
    # sentinel "" leading column so we can re-attach them.
    for cols in parse_qstat_columns(text):
        if cols and cols[0] == "":
            # Resource attribute line for the current host. Accepts both
            # the prefixed form (e.g. ``hl:gpu=2``, ``gl:gpu_used=1``)
            # used by most SGE installs and the bare form (``gpu=2``)
            # some clusters emit. Two scoped searches so a line that
            # carries both routes each value to the correct field —
            # substring-checking the whole line miscategorizes one of
            # the two values.
            if current is None:
                continue
            text_line = " ".join(cols[1:])
            m_used = re.search(r"(?:[A-Za-z]+:)?gpu_used=(\S+)", text_line)
            if m_used:
                current.gres_used = f"gpu:{m_used.group(1)}"
            m_free = re.search(r"(?<![A-Za-z_])(?:[A-Za-z]+:)?gpu=(\S+)", text_line)
            if m_free:
                current.gres = f"gpu:{m_free.group(1)}"
            continue
        if len(cols) < 9:
            continue
        host = cols[0]
        current = NodeSnapshot(name=host)
        current.cpu_tot = _to_int_or_none(cols[2])
        current.cpu_load = _to_float_or_none(cols[6])
        if current.cpu_load is not None and current.cpu_tot:
            current.cpu_load_frac = round(current.cpu_load / max(current.cpu_tot, 1), 4)
        current.real_mem_mb = _parse_mem_to_mb(cols[7])
        mem_used = _parse_mem_to_mb(cols[8])
        if current.real_mem_mb and mem_used is not None:
            current.alloc_mem_mb = mem_used
            current.alloc_mem_pct = round(mem_used / current.real_mem_mb, 4)
        nodes.append(current)
    return nodes


def _parse_qstat_full(text: str) -> dict[str, list[dict[str, Any]]]:
    """Best-effort parse of ``qstat -u '*' -F`` for co-tenants per host.

    SGE's qstat output here is queue-instance-oriented, not host-oriented;
    we map ``queue@host`` -> host. Returns minimal info — SGE does not
    expose start time in this view.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    current_host: str | None = None
    seen_jobs_per_host: dict[str, set[str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Section separators (e.g. ``############`` before PENDING JOBS,
        # or a row of dashes) must clear ``current_host`` — otherwise the
        # digit-led pending rows that follow get bucketed under whichever
        # queue@host appeared last in the running section.
        if stripped.startswith(("#", "-", "=")):
            current_host = None
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
            # cols layout for ``qstat -u '*' -F gpu``: jid, prio, name,
            # user, state, date, time, [queue,] slots, [task_spec].
            # Pending jobs (state contains ``w``: qw/hqw/Eqw) omit the
            # ``queue`` column, shifting ``slots`` from index 8 to 7 — a
            # fixed index 8 mis-reports a pending job's CPU count.
            slots_idx = 7 if "w" in state else 8
            if len(cols) > slots_idx:
                cpus = _to_int_or_none(cols[slots_idx]) or 0
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
