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
from collections.abc import Callable, Sequence

from hpc_agent import errors
from hpc_agent.infra import remote

__all__ = [
    "read_announcements",
    "read_announcements_batch",
    "wait_for_announce_change",
    "ANNOUNCE_SUBPATH",
    "ANNOUNCE_STATE_COMPLETE",
    "ANNOUNCE_STATE_FAILED",
    "ANNOUNCE_RUN_TERMINAL",
]

# Location + filename-state vocabulary, kept in LOCK-STEP with the standalone
# cluster-side dispatcher (``execution/mapreduce/dispatch.py``:
# ``_ANNOUNCE_DIRNAME`` / ``_ANNOUNCE_STATE_COMPLETE`` / ``_ANNOUNCE_STATE_FAILED``
# / ``_ANNOUNCE_RUN_TERMINAL``). The dispatcher ships to the compute node WITHOUT
# ``hpc_agent`` on the path, so it cannot import these — the duplication is by
# design (the standalone-boundary carve-out in
# ``docs/internals/engineering-principles.md``) and pinned equal by
# ``tests/ops/monitor/test_announce.py``.
ANNOUNCE_SUBPATH = ".hpc/announce"
ANNOUNCE_STATE_COMPLETE = "complete"
ANNOUNCE_STATE_FAILED = "failed"

