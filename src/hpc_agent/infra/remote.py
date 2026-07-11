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
    "REMOTE_DEADLINE_MARGIN_SEC",
    "REMOTE_DEADLINE_DEFAULT_SEC",
    "OP_MARKER_PREFIX",
    "build_remote_command",
    "remote_op",
    "current_remote_op",
    "ssh_run",
]

import contextlib
import contextvars
import os
import re
import select
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Final

from hpc_agent.infra import ssh_circuit
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


# ---------------------------------------------------------------------------
# Server-side self-destruct + self-identification (run-12 finding 20)
# ---------------------------------------------------------------------------
#
# The connection storm's REMOTE cost: an ssh command the client killed leaves
# an orphaned remote half (a bash+python pair per poll). Enough of them exhaust
# the login node's per-user process quota until sshd itself cannot fork a shell
# — a state self-service recovery cannot clear (the ``kill`` builtin needs a
# shell to run IN). Two structural defences, both applied at the ONE
# ssh-command-construction seam (:func:`ssh_run`), so every framework remote
# command inherits them regardless of the call site:
#
#   LAYER 1 — a server-side deadline. Wrap the command in ``timeout`` derived
#   from the SAME budget the client already enforces (plus a margin, so the
#   client timeout normally fires first and the remote wrapper is purely the
#   orphan bound). An orphaned remote half self-destructs by construction, no
#   matter how the client died.
#
#   LAYER 2 — a self-identifying marker. Prefix an inert ``HPC_AGENT_OP=<op>:
#   <epoch>`` token, landing it BOTH in the process environ (an env-assignment
#   prefix) AND — critically — in the process ARGV (passed as bash's ``$0``, so
#   it shows in ``ps -o args`` / matches ``pgrep -f``). The hygiene sweep
#   (:mod:`hpc_agent.ops.recover.stray_sweep`) can then count framework strays
#   and reap ONLY marked, over-age ones — never an unmarked user process.

#: Seconds added to the client timeout to derive the remote self-destruct bound.
#: The client timeout normally fires first; the remote ``timeout`` is the pure
#: orphan bound for the case where the client vanished (crash, kill -9, network
#: drop). Tunable via ``HPC_SSH_REMOTE_DEADLINE_MARGIN_SEC``.
REMOTE_DEADLINE_MARGIN_SEC: Final[int] = _env_int("HPC_SSH_REMOTE_DEADLINE_MARGIN_SEC", 60)

#: Remote self-destruct bound for a command the client runs with NO timeout
#: (``timeout=None`` — the streaming / long-running escape hatch). Never
#: unbounded: an orphan of even a long command must still eventually die.
#: Tunable via ``HPC_SSH_REMOTE_DEADLINE_DEFAULT_SEC`` (default 1h).
REMOTE_DEADLINE_DEFAULT_SEC: Final[int] = _env_int("HPC_SSH_REMOTE_DEADLINE_DEFAULT_SEC", 3600)

#: Grace (seconds) after the ``timeout`` SIGTERM before ``timeout`` escalates to
#: SIGKILL (``timeout -k``), so a child that traps/ignores TERM is still reaped.
_REMOTE_DEADLINE_KILL_GRACE_SEC: Final[int] = 10

#: Env escape hatch (documented on :func:`build_remote_command`): set to ``1`` to
#: emit the remote command completely UNWRAPPED — no ``timeout``, no marker — for
#: debugging a command whose behaviour the wrapper obscures.
_NO_REMOTE_DEADLINE_ENV: Final[str] = "HPC_SSH_NO_REMOTE_DEADLINE"

#: The marker key. ``pgrep -f`` / ``ps`` matching keys on this literal.
OP_MARKER_PREFIX: Final[str] = "HPC_AGENT_OP"

# The op label a wrapped command carries when no call site set one. A generic
# constant still makes the process framework-identifiable; the epoch keeps each
# marker distinct.
_DEFAULT_OP: Final[str] = "ssh"

