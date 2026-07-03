"""OS notification for the scheduled ``doctor`` watchdog (§5).

When the OS-scheduled ``doctor`` scan finds a stalled/orphaned run it must
*surface* the drafted re-arm proposal, not print JSON into a scheduler log
nobody reads (design §5: "Either side dying is loud"). This module raises a
dependency-free OS notification carrying the proposal summary.

Doctrine: **notify only, never act.** The notification is a surfacing of the
already-drafted proposal — a successor session (or the human) answers `y`/nudge.
The watchdog never restarts anything.

Mechanism (chosen for reliability without third-party deps):

* **Windows** — ``msg.exe <user> <text>``: non-blocking, ships on Windows
  Pro/Enterprise, no module install. A modal ``MessageBox`` would wedge a
  headless scheduled task, and a real toast needs an AppId-registered module —
  both worse than ``msg`` for an unattended firing. On Home editions ``msg`` is
  absent, so we fall back to the loud log file (below).
* **POSIX** — ``notify-send`` when present (the ``libnotify`` CLI, on virtually
  every desktop), else the loud log file.
* **Fallback (any platform)** — append to ``<journal_home>/doctor.alerts.log``.
  This is the guaranteed floor: it always surfaces the alert *somewhere* durable
  even with no desktop session, no ``msg``, no ``notify-send``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.run_record import journal_dir

_NOTIFY_TIMEOUT_SEC = 10


def summarize_proposals(proposals: list[dict[str, Any]]) -> str:
    """One-line, human-facing summary of the drafted re-arm proposals.

    Leads with the first stalled run's drafted proposal (already authored by
    ``doctor``); a ``(+N more)`` suffix counts the rest so a single toast never
    truncates the tail into silence.
    """
    if not proposals:
        return "hpc-agent doctor: no stalled runs."
    head = proposals[0]
    run_id = head.get("run_id", "?")
    since = head.get("last_tick_at") or "an unknown time"
    text = f"hpc-agent doctor: driver stalled since {since}, run {run_id} — re-arm?"
    extra = len(proposals) - 1
    if extra > 0:
        text += f" (+{extra} more stalled)"
    return text


def _append_alert_log(text: str, *, experiment_dir: Path) -> str:
    """Append *text* to the loud fallback log; return its path."""
    log_path = journal_dir(experiment_dir) / "doctor.alerts.log"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"{utcnow_iso()} {text}\n")
    return str(log_path)


def _try_run(argv: list[str]) -> bool:
    """Run *argv* best-effort; True iff it exists and exits 0."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_NOTIFY_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def raise_stall_notification(
    proposals: list[dict[str, Any]], *, experiment_dir: Path
) -> dict[str, Any]:
    """Surface *proposals* as an OS notification. Best-effort, never acts.

    Always writes the loud fallback log (the durable floor), then additionally
    fires the platform notifier when one is available. Returns the delivery
    record ``{mechanism, delivered, text, log_path}`` — ``mechanism`` is the
    richest channel that fired (``msg`` / ``notify-send`` / ``logfile``).
    """
    text = summarize_proposals(proposals)
    log_path = _append_alert_log(text, experiment_dir=experiment_dir)

    mechanism = "logfile"
    if os.name == "nt":
        user = os.environ.get("USERNAME") or "*"
        if _try_run(["msg", user, text]):
            mechanism = "msg"
    elif shutil.which("notify-send") and _try_run(["notify-send", "hpc-agent doctor", text]):
        mechanism = "notify-send"

    return {
        "mechanism": mechanism,
        "delivered": True,
        "text": text,
        "log_path": log_path,
    }
