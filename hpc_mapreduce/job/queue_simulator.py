"""Thin discrete-event simulator for queue-wait prediction.

Models FIFO + EASY backfill — the scheduler policy academic clusters
typically run. Calibratable residual against observed waits is the
validation target; perfect SLURM fidelity is not.

Module surface:

* :class:`SimJob` — one job inside the simulator's view (queued, running,
  or candidate / hypothetical).
* :class:`SimResult` — frozen output of a simulator run, including the
  candidate's predicted start offset (the "wait" we forecast) and the
  p10/p50/p90 distribution when ``simulate_distribution`` is used.
* :func:`simulate_one_pass` — single-replication forward simulation.
* :func:`simulate_distribution` — runs ``n_replications`` independent
  passes with sampled arrivals + sampled per-job runtime variability,
  returns the candidate's wait-time distribution.
* :func:`extract_running_jobs` — pull the running jobs out of a
  ``ClusterSnapshot`` (which surfaces them as per-node ``co_tenants``).
* :func:`available_resources` — node-level free CPU / memory / GPU
  capacity, derived from the snapshot.

Implementation notes
--------------------

State: a heap of pending events ``(time, kind, seq, job_id)``. Events are
``submit``, ``start``, and ``end``. When a job ends, its resources go
back to the pool; the scheduler immediately re-runs the policy loop and
tries to start queued jobs in priority order.

Priority: FIFO by submit time, ties broken by job id (lex). Real
schedulers use MULTIFACTOR; for v1 FIFO is enough — the validation loop
tracks the residual and the predictor's ``method`` field reports DES vs
diurnal-MA so we can layer MULTIFACTOR later only if calibration shows
systematic favoritism.

EASY backfill: when the head-of-queue (HoQ) job cannot start now, we
compute the earliest time it CAN start ("HoQ reservation") by simulating
each running job ending in order. Any backfill-eligible queued job that
both fits in the currently-free pool AND finishes before the HoQ
reservation may run immediately. Documented in any SLURM/Maui paper.

Resource accounting: nodes are flat for v1 (no NUMA, no per-socket
pinning). A job needs N CPUs, M MB, and optionally G GPUs of type T; it
is placed on a single node that has the capacity. Heterogeneous GPU
types are matched strictly. Multi-node placement is NOT modeled in v1
— callers requesting a job larger than any single node will see the
job sit forever (returned as ``predicted_start_offset_sec ==
max_horizon_sec``).
"""

from __future__ import annotations

import dataclasses
import heapq
import random
from typing import Any, Iterable

from hpc_mapreduce.infra.inspect import ClusterSnapshot, NodeSnapshot