# Ambient op label, set by the (optional) :func:`remote_op` context manager so a
# verb can tag the commands it issues without threading ``op=`` through every
# call. ``ssh_run``'s explicit ``op=`` argument still wins over this.
_CURRENT_OP: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "hpc_agent_remote_op", default=None
)


def current_remote_op() -> str | None:
    """The ambient remote-op label, or ``None`` when no :func:`remote_op` is active."""
    return _CURRENT_OP.get()


@contextlib.contextmanager
def remote_op(op: str) -> Iterator[None]:
    """Tag every :func:`ssh_run` issued in this context with *op* (LAYER-2 marker).

    Optional convenience so a verb (submit-s2, verify-canary, a poll loop) can
    label the marker its remote commands carry without passing ``op=`` down every
    layer. Nestable and contextvar-scoped (safe under threads / async). An
    explicit ``op=`` on :func:`ssh_run` overrides the ambient value.
    """
    token = _CURRENT_OP.set(op)
    try:
        yield
    finally:
        _CURRENT_OP.reset(token)


def _remote_deadline_seconds(timeout: float | None) -> int:
    """The server-side ``timeout`` bound (whole seconds) for a client *timeout*.

    ``None`` (client enforces no timeout) → :data:`REMOTE_DEADLINE_DEFAULT_SEC`
    (never unbounded). Otherwise the client budget + :data:`REMOTE_DEADLINE_MARGIN_SEC`
    so the client's own timeout normally fires first and the remote wrapper is
    only the orphan bound. Floored at 1s so a sub-second budget still wraps.
    """
    if timeout is None:
        return REMOTE_DEADLINE_DEFAULT_SEC
    return max(1, int(timeout) + REMOTE_DEADLINE_MARGIN_SEC)


def _op_marker(op: str | None) -> str:
    """The inert ``HPC_AGENT_OP=<op>:<epoch>`` token for LAYER-2 identification.

    *op* is sanitised to ``[A-Za-z0-9._-]`` (argv/pgrep-safe, no shell
    metacharacters) so the token can ride argv unquoted and match ``pgrep -f``.
    """
    token = op or current_remote_op() or _DEFAULT_OP
    token = re.sub(r"[^A-Za-z0-9._-]", "_", token) or _DEFAULT_OP
    return f"{OP_MARKER_PREFIX}={token}:{int(time.time())}"


