"""Process-liveness substrate — the ONE PID-existence probe the control plane uses.

Historically this decision had TWO byte-divergent hand-rolled copies that could
DISAGREE on a zombie / access-denied edge:

* ``_kernel/lifecycle/detached.py`` — win32 ``OpenProcess`` +
  ``GetExitCodeProcess`` == ``STILL_ACTIVE`` (259); POSIX ``os.kill(pid, 0)``.
* ``infra/ssh_slots.py`` — win32 ``OpenProcess`` + ``GetLastError() != 87``;
  POSIX ``os.kill(pid, 0)``.

The win32 halves diverged: the detached copy read a process that *exited with
code 259* as alive and an *exited-but-open-handle* pid as dead, while the
ssh_slots copy keyed off ``ERROR_INVALID_PARAMETER`` (87). Two definitions of
one substrate fact is exactly the one-definition doctrine's target
(``docs/internals/engineering-principles.md``). PID liveness is *substrate* (how
to probe a process — question 1 of the library-knowledge test), core only
DISPATCHES to the library (question 2), and ``psutil`` is import-safe and light
(question 3) — so the probe is outsourced to :func:`psutil.pid_exists`, the
canonical cross-platform implementation. It also closes the win32 footgun the
ssh_slots docstring flagged: ``os.kill(pid, 0)`` on Windows calls
``TerminateProcess`` and can never be used as a probe (psutil handles this).

Audit 2026-07-07 (finding #1). Both former call sites now route here: this is
the single definition, and both preserve a module-level ``_pid_alive`` seam that
tests monkeypatch — but the seam FORWARDS here, it does not re-implement.
"""

from __future__ import annotations

import psutil


def pid_alive(pid: int) -> bool:
    """Whether *pid* names a running process on this host.

    A dead pid is a reclaimable stale lease / slot; a live one refuses reclaim.
    The ``pid <= 0`` short-circuit is preserved from both former hand-rolls
    (0 and negatives are group/self sentinels for ``os.kill``, never a probe
    target — and it keeps :func:`psutil.pid_exists` off the win32 "System Idle
    Process" pid 0). Everything else delegates to :func:`psutil.pid_exists`,
    which returns ``True`` for POSIX zombies (as ``os.kill(pid, 0)`` also did)
    and handles the Windows ``os.kill``-is-``TerminateProcess`` gotcha.
    """
    if pid <= 0:
        return False
    return bool(psutil.pid_exists(pid))
