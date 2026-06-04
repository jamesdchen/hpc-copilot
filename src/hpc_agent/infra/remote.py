"""SSH and rsync utilities for remote HPC operations.

Provides thin wrappers around ssh/rsync so cluster commands can be
executed from a local machine without paramiko or other dependencies.

All functions take a single opaque ``ssh_target`` plus ``remote_path``.
``ssh_target`` is whatever ``ssh``/``scp``/``rsync`` accept as a
destination — either an explicit ``user@host`` (e.g.
``user@discovery2.usc.edu``) **or** an OpenSSH ``Host`` alias from
``~/.ssh/config`` (e.g. ``usc-discovery``). The alias form is preferred
because it lets ``IdentityFile`` / ``User`` / ``Hostname`` settings in
the user's ssh config flow through without us having to model them.

Every subprocess invocation in this module enforces a timeout so a flaky
cluster connection or paused rsync cannot block ``/submit``, ``/status``,
or ``/aggregate`` indefinitely.  The defaults are :data:`SSH_TIMEOUT_SEC`
for SSH/scp commands and :data:`RSYNC_TIMEOUT_SEC` for rsync transfers.
Callers may override per-call by passing ``timeout=`` (in seconds), or
disable enforcement entirely by passing ``timeout=None``.  When the
underlying child exceeds the timeout, the wrapper raises
:class:`TimeoutError` with a message that names the target and a snippet
of the command being run.
"""

from __future__ import annotations

__all__ = [
    "SSH_TIMEOUT_SEC",
    "RSYNC_TIMEOUT_SEC",
    "ssh_run",
]

import contextlib
import os
import select
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Final

from hpc_agent.infra.ssh_options import run_with_named_pipe_retry, ssh_argv

if TYPE_CHECKING:
    from collections.abc import Callable


def _env_int(name: str, default: int) -> int:
    """Return ``int(os.environ[name])`` if set to a valid int, else *default*.

    Used so site operators can tune the SSH/rsync timeouts without a
    code edit (campus clusters with slow login nodes / NFS mounts often
    need higher ceilings). Invalid values fall back to the default so a
    typo can't disable timeout enforcement.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Default subprocess timeouts (in seconds).  ``ssh_run`` covers login-node
# commands, including the status-reporter SSH calls that exec python and may
# need a few seconds; 60s is a generous ceiling for those.  ``rsync`` runs
# may legitimately move large repos over slow links, so we allow up to 30
# minutes before declaring the transfer hung.
#
# Both are tunable via env-var (``HPC_SSH_TIMEOUT_SEC`` /
# ``HPC_RSYNC_TIMEOUT_SEC``) so a slow campus cluster can raise the
# ceiling without a fork.
SSH_TIMEOUT_SEC = _env_int("HPC_SSH_TIMEOUT_SEC", 60)
RSYNC_TIMEOUT_SEC = _env_int("HPC_RSYNC_TIMEOUT_SEC", 1800)

# ``validate_remote_path``, ``validate_ssh_target``, and
# ``parse_remote_json`` live in :mod:`hpc_agent.infra.ssh_validation`.
# Binary resolution + SSH option assembly live in
# :mod:`hpc_agent.infra.ssh_options`; :func:`ssh_run` builds its argv through
# that module's :func:`ssh_argv` seam — the one place that owns native-binary
# resolution + BatchMode + POSIX multiplexing / the Windows ControlMaster override.


# Sentinel marker meaning "caller did not specify a timeout".  We need a
# distinct value (not ``None``) because ``timeout=None`` is the documented
# escape hatch for disabling enforcement entirely (e.g. legitimately
# long-running streaming SSH commands).  ``object()`` gives us a unique
# identity that no caller can accidentally collide with.
_DEFAULT: Final[Any] = object()

# ``DEFAULT_RSYNC_EXCLUDES`` and the file-transport helpers
# (``rsync_push`` / ``rsync_pull`` / ``deploy_runtime`` / ``run_combiner``
# / ``run_combiner_checked``) live in :mod:`hpc_agent.infra.transport`.
# Callers import them from there directly.


def _truncate(text: str, limit: int = 120) -> str:
    """Return *text* truncated to *limit* characters with an ellipsis suffix."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