def build_remote_command(cmd: str, *, timeout: float | None, op: str | None = None) -> str:
    """Wrap *cmd* for server-side self-destruct (LAYER 1) + self-identification (LAYER 2).

    Returns the string handed to ``ssh`` as the remote command. The shape is::

        HPC_AGENT_OP=<op>:<epoch> timeout -k <grace> <deadline>s bash -c '<cmd>' <same-marker>

    * ``timeout`` bounds the remote command's lifetime at
      :func:`_remote_deadline_seconds` (client budget + margin, or the generous
      default when the client set none), so an orphaned remote half — the run-12
      finding-20 login-node fork-exhaustion class — self-destructs on its own.
      ``bash -c '<cmd>'`` preserves *cmd*'s exact shell semantics (it is
      ``shlex.quote``-d, so a compound ``a && b | c`` command survives byte-for-
      byte); ``timeout`` exits with *cmd*'s own status normally, and ``124`` only
      when it fires — callers classify ``124`` as transient, never as a broken-env
      (126/127) or success signal.
    * The ``HPC_AGENT_OP`` marker rides both the process environ (the leading
      env-assignment) and the process argv (bash's ``$0``, the trailing token —
      what makes it visible to ``ps -o args`` / ``pgrep -f`` for the hygiene
      sweep). It is inert: the wrapped script never references ``$0``.

    Escape hatch: with ``HPC_SSH_NO_REMOTE_DEADLINE=1`` in the environment, *cmd*
    is returned completely unwrapped (no ``timeout``, no marker) — for debugging a
    command whose behaviour the wrapper obscures. Layers 1 and 2 are then off.
    """
    if os.environ.get(_NO_REMOTE_DEADLINE_ENV) == "1":
        return cmd
    marker = _op_marker(op)
    deadline = _remote_deadline_seconds(timeout)
    return (
        f"{marker} timeout -k {_REMOTE_DEADLINE_KILL_GRACE_SEC} {deadline}s "
        f"bash -c {shlex.quote(cmd)} {marker}"
    )


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
    ssh_target: str,
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

    *label* identifies the call site (e.g. ``"rsync push"``) and names it in
    the per-retry stderr diagnostic. Disable retries entirely by setting
    ``HPC_SSH_NO_BACKOFF=1`` (useful in tests that mock subprocess.run).

    *ssh_target* is required (keyword-only, no default) so the breaker
    cannot be silently bypassed by a future call site that forgets it:
    every attempt — including each ladder retry, and the
    ``HPC_SSH_NO_BACKOFF`` single shot — runs under the persistent
    per-host circuit breaker (:mod:`hpc_agent.infra.ssh_circuit`): the
    breaker is consulted BEFORE the attempt and the outcome is recorded
    after. Consequence for the ladder: three consecutive connection-level
    failures open the circuit mid-schedule, and the next rung raises
    :class:`hpc_agent.errors.SshCircuitOpen` (not retryable) instead of
    opening yet another connection — the fleet-level ban-hammer guard the
    2026-07-04 all-night probe storm showed was missing. Non-connection
    failures (auth refused, remote command non-zero) neither count nor trip.

    Each retry is announced on stderr (``<label>: attempt N failed (...);
    retrying in Xs``) — the ladder used to spin silently, which turned a
    stalled ssh-agent named pipe into an unexplained multi-minute hang with
    no breadcrumb in the driver log (2026-07-04 pre-flight wedge).
    """

    def guarded() -> subprocess.CompletedProcess[str]:
        return ssh_circuit.guarded_call(ssh_target, fn)

    if os.environ.get("HPC_SSH_NO_BACKOFF") == "1":
        return guarded()

    def _attempt() -> subprocess.CompletedProcess[str]:
        # TimeoutError raised by fn propagates to run_with_retry as a
        # retryable exception; a throttle-marked cp is re-raised as the
        # _ThrottleRetry signal so the same runner retries it.
        # SshCircuitOpen raised by the breaker gate is NOT in retry_on and
        # propagates immediately — an open circuit ends the ladder.
        cp = guarded()
        if _is_throttle_failure(cp):
            raise _ThrottleRetry(cp)
        return cp

    def _log_retry(attempt: int, exc: BaseException, delay: float) -> None:
        if isinstance(exc, _ThrottleRetry):
            detail = (exc.cp.stderr or "").strip() or f"exit {exc.cp.returncode}"
        else:
            detail = str(exc)
        print(
            f"{label}: attempt {attempt} failed ({_truncate(detail, 200)}); retrying in {delay:g}s",
            file=sys.stderr,
            flush=True,
        )

    try:
        return run_with_retry(_attempt, policy=_ssh_backoff_policy(), on_retry=_log_retry)
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


# Bound on the post-kill output drain in :func:`_capture_windows`. After the
# timed-out child is killed we give its reader threads a few seconds to hit
# EOF and hand over whatever output was buffered; if a grandchild still holds
# the pipe handles we abandon the (daemon) reader threads rather than block.
_POST_KILL_DRAIN_SEC: Final[float] = 5.0


def _capture_windows(
    argv: list[str],
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Blocking capture with a timeout that can *actually fire* (S2 wedge fix).

    ``subprocess.run(..., timeout=...)`` is NOT a hard deadline on Windows: on
    ``TimeoutExpired`` it kills the child and then calls an **unbounded**
    ``communicate()`` to collect the reader threads' output. When a grandchild
    inherited the stdout/stderr handles (ssh ControlMaster mux, agent relay),
    EOF never arrives and that post-kill ``communicate()`` blocks forever —
    the 2026-07-04 submit-flow pre-flight wedge (driver parked for hours in
    ``subprocess.run`` despite ``timeout=60``). Faulthandler pinned the stall
    to exactly this frame.

    This helper reimplements run-with-timeout with every wait bounded: kill on
    deadline, then drain for at most :data:`_POST_KILL_DRAIN_SEC` before
    abandoning the reader threads (they are daemon threads; leaking them
    cannot block interpreter exit).
    """
    proc = subprocess.Popen(  # noqa: S603 - argv built by ssh_argv, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        # Bounded second drain — the whole point. Collect what the reader
        # threads already buffered, but never wait for an EOF a grandchild
        # may withhold indefinitely.
        try:
            out, err = proc.communicate(timeout=_POST_KILL_DRAIN_SEC)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        raise subprocess.TimeoutExpired(argv, timeout or 0.0, output=out, stderr=err) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _capture_via_select(
    argv: list[str],
    *,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* capturing stdout/stderr, returning a ``CompletedProcess``.

    POSIX uses the close-pipes-on-exit :func:`_communicate_select` reader so a
    backgrounded remote child can't wedge the read; Windows falls back to
    :func:`_capture_windows`, a blocking capture whose timeout is a hard
    deadline (``subprocess.run``'s post-kill ``communicate()`` is unbounded
    and wedged the submit pre-flight probe for hours — see that helper's
    docstring). Both surfaces raise :class:`subprocess.TimeoutExpired` on
    *timeout*, so the caller's single translation to :class:`TimeoutError`
    covers either. This is the one capture seam ``ssh_run`` funnels through
    (and the point tests patch to fake remote output).
    """
    if _WINDOWS:
        return _capture_windows(argv, timeout=timeout)
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
    op: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* on a remote host via SSH.

    Every remote command is wrapped by :func:`build_remote_command` before it is
    handed to ``ssh`` — a server-side ``timeout`` self-destruct bound (LAYER 1)
    plus an inert ``HPC_AGENT_OP`` marker (LAYER 2) so an orphaned remote half
    cannot outlive its budget and the hygiene sweep can identify framework strays
    (run-12 finding 20). Set ``HPC_SSH_NO_REMOTE_DEADLINE=1`` to disable both for
    debugging.

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
    op:
        Optional LAYER-2 marker label (the ``<op>`` in ``HPC_AGENT_OP=<op>:
        <epoch>``). Defaults to the ambient :func:`remote_op` value, else
        ``"ssh"``. Sanitised to argv-safe characters.
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

    # LAYER 1 + LAYER 2 (run-12 finding 20): wrap the remote command with a
    # server-side self-destruct deadline (derived from the client budget the
    # engine and one-shot paths both enforce) + the self-identifying marker. Done
    # ONCE here so both the asyncssh engine and the one-shot path ship the wrapped
    # form; the original ``cmd`` is kept for the human-readable timeout message.
    remote_cmd = build_remote_command(cmd, timeout=effective_timeout, op=op)

    # Command-channel outsourcing fast path (opt-in, HPC_SSH_ENGINE=asyncssh):
    # run the command over a held asyncssh connection (library channel, typed
    # errors) instead of a fresh cold handshake per call. Capture-mode only
    # (streaming inherits the parent's fds, which the channel can't frame). ANY
    # engine trouble raises EngineUnavailable → fall straight through to the
    # one-shot path below, the permanent hard fallback. The engine is never
    # load-bearing (engine → one-shot), so an opt-in engine can never be worse
    # than today. (The deprecated phase-1 in-process broker that once sat
    # between the engine and the one-shot path was retired + deleted 2026-07-07
    # per docs/design/connection-broker.md; the one-shot path is now the sole
    # fallback.)
    if capture:
        from hpc_agent.infra import ssh_engine

        if ssh_engine.engine_enabled():
            try:
                return ssh_engine.engine_ssh_run(
                    remote_cmd, ssh_target=ssh_target, timeout=effective_timeout
                )
            except ssh_engine.EngineUnavailable:
                pass  # fall through to the one-shot path below

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
        argv = [*ssh_argv("ssh"), ssh_target, remote_cmd]
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

    return _with_ssh_backoff(_run, label=f"ssh {ssh_target}", ssh_target=ssh_target)