__all__ = [
    "SimJob",
    "SimResult",
    "extract_running_jobs",
    "available_resources",
    "simulate_one_pass",
    "simulate_distribution",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SimJob:
    """One job inside the simulator's view.

    ``submit_time`` is in seconds-from-sim-start. Running jobs in the
    initial snapshot have ``submit_time <= 0`` and ``state == "running"``
    with ``start_time`` already set; their ``end_time`` is filled in
    when the simulator schedules their completion.
    """

    job_id: str  # "real" id or "candidate-<n>" for hypotheticals
    user: str
    submit_time: float  # seconds from sim start
    walltime_ask: float  # seconds the user requested (``--time=``)
    cpus: int
    mem_mb: int
    gpus: int = 0  # total GPU count required
    gpu_type: str = ""  # strict-match; "" means CPU-only
    walltime_actual: float | None = None  # sampled actual runtime
    state: str = "queued"  # queued | running | complete | failed
    start_time: float | None = None
    end_time: float | None = None
    backfill_eligible: bool = True

    def required_resources(self) -> tuple[int, int, int, str]:
        return (self.cpus, self.mem_mb, self.gpus, self.gpu_type)


@dataclasses.dataclass(frozen=True)
class SimResult:
    """Output of a simulator run.

    ``predicted_start_offset_sec`` is the candidate's wait time. When the
    candidate never runs within ``max_horizon_sec`` it is set to the
    horizon (the caller treats this as "wait at least this long").

    ``predicted_state_at_horizon`` is a small dict useful for debugging:
    counts of jobs in each state at the end of the simulation, plus the
    final cluster utilization fraction.

    For ``simulate_one_pass``, ``n_replications == 1`` and
    ``p10/p50/p90`` are all equal to ``predicted_start_offset_sec``.
    """

    candidate_job_id: str
    predicted_start_offset_sec: float
    predicted_state_at_horizon: dict[str, Any]
    n_replications: int
    p10_wait_sec: float
    p50_wait_sec: float
    p90_wait_sec: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Snapshot adapters
# ---------------------------------------------------------------------------


def _parse_gres_total(gres: str) -> dict[str, int]:
    """Parse a SLURM ``Gres=`` string into ``{type: count}``.

    Examples:
      "gpu:a100:2" -> {"a100": 2}
      "gpu:a100:2,gpu:v100:1" -> {"a100": 2, "v100": 1}
      "" / "(null)" -> {}
    """
    out: dict[str, int] = {}
    if not gres or gres.strip() in {"(null)", "null"}:
        return out
    for chunk in gres.split(","):
        parts = chunk.strip().split(":")
        if len(parts) < 2:
            continue
        if parts[0].lower() != "gpu":
            continue
        if len(parts) == 2:
            try:
                out[""] = out.get("", 0) + int(_strip_paren(parts[1]))
            except ValueError:
                continue
        else:
            gpu_type = parts[1]
            try:
                cnt = int(_strip_paren(parts[2]))
            except ValueError:
                continue
            out[gpu_type] = out.get(gpu_type, 0) + cnt
    return out


def _strip_paren(s: str) -> str:
    head = s.split("(", 1)[0]
    return head.strip()


def extract_running_jobs(snapshot: ClusterSnapshot) -> list[SimJob]:
    """Extract running jobs from the snapshot's per-node ``co_tenants``.

    ``ClusterSnapshot`` does not carry a flat list of running jobs — they
    are surfaced under each ``NodeSnapshot.co_tenants``. A job that spans
    multiple nodes appears once per node; we de-dup by ``job_id`` keeping
    the first occurrence's resource numbers, since the snapshot's per-row
    ``cpus``/``mem_gb``/``gpus`` are already job-totals from sacct's
    ``ReqCPUS``/``ReqMem``/``AllocTRES``.

    Each running job's ``submit_time`` is derived from
    ``-elapsed_s`` — i.e. it submitted ``elapsed_s`` ago (we treat
    "submit" and "start" as coincident for active running jobs since the
    snapshot doesn't carry the original submit time). ``walltime_ask``
    is unknown from the snapshot, so we default it to ``elapsed_s``
    plus a 1-hour cushion; the residual-lifetime sampler refines it.
    """
    seen: set[str] = set()
    out: list[SimJob] = []
    for node in snapshot.nodes:
        for tenant in node.co_tenants or []:
            jid = str(tenant.get("job_id", "")).strip()
            if not jid or jid in seen:
                continue
            seen.add(jid)
            cpus = int(tenant.get("cpus") or 0)
            mem_gb = tenant.get("mem_gb")
            mem_mb = int(round(float(mem_gb) * 1024)) if mem_gb else 0
            gpus = int(tenant.get("gpus") or 0)
            elapsed = float(tenant.get("elapsed_s") or 0.0)
            walltime_ask = elapsed + 3600.0
            sj = SimJob(
                job_id=jid,
                user=str(tenant.get("user") or ""),
                submit_time=-elapsed,
                walltime_ask=walltime_ask,
                cpus=cpus,
                mem_mb=mem_mb,
                gpus=gpus,
                gpu_type=_infer_gpu_type_from_node(node) if gpus else "",
                state="running",
                start_time=-elapsed,
                end_time=None,
            )
            out.append(sj)
    return out


def _infer_gpu_type_from_node(node: NodeSnapshot) -> str:
    """Infer the GPU type a job on this node is using from the node's gres.

    A node typically advertises a single GPU type. When multiple types
    are present we return the first (arbitrary but deterministic) — the
    snapshot doesn't tell us which one a specific job allocated. This is
    a known v1 limitation; documented in the deferrals list.
    """
    types = list(_parse_gres_total(node.gres).keys())
    return types[0] if types else ""


def available_resources(
    snapshot: ClusterSnapshot,
) -> dict[str, dict[str, Any]]:
    """Compute per-node free CPUs / memory / GPU-by-type from the snapshot.

    Returns ``{node_name: {"cpus_free": int, "mem_mb_free": int,
    "gpus_free": {gpu_type: int}, "drained": bool}}``. A node in DRAIN
    or DOWN state has ``drained=True`` and zero free resources.
    """
    out: dict[str, dict[str, Any]] = {}
    for n in snapshot.nodes:
        if n.is_drained:
            out[n.name] = {
                "cpus_free": 0,
                "mem_mb_free": 0,
                "gpus_free": {},
                "drained": True,
                "cpus_total": int(n.cpu_tot or 0),
                "mem_mb_total": int(n.real_mem_mb or 0),
                "gpus_total": _parse_gres_total(n.gres),
            }
            continue
        cpus_total = int(n.cpu_tot or 0)
        cpus_alloc = int(n.cpu_alloc or 0)
        cpus_free = max(0, cpus_total - cpus_alloc)
        mem_total = int(n.real_mem_mb or 0)
        mem_alloc = int(n.alloc_mem_mb or 0)
        mem_free = max(0, mem_total - mem_alloc)
        gpus_total = _parse_gres_total(n.gres)
        gpus_used = _parse_gres_total(n.gres_used)
        gpus_free = {
            t: max(0, c - gpus_used.get(t, 0)) for t, c in gpus_total.items()
        }
        out[n.name] = {
            "cpus_free": cpus_free,
            "mem_mb_free": mem_free,
            "gpus_free": gpus_free,
            "drained": False,
            "cpus_total": cpus_total,
            "mem_mb_total": mem_total,
            "gpus_total": gpus_total,
        }
    return out


# ---------------------------------------------------------------------------
# Simulator core
# ---------------------------------------------------------------------------


_EVT_END = 0
_EVT_SUBMIT = 1
_EVT_START = 2  # diagnostic only


def _try_place(
    job: SimJob, free_by_node: dict[str, dict[str, Any]]
) -> str | None:
    """Return the node name a job fits on (first-fit), or None."""
    for name, free in free_by_node.items():
        if free["drained"]:
            continue
        if free["cpus_free"] < job.cpus:
            continue
        if free["mem_mb_free"] < job.mem_mb:
            continue
        if job.gpus > 0:
            if not job.gpu_type:
                total_avail = sum(free["gpus_free"].values())
                if total_avail < job.gpus:
                    continue
            else:
                if free["gpus_free"].get(job.gpu_type, 0) < job.gpus:
                    continue
        return name
    return None


def _consume(
    node: str,
    job: SimJob,
    free_by_node: dict[str, dict[str, Any]],
) -> None:
    free = free_by_node[node]
    free["cpus_free"] -= job.cpus
    free["mem_mb_free"] -= job.mem_mb
    if job.gpus > 0:
        if job.gpu_type:
            free["gpus_free"][job.gpu_type] -= job.gpus
        else:
            remaining = job.gpus
            for t in sorted(free["gpus_free"]):
                avail = free["gpus_free"][t]
                take = min(avail, remaining)
                free["gpus_free"][t] -= take
                remaining -= take
                if remaining <= 0:
                    break


def _release(
    node: str,
    job: SimJob,
    free_by_node: dict[str, dict[str, Any]],
) -> None:
    free = free_by_node[node]
    free["cpus_free"] += job.cpus
    free["mem_mb_free"] += job.mem_mb
    if job.gpus > 0:
        if job.gpu_type:
            free["gpus_free"][job.gpu_type] = (
                free["gpus_free"].get(job.gpu_type, 0) + job.gpus
            )
        else:
            # Best-effort symmetric to _consume: bump first known type.
            keys = sorted(free["gpus_free"]) or [""]
            free["gpus_free"][keys[0]] = (
                free["gpus_free"].get(keys[0], 0) + job.gpus
            )


def _hoq_reservation(
    queued: list[SimJob],
    running_endings: list[tuple[float, SimJob, str]],
    free_by_node: dict[str, dict[str, Any]],
    now: float,
) -> tuple[float, SimJob | None]:
    """Compute the earliest time the head-of-queue job can start.

    Walks the running-job end times in chronological order, releasing
    each job's resources virtually. As soon as the HoQ job fits, that's
    the reservation time. Returns ``(reservation_time, hoq_job)`` or
    ``(inf, None)`` if no HoQ exists or it can never start within the
    sampled job ends.
    """
    if not queued:
        return (float("inf"), None)
    hoq = queued[0]
    virt: dict[str, dict[str, Any]] = {
        name: {
            "cpus_free": f["cpus_free"],
            "mem_mb_free": f["mem_mb_free"],
            "gpus_free": dict(f["gpus_free"]),
            "drained": f["drained"],
        }
        for name, f in free_by_node.items()
    }
    if _try_place(hoq, virt) is not None:
        return (now, hoq)
    for end_t, job, node in sorted(running_endings, key=lambda t: t[0]):
        if node not in virt:
            continue
        f = virt[node]
        f["cpus_free"] += job.cpus
        f["mem_mb_free"] += job.mem_mb
        if job.gpus > 0 and job.gpu_type:
            f["gpus_free"][job.gpu_type] = (
                f["gpus_free"].get(job.gpu_type, 0) + job.gpus
            )
        if _try_place(hoq, virt) is not None:
            return (max(end_t, now), hoq)
    return (float("inf"), hoq)


def simulate_one_pass(
    snapshot: ClusterSnapshot,
    *,
    candidate: SimJob,
    user_profiles: dict[str, Any] | None = None,
    arrival_stream: list[SimJob] | None = None,
    residual_lifetimes: dict[str, float] | None = None,
    max_horizon_sec: float = 7 * 86400.0,
    seed: int | None = None,
) -> SimResult:
    """Simulate the scheduler forward from ``snapshot``'s state.

    Returns the predicted start offset for ``candidate``. If the
    candidate never runs within ``max_horizon_sec``, the returned
    ``predicted_start_offset_sec`` equals ``max_horizon_sec`` and the
    state-at-horizon dict marks ``candidate_state == "queued"``.

    ``arrival_stream``: optional list of ``SimJob`` representing future
    submissions sampled from the per-user Hawkes/Poisson process. Each
    must have ``submit_time > 0``. If None, no future arrivals.

    ``residual_lifetimes``: optional ``{job_id: end_offset_sec}`` mapping
    that overrides the default ``walltime_ask`` for running jobs in the
    snapshot. Sampled by ``queue_simulator_inputs.sample_residual_lifetimes``.
    """
    rng = random.Random(seed)
    free_by_node = available_resources(snapshot)
    running = extract_running_jobs(snapshot)

    events: list[tuple[float, int, int, str]] = []
    seq = 0
    placed_node: dict[str, str] = {}
    job_table: dict[str, SimJob] = {}

    for j in running:
        target = _placement_for_running(j, snapshot, free_by_node)
        if target is None:
            continue
        # Resources were already deducted via cpu_alloc/mem_alloc/gres_used
        # in available_resources(); we don't deduct again. We just need
        # to know the node so _release puts them back when the job ends.
        placed_node[j.job_id] = target
        if residual_lifetimes and j.job_id in residual_lifetimes:
            end_t = float(residual_lifetimes[j.job_id])
        else:
            elapsed = -j.submit_time
            remaining = max(0.0, j.walltime_ask - elapsed)
            end_t = remaining
        end_t = max(0.0, end_t)
        j.end_time = end_t
        j.walltime_actual = end_t + (-j.submit_time)
        seq += 1
        heapq.heappush(events, (end_t, _EVT_END, seq, j.job_id))
        job_table[j.job_id] = j

    job_table[candidate.job_id] = candidate
    seq += 1
    heapq.heappush(
        events, (max(0.0, candidate.submit_time), _EVT_SUBMIT, seq, candidate.job_id)
    )

    if arrival_stream:
        for arr in arrival_stream:
            if arr.submit_time < 0 or arr.submit_time > max_horizon_sec:
                continue
            seq += 1
            heapq.heappush(
                events, (arr.submit_time, _EVT_SUBMIT, seq, arr.job_id)
            )
            job_table[arr.job_id] = arr

    queued: list[SimJob] = []
    candidate_started = False
    candidate_start: float | None = None
    completed = 0
    failed = 0

    def _policy_loop(now: float) -> None:
        """FIFO + EASY-backfill policy. Mutates queued + free_by_node + events."""
        nonlocal seq, candidate_started, candidate_start
        if not queued:
            return
        queued.sort(key=lambda j: (j.submit_time, j.job_id))
        # Tier 1: drain HoQ-eligible runs.
        while queued:
            head = queued[0]
            target = _try_place(head, free_by_node)
            if target is None:
                break
            queued.pop(0)
            _consume(target, head, free_by_node)
            head.state = "running"
            head.start_time = now
            placed_node[head.job_id] = target
            if head.walltime_actual is None:
                head.walltime_actual = head.walltime_ask
            head.end_time = now + head.walltime_actual
            seq += 1
            heapq.heappush(
                events, (head.end_time, _EVT_END, seq, head.job_id)
            )
            if head.job_id == candidate.job_id and not candidate_started:
                candidate_started = True
                candidate_start = now
        if not queued:
            return
        # Tier 2: EASY backfill.
        running_endings = [
            (job_table[jid].end_time or float("inf"),
             job_table[jid],
             placed_node.get(jid, ""))
            for _, kind, _, jid in events
            if kind == _EVT_END and job_table.get(jid) is not None
            and (job_table[jid].state == "running")
        ]
        hoq_resv, _hoq = _hoq_reservation(queued, running_endings, free_by_node, now)
        i = 1
        while i < len(queued):
            cand = queued[i]
            if not cand.backfill_eligible:
                i += 1
                continue
            target = _try_place(cand, free_by_node)
            if target is None:
                i += 1
                continue
            actual = cand.walltime_actual if cand.walltime_actual is not None else cand.walltime_ask
            if now + actual > hoq_resv:
                i += 1
                continue
            queued.pop(i)
            _consume(target, cand, free_by_node)
            cand.state = "running"
            cand.start_time = now
            cand.walltime_actual = actual
            cand.end_time = now + actual
            placed_node[cand.job_id] = target
            seq += 1
            heapq.heappush(events, (cand.end_time, _EVT_END, seq, cand.job_id))
            if cand.job_id == candidate.job_id and not candidate_started:
                candidate_started = True
                candidate_start = now

    while events:
        time, kind, _seq, jid = heapq.heappop(events)
        if time > max_horizon_sec:
            break
        if kind == _EVT_END:
            j = job_table.get(jid)
            if j is None:
                continue
            if j.state != "running":
                continue
            node = placed_node.get(jid, "")
            if node:
                _release(node, j, free_by_node)
            j.state = "complete"
            completed += 1
            _policy_loop(time)
        elif kind == _EVT_SUBMIT:
            j = job_table.get(jid)
            if j is None:
                continue
            j.state = "queued"
            if j is not candidate and j.walltime_actual is None:
                j.walltime_actual = max(
                    1.0, j.walltime_ask * rng.uniform(0.6, 1.0)
                )
            queued.append(j)
            _policy_loop(time)

    if candidate_started and candidate_start is not None:
        wait = max(0.0, candidate_start - candidate.submit_time)
    else:
        wait = max_horizon_sec

    state_at_horizon = {
        "n_completed": completed,
        "n_failed": failed,
        "n_queued": len(queued),
        "candidate_state": candidate.state,
    }

    return SimResult(
        candidate_job_id=candidate.job_id,
        predicted_start_offset_sec=wait,
        predicted_state_at_horizon=state_at_horizon,
        n_replications=1,
        p10_wait_sec=wait,
        p50_wait_sec=wait,
        p90_wait_sec=wait,
    )


def _placement_for_running(
    j: SimJob,
    snapshot: ClusterSnapshot,
    free_by_node: dict[str, dict[str, Any]],
) -> str | None:
    """Find which node a running job from the snapshot is on.

    The snapshot's ``co_tenants`` list per node IS the ground truth.
    We search for a node whose ``co_tenants`` includes this job_id.
    Falls back to first-fit if no exact match (defensive — pre-snapshot
    races can drop the row).
    """
    for n in snapshot.nodes:
        for tenant in n.co_tenants or []:
            if str(tenant.get("job_id", "")).strip() == j.job_id:
                if n.name in free_by_node:
                    return n.name
    return _try_place(j, free_by_node)


def simulate_distribution(
    snapshot: ClusterSnapshot,
    *,
    candidate: SimJob,
    user_profiles: dict[str, Any] | None = None,
    n_replications: int = 64,
    max_horizon_sec: float = 7 * 86400.0,
    seed: int | None = None,
    arrival_sampler: Any = None,
    residual_sampler: Any = None,
) -> SimResult:
    """Run ``n_replications`` simulations with sampled inputs.

    Variance comes from:

    * the sampled future arrival stream (per-user non-homogeneous
      Poisson; via ``arrival_sampler(seed)``) and
    * the sampled actual-walltime per running job (per-user empirical
      ratio; via ``residual_sampler(seed)``).

    When samplers are not provided, only the per-arrival jitter inside
    ``simulate_one_pass`` provides variance; this still yields a
    legitimate (narrower) distribution.

    Returns p10/p50/p90 of the candidate's wait time.
    """
    if n_replications < 1:
        raise ValueError("n_replications must be >= 1")
    waits: list[float] = []
    last_state: dict[str, Any] = {}
    rng = random.Random(seed)
    for _i in range(n_replications):
        sub_seed = rng.randint(0, 2**31 - 1)
        arr = arrival_sampler(sub_seed) if arrival_sampler is not None else None
        res = residual_sampler(sub_seed) if residual_sampler is not None else None
        out = simulate_one_pass(
            snapshot,
            candidate=dataclasses.replace(candidate),
            user_profiles=user_profiles,
            arrival_stream=arr,
            residual_lifetimes=res,
            max_horizon_sec=max_horizon_sec,
            seed=sub_seed,
        )
        waits.append(out.predicted_start_offset_sec)
        last_state = out.predicted_state_at_horizon
    waits.sort()

    def _pct(p: float) -> float:
        if not waits:
            return max_horizon_sec
        if len(waits) == 1:
            return waits[0]
        k = (len(waits) - 1) * p
        lo = int(k)
        hi = min(lo + 1, len(waits) - 1)
        frac = k - lo
        return waits[lo] + frac * (waits[hi] - waits[lo])

    return SimResult(
        candidate_job_id=candidate.job_id,
        predicted_start_offset_sec=_pct(0.5),
        predicted_state_at_horizon=last_state,
        n_replications=n_replications,
        p10_wait_sec=_pct(0.1),
        p50_wait_sec=_pct(0.5),
        p90_wait_sec=_pct(0.9),
    )


def _job_iter(jobs: Iterable[SimJob]) -> list[SimJob]:
    """Public-style helper for callers that want to iterate w/o exposing list mutability."""
    return list(jobs)
