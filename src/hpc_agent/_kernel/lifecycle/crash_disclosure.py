"""Worker-side crash disclosure — the run-#13 finding 2 seam.

A DETACHED worker (:mod:`hpc_agent._kernel.lifecycle.detached`) runs its cluster
work to terminal in a subprocess whose stdout/stderr are captured to a
``_detached/*.log``. Run-#13 finding 2, live: a ``submit-s2`` worker died exit-2
when a VPN drop severed its ``scp`` child, and the log's final non-heartbeat line
was a normal ``[transport] progress`` line — **no traceback, no child stderr, no
exit-path disclosure**. The heartbeat proved the worker was alive with a busy
``ssh.exe`` child moments before death, but nothing was flushed on the way out, so
the harness-written terminal's claim that "the worker log carries the disclosed
failure" was FALSE.

This module closes the WORKER SIDE of that gap — it makes any exit path leave a
``[fatal]`` block in the log:

* :func:`install_crash_faulthandler` arms :mod:`faulthandler` so a hard signal
  (``SIGSEGV`` / ``SIGABRT`` / ``SIGFPE`` / ``SIGBUS`` / ``SIGILL`` — the killed /
  crashed paths a Python ``except`` can never catch) dumps a native traceback to
  the worker log before the process dies.
* :func:`emit_fatal_block` writes a bounded ``[fatal]`` block for the CATCHABLE
  exit paths — an unhandled exception (type + message + bounded traceback), a
  ``SystemExit`` with a non-zero code, or a non-zero verb return (exit code + the
  last known heartbeat stage) — and flushes it, so the log carries the disclosure
  even when the process is about to exit.
* :func:`log_has_fatal_marker` is the HONEST-TERMINAL read side: a bounded tail of
  the worker log, reporting whether a ``[fatal]`` marker is present and the last
  non-empty line, so the terminal writer can say what the log actually contains
  instead of asserting a disclosure the write path cannot guarantee.

Every function is best-effort and fail-open: the exit path must never gain a new
crash, and disclosure gated OFF outside a detached worker (``HPC_DETACHED_RUN_ID``
unset) so the foreground CLI's console stays clean.
"""

from __future__ import annotations

import contextlib
import faulthandler
import os
import sys
import traceback
from typing import TextIO

__all__ = [
    "FATAL_MARKER",
    "emit_fatal_block",
    "install_crash_faulthandler",
    "log_has_fatal_marker",
]

#: The marker the honest terminal scans for. A single token so a bounded tail read
#: can confirm "the worker disclosed on its way out" without parsing.
FATAL_MARKER = "[fatal]"

#: Bound on the traceback tail folded into a ``[fatal]`` block. A pathological
#: recursion traceback can be megabytes; the tail carries the actual failure
#: frame, which is what a post-mortem needs.
_TRACEBACK_TAIL_CHARS = 4000

#: Bound on the log tail the honest-terminal check reads. Large enough to clear a
#: run of heartbeat lines and find a ``[fatal]`` block flushed just before exit.
_LOG_TAIL_BYTES = 16_384


def _is_detached_worker() -> bool:
    """Disclosure is a no-op outside a detached worker (its stderr == the log)."""
    return bool(os.environ.get("HPC_DETACHED_RUN_ID"))


def install_crash_faulthandler(stream: TextIO | None = None) -> None:
    """Arm :mod:`faulthandler` to dump a native traceback on a fatal signal.

    No-op outside a detached worker. *stream* defaults to ``sys.stderr`` (the
    child's captured log). ``faulthandler`` captures the stream's file descriptor
    at enable time and writes to it directly on ``SIGSEGV`` / ``SIGABRT`` / etc.,
    so a hard crash that no Python ``except`` can catch still leaves a traceback in
    the log. Best-effort: a platform that cannot arm it degrades silently.
    """
    if not _is_detached_worker():
        return
    target = stream if stream is not None else sys.stderr
    # A platform that cannot arm faulthandler must never break the worker.
    with contextlib.suppress(Exception):
        faulthandler.enable(file=target, all_threads=True)


def emit_fatal_block(
    *,
    exc: BaseException | None = None,
    exit_code: int | str | None = None,
    last_stage: str | None = None,
    stream: TextIO | None = None,
) -> bool:
    """Write a bounded ``[fatal]`` block to the worker log; return whether it wrote.

    No-op (returns ``False``) outside a detached worker. Discloses whichever exit
    path fired:

    * *exc* — an unhandled exception or a non-zero ``SystemExit``: the exception
      type, message, and a bounded traceback tail.
    * *exit_code* — a non-zero verb return with no exception: the exit code.

    *last_stage* (the newest heartbeat line, e.g.
    ``[hb] alive 480s | child=ssh.exe cpu=17.2s``) is folded in as the last known
    stage — the VPN-severed worker's log now carries "was pulling via ssh.exe" even
    when the child stderr itself was lost. The block is flushed before returning so
    it survives an immediate process exit. Fail-open: any write error is swallowed
    and reported as ``False``.
    """
    if not _is_detached_worker():
        return False
    target = stream if stream is not None else sys.stderr
    try:
        lines = [f"{FATAL_MARKER} detached worker exit-path disclosure"]
        if exc is not None:
            lines.append(f"{FATAL_MARKER} {type(exc).__name__}: {exc}")
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if len(tb) > _TRACEBACK_TAIL_CHARS:
                tb = "...(traceback head elided)...\n" + tb[-_TRACEBACK_TAIL_CHARS:]
            lines.append(tb.rstrip("\n"))
        if exit_code is not None:
            lines.append(f"{FATAL_MARKER} exit_code={exit_code}")
        if last_stage:
            lines.append(f"{FATAL_MARKER} last known stage: {last_stage}")
        target.write("\n".join(lines) + "\n")
        target.flush()
        return True
    except Exception:  # noqa: BLE001 — the exit path must never gain a new crash
        return False


def log_has_fatal_marker(log_path: str | os.PathLike[str] | None) -> tuple[bool, str]:
    """Bounded tail read of the worker log for the honest terminal.

    Returns ``(has_fatal, last_nonempty_line)``. ``has_fatal`` is True iff a
    :data:`FATAL_MARKER` appears in the last :data:`_LOG_TAIL_BYTES` of the log —
    the "the worker disclosed on its way out" signal. ``last_nonempty_line`` is the
    final non-blank line of that tail (for the "log ends with <line>" honest
    message when no marker is present). Fail-open: a missing/unreadable log yields
    ``(False, "")`` so the terminal writer falls back to the no-disclosure branch.
    """
    if not log_path:
        return False, ""
    try:
        with open(log_path, "rb") as fh:
            try:
                fh.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
            except OSError:
                fh.seek(0)  # log shorter than the tail bound
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False, ""
    has_fatal = FATAL_MARKER in tail
    last_line = ""
    for line in reversed(tail.splitlines()):
        if line.strip():
            last_line = line.strip()
            break
    return has_fatal, last_line