# Run-level terminal WAKE marker (P3, docs/design/crash-only-monitoring.md).
# The dispatcher best-effort touches ONE ``<run_dir>/.run_terminal`` file whenever
# a task announces a terminal state, giving the per-host census WAITER
# (:func:`wait_for_announce_change`) a single well-known filename to short-circuit
# its remote poll loop on. It is DATA ONLY: doctrine row 11 — a marker WAKES the
# poller, it NEVER settles the run. A forged / premature ``.run_terminal`` wakes
# the waiter; the control plane then does a real census read
# (:func:`read_announcements`), and a non-terminal census keeps the run watching.
# Kept in lock-step with the standalone dispatcher's ``_ANNOUNCE_RUN_TERMINAL``.
ANNOUNCE_RUN_TERMINAL = ".run_terminal"

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
    """Return ``{present, announced, complete, failed, missing}`` from the markers.

    ONE bounded ssh exec: ``cd`` into ``<remote_path>/.hpc/announce/<run_id>``
    and, only on success, echo the ack then two pure ``ls … | wc -l`` counts —
    one per filename-encoded state. ``announced = complete + failed``;
    ``missing = max(0, task_count - announced)``.

    ``present`` is the capability signal — ``True`` iff the positive ack was
    seen, i.e. the announce dir EXISTS (this is an announce-era run whose
    dispatcher has STARTED). The dispatcher creates the dir EAGERLY at run start
    (rank 6, ``docs/plans/latency-audit-2026-07-15``), so ``present`` flips as
    soon as ANY array task begins executing — not only once the first task
    finishes. A missing announce dir (a pre-announce-wheel run, or a still-queued
    run whose dispatcher has not started yet) or a read carrying no positive ack
    returns ``present == False`` with all-zero counts (``announced == 0``): the
    caller falls through to the legacy probe / reporter-walk path. An ssh
    TRANSPORT failure (rc != 0, e.g. rc 255) raises
    :class:`~hpc_agent.errors.RemoteCommandFailed` so the caller never reads a
    connectivity blip as "nothing announced".

    Phase-1 consumers (``reconcile``) key off ``announced > 0`` and ignore
    ``present``; the Phase-2 monitor poll loop keys off ``present`` to prefer
    this ONE-readdir census over the per-task reporter walk for the whole
    lifecycle (``docs/design/crash-only-monitoring.md``).
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
        # No positive ack: the announce dir doesn't exist yet (a pre-announce-
        # wheel run, or a still-queued run whose dispatcher hasn't started) or the
        # read was truncated. Treat as "no announcements" — the caller falls
        # through to the legacy probe path unchanged.
        return {
            "present": 0,
            "announced": 0,
            "complete": 0,
            "failed": 0,
            "missing": max(0, int(task_count)),
        }
    complete = _parse_count(lines, "complete=")
    failed = _parse_count(lines, "failed=")
    announced = complete + failed
    return {
        "present": 1,
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


# Positive-evidence ack for the batched read + the remote WAITER, same discipline
# as ``_ANNOUNCE_ACK``: an ABSENT ack is positive proof the read/wait was severed
# mid-flight (a NAT idle-drop, a reaper) rather than a genuine empty result, so
# the caller degrades rather than mis-settling a run on truncated bytes.
_BATCH_ACK = "__HPC_ANNOUNCE_BATCH_ACK__"
_WAIT_ACK = "__HPC_ANNOUNCE_WAIT_ACK__"
_WAIT_WOKE = "__HPC_ANNOUNCE_WOKE__"


def read_announcements_batch(
    *, ssh_target: str, remote_path: str, run_task_counts: dict[str, int]
) -> dict[str, dict[str, int]]:
    """Census MANY runs on one login node in ONE bounded ssh exec (F4, per-host fold).

    The per-run :func:`read_announcements` costs one dial each; a fleet of N runs
    on the same login node therefore pays N dials per monitor tick (the run-12
    connection-storm class, one plane down). This folds the census for the whole
    fleet into a SINGLE exec — one ``cd``+``ls`` per run dir, all in one remote
    shell — so the fleet census is 1 exec/tick regardless of run count (the
    ``batch-status`` discipline applied to the announce plane).

    Returns ``{run_id: {present, announced, complete, failed, missing}}`` — the
    SAME per-run shape :func:`read_announcements` returns, so a caller can swap the
    two freely. Ack-gated as a whole (``_BATCH_ACK``): an ssh TRANSPORT failure
    (rc 255) raises :class:`~hpc_agent.errors.RemoteCommandFailed`; a severed read
    that dropped the ack degrades every run to not-present (all-zero, the caller
    falls through per run). A run whose dir does not exist yet reports ``present:
    0`` individually — exactly as the per-run reader does.
    """
    if not run_task_counts:
        return {}
    root = f"{remote_path.rstrip('/')}/{ANNOUNCE_SUBPATH}"
    parts = [f"printf '%s\\n' {shlex.quote(_BATCH_ACK)}"]
    for run_id in run_task_counts:
        run_dir = f"{root}/{run_id}"
        n_complete = f'"$(ls task_*.{ANNOUNCE_STATE_COMPLETE} 2>/dev/null | wc -l)"'
        n_failed = f'"$(ls task_*.{ANNOUNCE_STATE_FAILED} 2>/dev/null | wc -l)"'
        # Per-run present ack echoed ONLY after a successful cd into its dir, so an
        # absent per-run marker (dir not created yet) reads present:0 for that run
        # while the batch as a whole still acked.
        parts.append(
            f"( cd {shlex.quote(run_dir)} 2>/dev/null "
            f"&& printf 'run=%s present=1 complete=%s failed=%s\\n' "
            f"{shlex.quote(run_id)} {n_complete} {n_failed} )"
        )
    cmd = "; ".join(parts) + "; true"
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise errors.RemoteCommandFailed(
            f"batch announce read failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    lines = [ln.strip() for ln in proc.stdout.splitlines()]
    out: dict[str, dict[str, int]] = {}
    if _BATCH_ACK not in lines:
        # Severed read: no run vouched-for. Degrade EVERY run to not-present so the
        # caller falls through to its per-run legacy path, never a spurious zero.
        for run_id, tc in run_task_counts.items():
            out[run_id] = {
                "present": 0,
                "announced": 0,
                "complete": 0,
                "failed": 0,
                "missing": max(0, int(tc)),
            }
        return out
    present_rows = _parse_batch_rows(lines)
    for run_id, tc in run_task_counts.items():
        row = present_rows.get(run_id)
        if row is None:
            out[run_id] = {
                "present": 0,
                "announced": 0,
                "complete": 0,
                "failed": 0,
                "missing": max(0, int(tc)),
            }
            continue
        complete, failed = row
        announced = complete + failed
        out[run_id] = {
            "present": 1,
            "announced": announced,
            "complete": complete,
            "failed": failed,
            "missing": max(0, int(tc) - announced),
        }
    return out


def _parse_batch_rows(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Parse ``run=<id> present=1 complete=<n> failed=<n>`` rows into ``{id: (c, f)}``."""
    rows: dict[str, tuple[int, int]] = {}
    for line in lines:
        if not line.startswith("run="):
            continue
        fields: dict[str, str] = {}
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                fields[k] = v
        rid = fields.get("run")
        if not rid:
            continue
        try:
            rows[rid] = (int(fields.get("complete", "0")), int(fields.get("failed", "0")))
        except ValueError:
            continue
    return rows