# Rate-limit / throttle markers in stderr that indicate the cluster's sshd
# refused the connection (MaxStartups, fail2ban, PAM session limits) — i.e.
# transient, retryable errors. A plain wrong-host or auth failure is NOT
# retried. Match case-insensitively to be robust to different OpenSSH /
# distro spellings.
_SSH_THROTTLE_MARKERS: tuple[str, ...] = (
    # Suffix-trimmed so we match both "Connection closed by remote host"
    # and "Connection closed" (sshd may log either).
    "ssh_exchange_identification: connection closed",
    "kex_exchange_identification: connection closed",
    "kex_exchange_identification: read: connection reset",
    "connection reset by peer",
    "connection refused",
    # rsync surfaces the underlying ssh failure verbatim plus its own marker:
    "rsync error: error in rsync protocol data stream",
)

# Backoff schedule for transient ssh/rsync failures. Caller sees up to
# 4 retries with delays 2s/4s/8s/16s — total ~30s of waiting. Long enough
# to ride through a sshd MaxStartups burst, short enough that a permanent
# failure surfaces in well under a minute.
_BACKOFF_DELAYS_SEC: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)


def _is_throttle_failure(cp: subprocess.CompletedProcess[str]) -> bool:
    """True if *cp* looks like an ssh rate-limit failure worth retrying.

    We consider non-zero returncode + a known sshd-throttle marker in
    stderr to be transient. A bare timeout (which raises before reaching
    here) is also transient and handled by the caller's except clause.
    """
    if cp.returncode == 0:
        return False
    blob = ((cp.stderr or "") + "\n" + (cp.stdout or "")).lower()
    return any(marker in blob for marker in _SSH_THROTTLE_MARKERS)


