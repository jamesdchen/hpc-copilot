"""Read the cluster's per-task terminal announcements in ONE bounded ssh exec.

Crash-only-monitoring Phase 1 (``docs/design/crash-only-monitoring.md``). The
cluster-side dispatcher writes one small marker file per task on its terminal
bookkeeping — ``<remote_path>/.hpc/announce/<run_id>/task_<id>.complete`` or
``…/task_<id>.failed`` — the FILENAME encoding the promote/failure verdict the
dispatcher committed (the finding-16 empty-output guard's verdict, not the raw
executor rc). Encoding the state in the filename lets the client COUNT
per-state outcomes with a pure ``ls`` — no shared-file append (Lustre/NFS
contention), no per-file ``cat``, one fork-minimal exec that a NAT'd link can
finish inside its idle window (run-12 findings 20/24: a 20-25 min silent
status-reporter walk was severed mid-flight and the run could not be verified).

TRUST BOUNDARY: an announcement settles run LIFECYCLE only — did each task
reach a terminal state, and which one. It is NOT an integrity claim about a
task's OUTPUT. ``reconcile`` uses a FULL announcement to skip the expensive
status-reporter walk for the lifecycle verdict; the aggregate integrity gate
still independently verifies every result. A marker never vouches for content.
"""

from __future__ import annotations

import shlex

from hpc_agent import errors
from hpc_agent.infra import remote

__all__ = [
    "read_announcements",
    "ANNOUNCE_SUBPATH",
    "ANNOUNCE_STATE_COMPLETE",
    "ANNOUNCE_STATE_FAILED",
]

# Location + filename-state vocabulary, kept in LOCK-STEP with the standalone
# cluster-side dispatcher (``execution/mapreduce/dispatch.py``:
# ``_ANNOUNCE_DIRNAME`` / ``_ANNOUNCE_STATE_COMPLETE`` / ``_ANNOUNCE_STATE_FAILED``).
# The dispatcher ships to the compute node WITHOUT ``hpc_agent`` on the path, so
# it cannot import these — the duplication is by design (the standalone-boundary
# carve-out in ``docs/internals/engineering-principles.md``) and pinned equal by
# ``tests/ops/monitor/test_announce.py``.
ANNOUNCE_SUBPATH = ".hpc/announce"
ANNOUNCE_STATE_COMPLETE = "complete"
ANNOUNCE_STATE_FAILED = "failed"

# Positive-evidence ack (same discipline as reconcile's wave/alive listings):
# the shell echoes this token ONLY after a successful ``cd`` into the announce
# dir, so an ABSENT ack — a ``cd`` that failed because the dir doesn't exist yet
# (a pre-announce run/wheel) or a silently truncated read — reads as "no
# announcements", never as a spurious zero count that could mis-settle a run. A
# genuinely empty-but-present dir still ``cd``s OK and reports zero counts.
_ANNOUNCE_ACK = "__HPC_ANNOUNCE_ACK__"


def read_announcements(
    *, ssh_target: str, remote_path: str, run_id: str, task_count: int
) -> dict[str, int]:
    """Return ``{announced, complete, failed, missing}`` from the cluster markers.

    ONE bounded ssh exec: ``cd`` into ``<remote_path>/.hpc/announce/<run_id>``
    and, only on success, echo the ack then two pure ``ls … | wc -l`` counts —
    one per filename-encoded state. ``announced = complete + failed``;
    ``missing = max(0, task_count - announced)``.

    A missing announce dir (a pre-announce run) or a read carrying no positive
    ack returns all-zero counts (``announced == 0``) — the capability signal the
    caller uses to fall through to the legacy probe path unchanged. An ssh
    TRANSPORT failure (rc != 0, e.g. rc 255) raises
    :class:`~hpc_agent.errors.RemoteCommandFailed` so the caller never reads a
    connectivity blip as "nothing announced".
    """
    announce_dir = f"{remote_path.rstrip('/')}/{ANNOUNCE_SUBPATH}/{run_id}"
    n_complete = f'"$(ls task_*.{ANNOUNCE_STATE_COMPLETE} 2>/dev/null | wc -l)"'
    n_failed = f'"$(ls task_*.{ANNOUNCE_STATE_FAILED} 2>/dev/null | wc -l)"'
    cmd = (
        f"cd {shlex.quote(announce_dir)} 2>/dev/null "
        f"&& printf '%s\\n' {shlex.quote(_ANNOUNCE_ACK)} "
        f"&& printf 'complete=%s\\n' {n_complete} "
        f"&& printf 'failed=%s\\n' {n_failed}; true"
    )
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"announce read failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines()]
    if _ANNOUNCE_ACK not in lines:
        # No positive ack: the announce dir doesn't exist yet (pre-announce run)
        # or the read was truncated. Treat as "no announcements" — the caller
        # falls through to the legacy probe path unchanged.
        return {"announced": 0, "complete": 0, "failed": 0, "missing": max(0, int(task_count))}
    complete = _parse_count(lines, "complete=")
    failed = _parse_count(lines, "failed=")
    announced = complete + failed
    return {
        "announced": announced,
        "complete": complete,
        "failed": failed,
        "missing": max(0, int(task_count) - announced),
    }


def _parse_count(lines: list[str], prefix: str) -> int:
    """Parse the integer from the first ``<prefix><int>`` line, else 0."""
    for line in lines:
        if line.startswith(prefix):
            try:
                return int(line[len(prefix) :])
            except ValueError:
                return 0
    return 0