def wait_for_announce_change(
    *,
    ssh_target: str,
    remote_path: str,
    run_ids: Sequence[str],
    deadline_seconds: float,
    poll_seconds: float = 2.0,
    stamp: Callable[[], None] | None = None,
    _ssh_run: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Long-poll a login node for an announce-marker change — the wait moved REMOTE-side (P3).

    Doctrine, docs/design/crash-only-monitoring.md:

    * **C1 — inotify FORBIDDEN.** On NFS/Lustre an inotify watch never fires for a
      write from ANOTHER node (the compute node writes the marker; the login node
      watching sees nothing). The remote body is a plain ``sh`` poll loop —
      ``sleep`` + ``ls``/opendir forcing a readdir revalidation — so the win is
      moving the poll to the SAME filesystem client the dispatcher writes on, not a
      kernel notification.
    * **C4 — one waiter per HOST multiplexing all runs.** *run_ids* are the runs on
      this login node; the loop watches all their announce dirs in ONE remote shell
      (one dial), so a fleet of N runs costs one waiter, not N. Runs on the engine's
      persistent asyncssh channel via :func:`remote.ssh_run` (the one-shot leg is
      the ``run_capture_bounded`` bounded runner — doctrine row 13 / L274).
    * **C2 — a wake is a HINT, never a settle** (doctrine row 11). This returns
      ``{"woke": bool, "acked": bool, ...}`` — a signal to READ, not a verdict. The
      caller does a real census (:func:`read_announcements`) after every wake; a
      forged / premature ``.run_terminal`` wakes the loop but the census reads
      non-terminal and the run keeps watching (fire test, not prose).
    * **watchdog during the blocked wait** (doctrine row 13). The whole wait is one
      blocking dial; *stamp* is called ONCE right before it enters so a poller that
      dies mid-wait leaves a ``next_tick_due`` that lapses — the caller passes a
      closure over the ONE ``stamp_watchdog_tick`` definition (row L253).

    The remote loop echoes ``_WAIT_WOKE`` the instant it observes a NEW marker
    filename (any ``task_*`` count change) OR a ``.run_terminal`` marker in any
    watched dir, and always echoes ``_WAIT_ACK`` before exiting. An ABSENT ack is
    positive proof the wait was severed mid-flight (``acked == False``): the caller
    treats a severed wait as "no wake, fall through and re-census" rather than
    trusting truncated bytes — the severed-vs-empty distinction, preserved per host.
    """
    if stamp is not None:
        # Route the pre-wait liveness stamp through the caller's closure over the
        # ONE stamp_watchdog_tick definition. A dead poller then lapses its
        # next_tick_due while blocked in the single remote dial.
        stamp()
    runner = _ssh_run if _ssh_run is not None else remote.ssh_run
    if not run_ids:
        return {"woke": False, "acked": False, "waited": False}
    root = f"{remote_path.rstrip('/')}/{ANNOUNCE_SUBPATH}"
    dirs = [f"{root}/{shlex.quote(rid).strip(chr(39))}" for rid in run_ids]
    # Build the poll loop. ``sig`` is the sorted marker inventory across every
    # watched run dir; a change in it (a new/removed task_* marker) OR any
    # ``.run_terminal`` wake marker breaks the loop. Bounded by an absolute epoch
    # deadline so the remote half self-terminates even if the client link drops.
    ls_dirs = " ".join(shlex.quote(f"{root}/{rid}") for rid in run_ids)
    term_tests = " || ".join(
        f"test -f {shlex.quote(f'{root}/{rid}/{ANNOUNCE_RUN_TERMINAL}')}" for rid in run_ids
    )
    poll = max(1, int(poll_seconds))
    budget = max(1, int(deadline_seconds))
    remote_body = (
        f"__deadline=$(( $(date +%s) + {budget} )); "
        f"__sig() {{ ls {ls_dirs} 2>/dev/null | sort | cksum; }}; "
        f"__init=$(__sig); "
        f"while [ $(date +%s) -lt $__deadline ]; do "
        f"  if {term_tests}; then printf '%s\\n' {shlex.quote(_WAIT_WOKE)}; break; fi; "
        f'  __cur=$(__sig); if [ "$__cur" != "$__init" ]; then '
        f"    printf '%s\\n' {shlex.quote(_WAIT_WOKE)}; break; fi; "
        f"  sleep {poll}; "
        f"done; "
        f"printf '%s\\n' {shlex.quote(_WAIT_ACK)}"
    )
    # Client-side timeout = the remote budget plus slack for the dial + last sleep,
    # so the client's bounded runner reaps a wedged link only AFTER the remote
    # self-destruct has had its chance (never the reverse — that would orphan the
    # remote half). ``dirs`` is referenced to keep the quoted list live for callers
    # that introspect the command; the loop uses ``ls_dirs``.
    _ = dirs
    client_timeout = float(budget) + float(poll) + 15.0
    try:
        proc = runner(remote_body, ssh_target=ssh_target, timeout=client_timeout)
    except Exception:  # noqa: BLE001 — a wait fault is a non-event; caller re-censuses
        return {"woke": False, "acked": False, "waited": True}
    stdout = getattr(proc, "stdout", "") or ""
    rc = getattr(proc, "returncode", 0)
    lines = [ln.strip() for ln in stdout.splitlines()]
    acked = _WAIT_ACK in lines
    woke = _WAIT_WOKE in lines
    if rc != 0:
        # A transport-level non-zero (255) that still produced no ack: severed.
        acked = acked and rc == 0
    return {"woke": bool(woke and acked), "acked": bool(acked), "waited": True}