def _with_ssh_backoff(
    fn: Callable[[], subprocess.CompletedProcess[str]],
    *,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Call *fn* with exponential-backoff retry on transient ssh failures.

    *fn* is a zero-arg thunk that performs the ssh/scp/rsync subprocess
    and returns its CompletedProcess. We retry on:

    * :class:`TimeoutError` raised by the underlying wrapper, AND
    * non-zero returncode whose stderr matches a known sshd-throttle
      marker (see :data:`_SSH_THROTTLE_MARKERS`).

    Permanent failures (auth refused, host unreachable, command not
    found) return immediately with the failing CompletedProcess.

    *label* is interpolated into the optional log line so the caller's
    diagnostic identifies which step is being retried (e.g. ``"rsync
    push"``, ``"scp dispatch.py"``). Disable retries entirely by setting
    ``HPC_SSH_NO_BACKOFF=1`` (useful in tests that mock subprocess.run).
    """
    if os.environ.get("HPC_SSH_NO_BACKOFF") == "1":
        return fn()

    last_cp: subprocess.CompletedProcess[str] | None = None
    last_exc: Exception | None = None
    for attempt, delay in enumerate((0.0, *_BACKOFF_DELAYS_SEC)):
        if delay > 0:
            time.sleep(delay)
        try:
            cp = fn()
        except TimeoutError as exc:
            last_exc = exc
            last_cp = None
            continue
        last_cp = cp
        last_exc = None
        if not _is_throttle_failure(cp):
            return cp
        # Throttle marker — retry unless we've exhausted the schedule.
        if attempt == len(_BACKOFF_DELAYS_SEC):
            return cp
    # Exhausted retries on TimeoutError specifically.
    if last_exc is not None and last_cp is None:
        raise last_exc
    # Should be unreachable; mypy needs the guarantee.
    assert last_cp is not None, f"_with_ssh_backoff exhausted with no result for {label}"
    return last_cp


# ---------------------------------------------------------------------------
# Capture reader (#209): close-pipes-on-exit, anti backgrounded-child hang
# ---------------------------------------------------------------------------
#
# A plain ``subprocess.run`` (or ``Popen.communicate``) reads each pipe to
# EOF, and EOF only arrives when the *last* writer closes the fd. When a
# remote command backgrounds a child that inherited ssh's stdout/stderr pipe,
# that child keeps the write end open after the foreground process exits — so
# the parent ``ssh`` (and therefore we) block until the child dies or our
# timeout fires. For an unattended agent polling ``status`` that turns a
# finished job into a full ``SSH_TIMEOUT_SEC`` stall.
#
# The reader below borrows the *technique* (not the code, not the dependency)
# from ``remotemanager``'s ``CMD._communicate_with_select`` (MIT,
# https://gitlab.com/l_sim/remotemanager): a ``select()`` loop drains whatever
# stdout/stderr have ready while re-checking ``proc.poll()`` on a fixed
# cadence; the moment the process has exited it does one final *non-blocking*
# drain of the already-buffered bytes and stops, never waiting for EOF. The
# ``ssh_argv`` seam (BatchMode, ControlMaster multiplexing, native-binary
# resolution in :mod:`ssh_options`) is untouched — only the inner read changes.

# select(2) over anonymous pipes is POSIX-only; native Windows has no
# equivalent, and the backgrounded-child hang is itself a POSIX process-group
# artefact, so the reader is gated to POSIX and Windows keeps the blocking
# ``subprocess.run`` path.
_WINDOWS: Final[bool] = sys.platform == "win32"

# Re-check process liveness on this cadence even when no pipe bytes arrive, so
# a backgrounded grandchild holding the pipe open cannot keep us parked in
# ``select()`` past the foreground exit. 0.2s is imperceptible for an
# interactive poll yet bounds post-exit latency tightly.
_SELECT_POLL_INTERVAL_SEC: Final[float] = 0.2

# Bytes pulled per ready-fd read; 64 KiB comfortably exceeds a typical pipe
# buffer so one ``os.read`` drains what ``select`` reported ready.
_READ_CHUNK_BYTES: Final[int] = 65536


def _kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Best-effort kill + wait so a timed-out child isn't left a zombie."""
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def _communicate_select(
    proc: subprocess.Popen[bytes],
    *,
    argv: list[str],
    timeout: float | None,
) -> tuple[str, str]:
    """Drain *proc*'s stdout/stderr, closing our pipe ends the instant the
    child exits rather than waiting for EOF (see the module note above).

    Returns the decoded ``(stdout, stderr)`` (UTF-8, ``errors="replace"`` so a
    stray non-UTF-8 byte in cluster output can't crash a status poll). Raises
    :class:`subprocess.TimeoutExpired` — after killing and reaping *proc* —
    when *timeout* elapses before the foreground process exits; the caller
    translates that into :class:`TimeoutError`, exactly as the blocking path
    does.
    """
    assert proc.stdout is not None and proc.stderr is not None
    out_fd = proc.stdout.fileno()
    err_fd = proc.stderr.fileno()
    buffers: dict[int, bytearray] = {out_fd: bytearray(), err_fd: bytearray()}
    open_fds = {out_fd, err_fd}
    deadline = None if timeout is None else time.monotonic() + timeout

    def _read_ready(block_for: float) -> None:
        """One ``select`` pass: append from every readable fd, drop fds at EOF."""
        if not open_fds:
            return
        try:
            ready, _, _ = select.select(list(open_fds), [], [], block_for)
        except (OSError, ValueError):
            # An fd was closed under us — nothing readable this pass.
            return
        for fd in ready:
            try:
                chunk = os.read(fd, _READ_CHUNK_BYTES)
            except OSError:
                open_fds.discard(fd)
                continue
            if chunk:
                buffers[fd] += chunk
            else:  # EOF on this stream
                open_fds.discard(fd)

    try:
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    assert timeout is not None  # deadline set ⇒ timeout set
                    _kill_and_reap(proc)
                    raise subprocess.TimeoutExpired(
                        argv,
                        timeout,
                        output=bytes(buffers[out_fd]),
                        stderr=bytes(buffers[err_fd]),
                    )
                block_for = min(remaining, _SELECT_POLL_INTERVAL_SEC)
            else:
                block_for = _SELECT_POLL_INTERVAL_SEC

            _read_ready(block_for)

            if proc.poll() is not None:
                # Foreground process is done. Drain only what is already
                # buffered (non-blocking) and stop — do NOT wait for EOF, which
                # a backgrounded child holding the pipe may never send.
                while open_fds:
                    ready, _, _ = select.select(list(open_fds), [], [], 0)
                    if not ready:
                        break
                    for fd in ready:
                        chunk = os.read(fd, _READ_CHUNK_BYTES)
                        if chunk:
                            buffers[fd] += chunk
                        else:
                            open_fds.discard(fd)
                break

            if not open_fds:
                # Both pipes hit real EOF while the process was still being
                # reaped (the common, no-backgrounded-child case): wait for
                # exit, bounded by the deadline.
                wait_for = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=wait_for)
                except subprocess.TimeoutExpired:
                    _kill_and_reap(proc)
                    raise
                break
    finally:
        # We own these pipes (opened via Popen); close our read ends so the fds
        # don't leak even when we bail out early on a backgrounded child.
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError):
                stream.close()

    if proc.returncode is None:
        proc.wait()
    out = bytes(buffers[out_fd]).decode("utf-8", "replace")
    err = bytes(buffers[err_fd]).decode("utf-8", "replace")
    return out, err


