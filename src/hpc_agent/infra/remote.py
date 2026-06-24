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

from hpc_agent.infra.retry import RetryPolicy, run_with_retry
from hpc_agent.infra.ssh_options import run_with_named_pipe_retry, ssh_argv
from hpc_agent.infra.ssh_throttle import throttle_connection

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
#
# This tuple is a clean geometric doubling, so it is re-expressed as a
# :class:`~hpc_agent.infra.retry.RetryPolicy` by :func:`_ssh_backoff_policy`
# (base = first delay × ``backoff_factor=2``) rather than consumed element by
# element — the single retry surface #308 consolidates onto. It stays the
# documented source of truth for the schedule, and ``test_remote`` asserts the
# derived policy reproduces it exactly so a future edit that broke the doubling
# would fail loudly instead of silently changing which delays are slept.
_BACKOFF_DELAYS_SEC: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)


class _ThrottleRetry(Exception):  # noqa: N818 - a retry *signal*, not an error
    """Internal signal wrapping a throttle-marked CompletedProcess for retry.

    :func:`run_with_retry` drives backoff by retrying on *exceptions*, but a
    throttle failure is a returned ``CompletedProcess``, not a raised error —
    and the historical contract is that a throttle failure surviving every
    retry is **returned** (the caller inspects the cp), never raised. Wrapping
    the cp in this signal lets the shared runner apply the schedule while
    :func:`_with_ssh_backoff` unwraps it on exhaustion to return the cp.
    """

    def __init__(self, cp: subprocess.CompletedProcess[str]) -> None:
        super().__init__()
        self.cp = cp


def _ssh_backoff_policy() -> RetryPolicy:
    """The :data:`_BACKOFF_DELAYS_SEC` schedule as a :class:`RetryPolicy`.

    Built from the module-level tuple at call time so tests that pin the
    schedule (``monkeypatch.setattr(... _BACKOFF_DELAYS_SEC, (0.0,) * 4)``)
    still drive both the attempt count and the per-attempt delay. ``1 +
    len(...)`` attempts = one initial try plus one retry per scheduled delay,
    matching the previous ``enumerate((0.0, *_BACKOFF_DELAYS_SEC))`` loop.
    """
    base = _BACKOFF_DELAYS_SEC[0] if _BACKOFF_DELAYS_SEC else 0.0
    return RetryPolicy(
        max_attempts=1 + len(_BACKOFF_DELAYS_SEC),
        base_delay_sec=base,
        backoff_factor=2.0,
        retry_on=(TimeoutError, _ThrottleRetry),
    )


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

    The backoff itself is delegated to the shared :func:`run_with_retry`
    surface (#308) via :func:`_ssh_backoff_policy`; this wrapper only adapts
    the two retry triggers to it — ``TimeoutError`` propagates naturally, and a
    throttle-marked cp is carried through retries by :class:`_ThrottleRetry`
    and unwrapped on exhaustion so the cp is returned rather than raised.

    *label* identifies the call site (e.g. ``"rsync push"``) and is retained
    for caller-side diagnostics. Disable retries entirely by setting
    ``HPC_SSH_NO_BACKOFF=1`` (useful in tests that mock subprocess.run).
    """
    if os.environ.get("HPC_SSH_NO_BACKOFF") == "1":
        return fn()

    def _attempt() -> subprocess.CompletedProcess[str]:
        # TimeoutError raised by fn propagates to run_with_retry as a
        # retryable exception; a throttle-marked cp is re-raised as the
        # _ThrottleRetry signal so the same runner retries it.
        cp = fn()
        if _is_throttle_failure(cp):
            raise _ThrottleRetry(cp)
        return cp

    try:
        return run_with_retry(_attempt, policy=_ssh_backoff_policy())
    except _ThrottleRetry as exhausted:
        # Every retry consumed and still throttled: return the failing cp for
        # the caller to inspect (an exhausted TimeoutError, by contrast,
        # propagates out of run_with_retry unchanged).
        return exhausted.cp


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

    # Cap the connection-open *rate* to this host (ban-driver guard): a burst of
    # back-to-back ssh calls (retry storms, parallel probes) is what trips a
    # cluster's fail2ban / rate-limiter, and neither ConnectTimeout (duration)
    # nor IdentitiesOnly (auth attempts) bounds frequency. No-op unless
    # HPC_SSH_SAFE_INTERVAL is set (>0). Runs once per ssh_run; retries are
    # already spaced by _with_ssh_backoff. See infra.ssh_throttle.
    throttle_connection(ssh_target)

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
