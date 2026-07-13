"""Liveness heartbeat for DETACHED workers — the run-#12 findings 3/16/27 seam.

A detached submit block (:mod:`hpc_agent._kernel.lifecycle.detached`) runs its
cluster work to terminal in a subprocess whose stdout/stderr are captured to a
``_detached/*.log``. Finding 27's corollary obligation (twice reconfirmed the
night of 2026-07-11, and the repo's standing >10s-progress-file discipline):
**a 0-byte log for 8 minutes of LEGITIMATE work — a parent blocked on an scp
child — is indistinguishable from the frozen-at-birth failure (finding 16: 0.015s
CPU / 14 min, kernel-blocked at startup) without psutil excavation.** This module
closes that gap with ONE seam: while the worker's verb runs, a background thread
appends a single ``[hb]`` line to the log every ~30s.

Design (see the calling seam in :func:`hpc_agent.cli.dispatch.main`):

* **The thread lives in the DETACHED CHILD's process**, not the spawning session
  — it is started in the child's ``main`` before the verb runs and stopped in the
  ``finally`` so it survives session death exactly as the worker does.
* **The heartbeat writes to ``sys.stderr``** (the child's stderr is dup'd onto the
  same captured log as stdout), because stdout is contractually "exclusively the
  single-line JSON envelope" (``cli/dispatch.py``). Diagnostic prose belongs on
  stderr, so the ``[hb]`` lines never pollute the stdout envelope stream while
  still landing in the one log a post-mortem tails.
* **Append-only, never truncates; every write failure is swallowed.** A heartbeat
  must never kill the worker (constraint b).
* **The loop waits BEFORE it writes** (``Event.wait(interval)``) and re-checks
  the stop event after the (psutil-slow) snapshot, so a stop signalled in the
  ``finally`` suppresses any beat still in flight (constraint c). The verb's
  envelope staying the literal last line is best-effort — the verb prints its
  envelope before the CM's ``finally`` runs, so a beat can theoretically land
  just after it — and that is sufficient: every envelope consumer scans
  BACKWARD for the newest parseable JSON line (see
  :func:`hpc_agent.ops.aggregate_blocks._harvest_ledger_tail`'s torn-tail rationale),
  so a trailing non-JSON ``[hb]`` line is skipped, never misparsed.
* **Frozen-at-birth flag** (constraint d): if psutil confirms the worker has
  spawned NO child and burned negligible CPU across the first
  :data:`_FROZEN_AFTER_HEARTBEATS` beats, the line says ``no verb output yet
  (frozen-at-birth suspect)`` — the finding-16 signature, spelled out in the log.

  This is the signal :func:`hpc_agent.ops.recover.doctor.scan_dead_detached_workers`
  could later CONSUME (not implemented here): today that scan only fires once a
  worker's pid is DEAD with no recorded terminal. A stalled-but-alive worker whose
  log's last ``[hb]`` line is minutes stale (elapsed jumped, or the line carries
  the frozen-at-birth flag) is a mid-flight freeze the scan cannot yet see —
  reading heartbeat staleness/flag from the ``_detached/*.log`` beside each lease
  would extend the scan from "dead pid" to "alive but wedged".
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any, TextIO

__all__ = ["detached_heartbeat"]

_ENV_INTERVAL = "HPC_DETACH_HEARTBEAT_SEC"
_DEFAULT_INTERVAL_SEC = 30.0
# After this many beats with no child ever spawned AND negligible CPU, flag the
# frozen-at-birth signature explicitly. Two beats (≈60s at the default cadence)
# is well past a healthy verb's first fork/first output.
_FROZEN_AFTER_HEARTBEATS = 2
# The finding-16 worker sat at 0.015s CPU for 14 minutes; anything under this is
# "did nothing", not "is working".
_FROZEN_CPU_SECONDS = 0.5


def _heartbeat_interval() -> float:
    """Resolve the cadence from ``HPC_DETACH_HEARTBEAT_SEC`` (default 30s).

    A non-positive value (``0`` / negative) is the documented escape hatch —
    the caller treats it as "disabled". A malformed value falls back to the
    default rather than crashing the worker over a typo'd env var.
    """
    raw = os.environ.get(_ENV_INTERVAL)
    if raw is None:
        return _DEFAULT_INTERVAL_SEC
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_SEC


def _self_cpu_seconds() -> float:
    """This process's own cumulative CPU (user+system) seconds, or 0.0.

    Used only to distinguish a frozen-at-birth worker (≈0 CPU) from a busy one.
    Any psutil hiccup degrades to 0.0 — the frozen flag is additionally gated on
    ``psutil_ok`` so a missing library never manufactures a false suspicion.
    """
    try:
        import psutil

        t = psutil.Process().cpu_times()
        return float(t.user + t.system)
    except Exception:  # noqa: BLE001 — a probe hiccup must never break the heartbeat
        return 0.0


def _child_snapshot() -> tuple[str | None, float, bool, bool]:
    """``(deepest_child_name, its_cumulative_cpu_sec, saw_any_child, psutil_ok)``.

    Walks this process's descendant tree with psutil and returns the DEEPEST
    descendant — the leaf actually doing the blocking leg (the ``scp`` / ``rsync``
    / ``ssh`` under the poll loop), so "pulling" vs "reducing" vs "no children" is
    readable from the log alone. ``psutil_ok`` is False only when the library is
    absent or the probe raised; ``saw_any_child`` is True whenever the process has
    ANY descendant (even if naming the deepest one raced and failed).
    """
    try:
        import psutil
    except Exception:  # noqa: BLE001 — psutil is a hard dep, but degrade gracefully
        return None, 0.0, False, False
    try:
        me = psutil.Process()
        children = me.children(recursive=True)
    except Exception:  # noqa: BLE001 — a torn process table must not crash the beat
        return None, 0.0, False, False
    if not children:
        return None, 0.0, True, True
    my_pid = me.pid
    best_name: str | None = None
    best_cpu = 0.0
    best_depth = -1
    for child in children:
        try:
            depth = 0
            node: Any = child
            # Count ancestors up to this process — the deepest wins.
            while node is not None and getattr(node, "pid", my_pid) != my_pid and depth < 64:
                node = node.parent()
                depth += 1
            name = child.name()
            cpu_t = child.cpu_times()
            cpu = float(getattr(cpu_t, "user", 0.0) + getattr(cpu_t, "system", 0.0))
        except Exception:  # noqa: BLE001 — a child that exited mid-walk is skipped
            continue
        if depth > best_depth:
            best_depth = depth
            best_name = name
            best_cpu = cpu
    return best_name, best_cpu, True, True


def _build_line(
    *,
    elapsed_sec: float,
    count: int,
    child_name: str | None,
    child_cpu: float,
    saw_child_ever: bool,
    psutil_ok: bool,
) -> str:
    """Format one ``[hb]`` line. Visually distinct from the JSON envelope.

    Example: ``[hb] alive 480s | child=scp.exe cpu=17.2s``. With no descendant:
    ``[hb] alive 480s | no children``. When the frozen-at-birth signature holds:
    ``[hb] alive 480s | no children | no verb output yet (frozen-at-birth suspect)``.
    """
    parts = [f"[hb] alive {int(elapsed_sec)}s"]
    if child_name is not None:
        parts.append(f"child={child_name} cpu={child_cpu:.1f}s")
    else:
        parts.append("no children")
    if (
        psutil_ok
        and not saw_child_ever
        and count >= _FROZEN_AFTER_HEARTBEATS
        and _self_cpu_seconds() < _FROZEN_CPU_SECONDS
    ):
        parts.append("no verb output yet (frozen-at-birth suspect)")
    return " | ".join(parts)


def _run_loop(stop: threading.Event, interval: float, stream: TextIO) -> None:
    """Emit a heartbeat line every *interval* seconds until *stop* is set.

    Waits BEFORE each write, so a stop signalled in the caller's ``finally`` wakes
    the loop and it exits without emitting; a beat already past the wait re-checks
    the stop event after the (psutil-slow) snapshot so it, too, is suppressed.
    Every write/probe error is swallowed: a heartbeat never kills the worker.
    """
    start = time.monotonic()
    count = 0
    saw_child_ever = False
    while not stop.wait(interval):
        count += 1
        try:
            name, cpu, saw_child, psutil_ok = _child_snapshot()
            if stop.is_set():
                return
            saw_child_ever = saw_child_ever or saw_child
            line = _build_line(
                elapsed_sec=time.monotonic() - start,
                count=count,
                child_name=name,
                child_cpu=cpu,
                saw_child_ever=saw_child_ever,
                psutil_ok=psutil_ok,
            )
            stream.write(line + "\n")
            stream.flush()
        except Exception:  # noqa: BLE001 — a heartbeat must NEVER kill the worker
            pass


@contextlib.contextmanager
def detached_heartbeat(stream: TextIO | None = None) -> Iterator[None]:
    """Run the liveness heartbeat for the duration of a detached worker's verb.

    A no-op unless this process is a DETACHED worker (``HPC_DETACHED_RUN_ID`` set
    by :func:`hpc_agent._kernel.lifecycle.detached._spawn_detached`) AND the
    cadence is positive (``HPC_DETACH_HEARTBEAT_SEC=0`` is the escape hatch). The
    background thread is started before the wrapped body runs and stopped in the
    ``finally``; it is a daemon and the join is bounded, so it can never wedge the
    worker's exit.

    *stream* defaults to ``sys.stderr`` (the child's captured log). Injectable for
    tests.
    """
    interval = _heartbeat_interval()
    if not os.environ.get("HPC_DETACHED_RUN_ID") or interval <= 0:
        yield
        return
    target = stream if stream is not None else sys.stderr
    stop = threading.Event()
    thread = threading.Thread(
        target=_run_loop,
        args=(stop, interval, target),
        name="hpc-detach-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # The loop is in Event.wait(); set() wakes it at once. A bounded join
        # means a wedged writer can never hold up the worker's exit.
        thread.join(timeout=max(2.0, min(interval, 5.0)))