def _capture_via_select(
    argv: list[str],
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* capturing stdout/stderr, returning a ``CompletedProcess``.

    POSIX uses the close-pipes-on-exit :func:`_communicate_select` reader so a
    backgrounded remote child can't wedge the read; Windows falls back to the
    blocking ``subprocess.run`` (select(2) over pipes is POSIX-only). Both
    surfaces raise :class:`subprocess.TimeoutExpired` on *timeout*, so the
    caller's single translation to :class:`TimeoutError` covers either. This is
    the one capture seam ``ssh_run`` funnels through (and the point tests patch
    to fake remote output).
    """
    if _WINDOWS:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = _communicate_select(proc, argv=argv, timeout=timeout)
    assert proc.returncode is not None  # set by _communicate_select
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def ssh_run(
    cmd: str,
    *,
    ssh_target: str,
    capture: bool = True,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* on a remote host via SSH.

    Parameters
    ----------
    cmd:
        Shell command string to execute remotely.
    ssh_target:
        ssh destination — either ``user@host`` or an OpenSSH alias.
    capture:
        If True (default), capture stdout/stderr and return them.
        If False, inherit the parent process's stdout/stderr (useful for
        streaming long-running output).
    timeout:
        Per-call subprocess timeout in seconds.  When omitted, the module
        default :data:`SSH_TIMEOUT_SEC` is applied.  Pass ``timeout=None``
        explicitly to disable timeout enforcement (e.g. for legitimately
        long-running streaming commands); the bare ``None`` is propagated
        through to ``subprocess.run`` as ``timeout=None``.  The timeout
        is applied regardless of *capture* — the two parameters are
        orthogonal.

    Returns
    -------
    subprocess.CompletedProcess with returncode, stdout, stderr.

    Raises
    ------
    TimeoutError
        If the underlying ``subprocess.run`` exceeds the timeout.
    """
    effective_timeout: float | None = SSH_TIMEOUT_SEC if timeout is _DEFAULT else timeout

    def _attempt() -> subprocess.CompletedProcess[str]:
        # Rebuild argv each attempt: the named-pipe-failure retry path picks up
        # the updated _ssh_multiplex_opts() after mark_named_pipe_broken().
        # BatchMode=yes refuses password/keyboard-interactive prompts so an
        # unknown host or missing key surfaces as an immediate auth failure
        # rather than blocking until the timeout. _tar_ssh_push and
        # _scp_pull already use this flag.
        argv = [*ssh_argv("ssh"), ssh_target, cmd]
        try:
            if capture:
                # POSIX: close-pipes-on-exit reader so a remote command that
                # backgrounds a child holding the stdout/stderr pipe returns
                # the instant the foreground process exits instead of stalling
                # until ``effective_timeout`` (#209). Windows falls back to
                # subprocess.run inside the seam.
                return _capture_via_select(argv, timeout=effective_timeout)
            # Streaming mode inherits the parent's stdout/stderr — there are no
            # pipes for us to manage, so the original blocking call is correct.
            return subprocess.run(
                argv,
                capture_output=False,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ssh to {ssh_target} timed out after {effective_timeout}s: {_truncate(cmd)}"
            ) from exc

    def _run() -> subprocess.CompletedProcess[str]:
        # Auto-fallback on the syscall-layer named-pipe ControlMaster failure
        # mode (Windows OpenSSH version probe can't catch it; 2026-06-04). The
        # retry rebuilds argv inside _attempt to pick up the legacy override
        # after mark_named_pipe_broken(). No-op for streaming mode (proc.stderr
        # is None when capture_output=False).
        return run_with_named_pipe_retry(_attempt)

    return _with_ssh_backoff(_run, label=f"ssh {ssh_target}")
