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
    "validate_ssh_target",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
    "run_combiner",
    "run_combiner_checked",
    "parse_remote_json",
]

import os
import shutil  # noqa: F401 — kept so tests that patch remote.shutil.which still steer transport._have_rsync
import subprocess
import sys  # noqa: F401 — kept so tests that monkeypatch remote.sys.platform still steer ssh_options
import time
from typing import TYPE_CHECKING, Any, Final

from hpc_agent.infra.ssh_options import (
    _DEFAULT_SSH_PERSIST_INTERVAL,  # noqa: F401 — re-export for backwards compat
    _DISALLOWED_PERSIST_CHARS,  # noqa: F401 — re-export for backwards compat
    _resolve_ssh_persist_interval,  # noqa: F401 — re-export for backwards compat
    _ssh_multiplex_opts,
)
from hpc_agent.infra.ssh_validation import (
    _DISALLOWED_REMOTE_PATH_CHARS,  # noqa: F401 — re-export for backwards compat
    _DISALLOWED_TARGET_CHARS,  # noqa: F401 — re-export for backwards compat
    parse_remote_json,
    validate_remote_path,  # noqa: F401 — re-export for backwards compat
    validate_ssh_target,
)

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
# ``parse_remote_json`` live in :mod:`hpc_agent.infra.ssh_validation` —
# they are pure, no-I/O helpers. The disallowed-char constants
# (``_DISALLOWED_TARGET_CHARS`` / ``_DISALLOWED_REMOTE_PATH_CHARS``)
# live there too; the names re-export via the top-level import block so
# existing call sites (``from hpc_agent.infra.remote import
# validate_ssh_target``) keep working unchanged.

# SSH option-building helpers (``_ssh_multiplex_opts``,
# ``_resolve_ssh_persist_interval``) plus the two constants they consult
# live in :mod:`hpc_agent.infra.ssh_options`. They re-import at the top
# of this module so existing test access via ``remote._ssh_multiplex_opts``
# keeps working.


# Sentinel marker meaning "caller did not specify a timeout".  We need a
# distinct value (not ``None``) because ``timeout=None`` is the documented
# escape hatch for disabling enforcement entirely (e.g. legitimately
# long-running streaming SSH commands).  ``object()`` gives us a unique
# identity that no caller can accidentally collide with.
_DEFAULT: Final[Any] = object()

# ``DEFAULT_RSYNC_EXCLUDES`` lives in :mod:`hpc_agent.infra.transport`
# alongside ``rsync_push`` (its sole consumer) and re-exports back via
# the deferred import below.


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
    # BatchMode=yes refuses password/keyboard-interactive prompts so an
    # unknown host or missing key surfaces as an immediate auth failure
    # rather than blocking until the timeout. _tar_ssh_push and
    # _scp_pull already use this flag.
    argv = ["ssh", "-o", "BatchMode=yes", *_ssh_multiplex_opts(), ssh_target, cmd]

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                capture_output=capture,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"ssh to {ssh_target} timed out after {effective_timeout}s: {_truncate(cmd)}"
            ) from exc

    return _with_ssh_backoff(_run, label=f"ssh {ssh_target}")


# Transport helpers (rsync push/pull, scp/tar fallbacks, deploy_runtime,
# run_combiner / run_combiner_checked, DEFAULT_RSYNC_EXCLUDES) live in
# :mod:`hpc_agent.infra.transport`. They re-import below — placed AFTER
# ssh_run / _with_ssh_backoff so transport.py can import remote without
# a circular dependency.
from hpc_agent.infra.transport import (  # noqa: E402 — placed below ssh_run on purpose
    DEFAULT_RSYNC_EXCLUDES,  # noqa: F401 — re-export for backwards compat
    _have_rsync,  # noqa: F401 — re-export for backwards compat (mock target)
    _remote_clean_cmd,  # noqa: F401 — re-export for backwards compat (tested directly)
    _scp_pull,  # noqa: F401 — re-export for backwards compat
    _tar_ssh_push,  # noqa: F401 — re-export for backwards compat
    deploy_runtime,  # noqa: F401 — re-export for backwards compat
    rsync_pull,  # noqa: F401 — re-export for backwards compat
    rsync_push,  # noqa: F401 — re-export for backwards compat
    run_combiner,  # noqa: F401 — re-export for backwards compat
    run_combiner_checked,  # noqa: F401 — re-export for backwards compat
)
