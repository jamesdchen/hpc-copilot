#!/usr/bin/env python3
"""Cluster-side heartbeat watcher (hpc-copilot hybrid monitor, design §5).

Stdlib-only. Shipped to the cluster by the ``watcher-install`` verb and
re-fired by cron / scrontab / a self-resubmitting job — NEVER a long-running
loop (the scheduler re-fires it). Each firing, for every run directory it is
pointed at:

  * (re)writes ``<run>/.hpc_watcher_status.json`` — a liveness heartbeat the
    client can read to prove the cluster side is alive;
  * reads ``<run>/.hpc_last_read`` (stamped by the laptop client on every
    status poll) and, when it is missing or older than ``--stale-sec``, writes
    ``<run>/.hpc_watcher_ALARM`` naming the staleness so the client surfaces it
    loudly. When the client is reading again a stale ALARM is cleared, so a
    transient laptop-sleep blip self-heals.

Either side dying is loud: if the client stops reading, this writes an ALARM;
if this watcher stops firing, the heartbeat's ``ts`` goes stale and the client
notices.

MUST NOT import ``hpc_agent`` (the standalone-file rule): it runs under the
cluster's bare python, which has no framework install.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time

STATUS_NAME = ".hpc_watcher_status.json"
LAST_READ_NAME = ".hpc_last_read"
ALARM_NAME = ".hpc_watcher_ALARM"


def _iso(ts):
    """Render an epoch seconds value as an ISO-8601 UTC string."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def _atomic_write(path, text):
    """Write *text* to *path* via a temp file + rename (no torn reads)."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _process_run_dir(run_dir, stale_sec, job_name, now):
    """Heartbeat + staleness check for one run dir; return True iff alarming."""
    status_path = os.path.join(run_dir, STATUS_NAME)
    last_read_path = os.path.join(run_dir, LAST_READ_NAME)
    alarm_path = os.path.join(run_dir, ALARM_NAME)

    try:
        last_read_mtime = os.path.getmtime(last_read_path)
    except OSError:
        last_read_mtime = None

    if last_read_mtime is None:
        age = None
        stale = True
        reason = (
            f"client has never stamped {LAST_READ_NAME} (no laptop status read "
            "since the watcher was installed)"
        )
    else:
        age = now - last_read_mtime
        stale = age > stale_sec
        reason = (
            f"client has not read status since {_iso(last_read_mtime)} "
            f"({int(age)} s ago, threshold {stale_sec} s)"
        )

    status = {
        "ts": _iso(now),
        "run_dir": run_dir,
        "job_name": job_name,
        "stale_sec": stale_sec,
        "last_read": _iso(last_read_mtime) if last_read_mtime is not None else None,
        "last_read_age_sec": int(age) if age is not None else None,
        "alarm": bool(stale),
    }
    _atomic_write(status_path, json.dumps(status, indent=2, sort_keys=True) + "\n")

    if stale:
        _atomic_write(alarm_path, f"{reason}\nwatcher_ts={_iso(now)}\nrun_dir={run_dir}\n")
    else:
        # Client is reading again: clear a stale ALARM so a transient gap heals.
        with contextlib.suppress(OSError):
            os.remove(alarm_path)
    return bool(stale)


def main(argv=None):
    parser = argparse.ArgumentParser(description="hpc-agent cluster-side watcher (one firing).")
    parser.add_argument(
        "--run-dir",
        action="append",
        required=True,
        dest="run_dirs",
        help="A run directory to watch (repeatable).",
    )
    parser.add_argument(
        "--stale-sec",
        type=int,
        default=1800,
        help="Alarm when .hpc_last_read is missing or older than this (seconds).",
    )
    parser.add_argument(
        "--job-name",
        default="",
        help="Run's job name, recorded in the heartbeat for legibility.",
    )
    ns = parser.parse_args(argv)

    now = time.time()
    for run_dir in ns.run_dirs:
        try:
            _process_run_dir(run_dir, ns.stale_sec, ns.job_name, now)
        except OSError as exc:
            # A single bad run dir must not stop the others; surface it.
            sys.stderr.write(f"hpc_watcher: {run_dir}: {exc}\n")
    # Always exit 0 so cron does not email on every stale firing — loudness
    # lives in the ALARM file the client surfaces, not in the exit code.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
