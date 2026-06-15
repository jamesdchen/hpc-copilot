"""Tick-log writer (``<run_id>.monitor.jsonl``) and status fingerprint.

Extracted from :mod:`hpc_agent.ops.monitor_flow` so the orchestrator
keeps its focus on the per-poll lifecycle. The functions here are
side-effecting (one writes a JSONL record under
``<exp>/.hpc/runs/``; the others are pure compute) but they form a
cohesive tick-bookkeeping concern shared with the slash-command surface.

Re-exported from :mod:`hpc_agent.ops.monitor_flow` so the helpers stay
reachable under their legacy attribute path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.run_record import runs_dir

__all__ = ["_append_tick", "_status_fingerprint", "_tick_log_path"]

# Keys stamped on every poll that carry no state-change signal. They must be
# excluded from the fingerprint, or the equality oracle flips on every tick
# and the adaptive backoff never engages (it would keep polling at the floor
# cadence for the whole run). ``checked_at`` is set unconditionally by both
# ``status.record_status`` and ``reconcile`` on each poll.
_VOLATILE_FINGERPRINT_KEYS = frozenset({"checked_at"})


def _status_fingerprint(status: dict[str, Any]) -> str:
    """Return a stable hash of the *state-bearing* part of the status dict.

    Any change in task counts, scheduler-state flips, new waves, etc.
    flips the fingerprint and resets the adaptive backoff. Volatile
    per-poll keys (see ``_VOLATILE_FINGERPRINT_KEYS``, e.g. the
    ``checked_at`` timestamp) are stripped first so an *unchanged* status
    hashes identically across ticks. We serialize with ``sort_keys=True``
    and ``default=str`` so heterogeneous (and nested-dict) values like the
    ``waves`` block hash deterministically without us having to enumerate
    which keys matter. blake2b is fast and collision-resistant enough for
    an equality oracle.
    """
    state = {k: v for k, v in status.items() if k not in _VOLATILE_FINGERPRINT_KEYS}
    try:
        payload = json.dumps(state, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        # Pathological payload — fall back to a per-call unique value so
        # we never spuriously declare "unchanged" on an opaque diff.
        payload = repr(sorted(state.items(), key=lambda kv: kv[0])).encode(
            "utf-8", errors="replace"
        )
    return hashlib.blake2b(payload, digest_size=16).hexdigest()


def _tick_log_path(experiment_dir: Path, run_id: str) -> Path:
    """Return the path the slash-command surface writes its tick log to.

    Sharing the file across surfaces lets ``/monitor-hpc summary`` work
    regardless of whether monitoring was driven by repeated slash-command
    invocations or by one long monitor-flow call.
    """
    return runs_dir(experiment_dir) / f"{run_id}.monitor.jsonl"


def _append_tick(
    experiment_dir: Path,
    run_id: str,
    *,
    summary: dict[str, Any],
    diff_from_prev: dict[str, list[int]],
    actions: list[dict[str, Any]],
    lifecycle_state: str,
    next_tick_seconds: float | None,
) -> None:
    """Append one JSONL record to ``<run_id>.monitor.jsonl`` (best-effort).

    Holds an exclusive flock for the duration of the append so a
    concurrent slash-command writer can't interleave bytes mid-line.
    """
    record = {
        "tick_id": utcnow_iso(),
        "run_id": run_id,
        "summary": summary,
        "diff_from_prev": diff_from_prev,
        "preflight": "ok",
        "actions": actions,
        "lifecycle_state": lifecycle_state,
        "next_tick_seconds": next_tick_seconds,
        "console_emitted": False,
    }
    path = _tick_log_path(experiment_dir, run_id)
    # B7: Route the JSONL append through hpc_agent._kernel.extension.telemetry,
    # which owns the flock-guarded writer pattern. Telemetry's
    # monitor-jsonl sink ignores HPC_TELEMETRY_SINK because this caller
    # is the canonical producer.
    try:
        from hpc_agent._kernel.extension.telemetry import record as _telemetry_record

        _telemetry_record(
            "tick",
            record,
            sink="monitor-jsonl",
            monitor_jsonl_path=path,
        )
    except Exception:  # noqa: BLE001 — never crash the loop on telemetry
        # Tick log writes must never crash the loop. The journal record
        # is the primary state; this is observability.
        pass
