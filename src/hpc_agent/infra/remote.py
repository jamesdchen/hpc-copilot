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

import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from hpc_agent.infra.ssh_validation import (
    _DISALLOWED_REMOTE_PATH_CHARS,  # noqa: F401 — re-export for backwards compat
    _DISALLOWED_TARGET_CHARS,  # noqa: F401 — re-export for backwards compat
    parse_remote_json,
    validate_remote_path,
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

# Default ControlPersist window. Tunable via ``HPC_SSH_PERSIST_INTERVAL``;
# see :func:`_ssh_multiplex_opts` for the accepted shapes.
_DEFAULT_SSH_PERSIST_INTERVAL = "10m"

# Characters that must never appear in the persist-interval env var. OpenSSH
# accepts plain ints (seconds), suffixed durations (``30m``, ``2h``), ``0``
# (persist until master exits), and ``no``/``yes``; none of those need any
# of these chars, so any occurrence indicates a typo or an injection attempt.
_DISALLOWED_PERSIST_CHARS = " \t\n\r;|&`$<>\"'\\*?!()=/"


def _ssh_multiplex_opts() -> list[str]:
    """Return SSH options that enable connection multiplexing.

    First call to a host opens the master socket; subsequent calls within
    the ControlPersist window reuse it. For an agent polling ``status``
    every 30s during a 4-hour job, this is the difference between hundreds
    of full handshakes and a single one.

    Env vars
    --------
    ``HPC_NO_SSH_MULTIPLEX=1``
        Opt out of multiplexing entirely (some clusters disallow
        multiplexed sessions, e.g. due to PAM-based session limits).
        Multiplexing is also auto-disabled on Windows because the
        ``ControlPath`` Unix socket isn't supported by native Windows
        OpenSSH (``ssh.exe`` aborts with ``getsockname failed: Not a
        socket``).
    ``HPC_SSH_PERSIST_INTERVAL``
        Override the ControlPersist window. The value is passed verbatim
        to OpenSSH, so any shape ``ssh_config(5)`` accepts works:

        * plain integer seconds (e.g. ``600``)
        * suffixed durations (e.g. ``30m``, ``2h``, ``1h30m``)
        * ``0`` — persist until the master exits
        * ``no`` / ``yes`` — disable persist (master exits with last
          session) / persist forever

        Defaults to ``10m`` for backwards compatibility. The value is
        validated loosely: whitespace and shell metacharacters are
        rejected; on rejection we log to stderr (no raise) and fall back
        to the default so a typo cannot break every cluster call.

        When the value is ``no``, no ``ControlPersist`` option is emitted —
        OpenSSH's default behaviour applies (master exits with the last
        client session).
    """
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1":
        return []
    if sys.platform == "win32":
        return []
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    opts = [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={runtime_dir}/hpc-cm-%C",
    ]
    persist = _resolve_ssh_persist_interval()
    if persist is not None:
        opts += ["-o", f"ControlPersist={persist}"]
    return opts


def _resolve_ssh_persist_interval() -> str | None:
    """Return the ControlPersist value to emit, or ``None`` to omit the opt.

    Reads ``HPC_SSH_PERSIST_INTERVAL`` and applies the validation rule
    documented on :func:`_ssh_multiplex_opts`. Returns ``None`` when the
    operator explicitly disabled persist (``no``); otherwise returns the
    string to embed after ``ControlPersist=``. Invalid values fall back
    to :data:`_DEFAULT_SSH_PERSIST_INTERVAL` after a stderr warning.
    """
    raw = os.environ.get("HPC_SSH_PERSIST_INTERVAL")
    if raw is None or raw == "":
        return _DEFAULT_SSH_PERSIST_INTERVAL
    bad = sorted({c for c in _DISALLOWED_PERSIST_CHARS if c in raw})
    if bad:
        # Don't raise — a malformed env var in someone's shell rc should
        # not break every ssh call. Warn loudly and fall back to default.
        import sys

        print(
            f"hpc-agent: ignoring HPC_SSH_PERSIST_INTERVAL={raw!r} "
            f"(disallowed characters {bad!r}); using default "
            f"{_DEFAULT_SSH_PERSIST_INTERVAL!r}",
            file=sys.stderr,
        )
        return _DEFAULT_SSH_PERSIST_INTERVAL
    if raw.lower() == "no":
        return None
    return raw


# Sentinel marker meaning "caller did not specify a timeout".  We need a
# distinct value (not ``None``) because ``timeout=None`` is the documented
# escape hatch for disabling enforcement entirely (e.g. legitimately
# long-running streaming SSH commands).  ``object()`` gives us a unique
# identity that no caller can accidentally collide with.
_DEFAULT: Final[Any] = object()

DEFAULT_RSYNC_EXCLUDES: list[str] = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".claude/",
    "hpc_agent/",  # protect deployed runtime stubs from --delete
    # Protect framework files scp'd into the cluster-side .hpc/ from the
    # local rsync's --delete pass.  The local .hpc/ contains only
    # tasks.py + runs/<id>.json; the cluster also holds _hpc_dispatch.py,
    # _hpc_combiner.py, and templates/ placed there by deploy_runtime.
    ".hpc/_hpc_dispatch.py",
    ".hpc/_hpc_combiner.py",
    ".hpc/templates/",
]


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


def _have_rsync() -> bool:
    """Return True if an ``rsync`` binary is on PATH.

    Detection at runtime via :func:`shutil.which`. Activates the scp/tar
    fallback when False (typically Windows hosts without WSL/MSYS rsync).
    """
    return shutil.which("rsync") is not None


def _remote_clean_cmd(remote_path: str, exclude: list[str]) -> str:
    """Build the remote shell command that deletes everything under
    *remote_path* except paths the *exclude* set protects.

    Gives the tar fallback rsync's ``--delete --exclude=...`` semantics:
    anything in the remote tree not protected by an exclude is removed
    before the fresh ``tar x`` extract, so a re-push cannot leave stale
    files behind. Anchoring mirrors rsync — a pattern containing an
    internal ``/`` is anchored to *remote_path* (``find -path``); a bare
    name matches at any depth (``find -name``).

    Safety: ``find -mindepth 1`` guarantees *remote_path* itself is
    never removed, and ``xargs -r`` skips ``rm`` entirely when nothing
    matched (a fresh remote dir). The caller (:func:`rsync_push`) has
    already run :func:`validate_remote_path`, so *remote_path* carries
    no shell metacharacters; every interpolated value is still
    ``shlex.quote``-d for defence in depth.
    """
    quoted_remote = shlex.quote(remote_path)
    root = remote_path.rstrip("/")
    prune_terms: list[str] = []
    for raw in exclude:
        pattern = raw.rstrip("/")
        if not pattern:
            continue
        if "/" in pattern:
            prune_terms.append(f"-path {shlex.quote(f'{root}/{pattern}')}")
        else:
            prune_terms.append(f"-name {shlex.quote(pattern)}")
    find_cmd = f"find {quoted_remote} -mindepth 1"
    if prune_terms:
        find_cmd += " \\( " + " -o ".join(prune_terms) + " \\) -prune -o"
    # -print0 / xargs -0 keep paths with spaces intact; -r skips rm on
    # empty input; -- stops rm treating a dash-led name as a flag. The
    # pipeline's exit status is rm's, which is 0 even if find races a
    # just-deleted subtree (rm -f ignores missing operands).
    return f"{find_cmd} -print0 | xargs -0 -r rm -rf --"


def _tar_ssh_push(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: str | Path,
    exclude: list[str],
    delete: bool = False,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Push *local_path* to *remote_path* via ``tar c | ssh tar x``.

    Used as the rsync_push fallback when rsync is absent. Respects the
    same *exclude* patterns as rsync (passed through to ``tar
    --exclude``). Returns a CompletedProcess so callers can inspect the
    same fields (returncode, stderr) they would for rsync.

    Implementation: spawn ``tar c`` and ``ssh tar x`` as two Popens
    connected by a pipe; both must exit zero for success.

    ``delete=True`` mirrors rsync's ``--delete``: a remote pre-clean
    step (see :func:`_remote_clean_cmd`) removes everything under
    *remote_path* that the *exclude* set does not protect, before the
    fresh ``tar x`` extract — so stale files cannot survive a re-push.
    The pre-clean and the extract run in a single ssh invocation.
    """
    src_dir = str(local_path).rstrip("/\\")

    # tar excludes mirror rsync's pattern shape (relative paths under src).
    tar_excludes: list[str] = []
    for pattern in exclude:
        tar_excludes += [f"--exclude={pattern.rstrip('/')}"]

    tar_cmd = ["tar", "c", *tar_excludes, "-C", src_dir, "."]
    # mkdir -> [pre-clean] -> extract, in one ssh invocation. ``tar x``
    # consumes the archive piped into ssh's stdin; the optional
    # pre-clean (delete=True) runs first and gives the fallback the
    # same --delete semantics rsync would apply.
    quoted_remote = shlex.quote(remote_path)
    remote_steps = [f"mkdir -p {quoted_remote}"]
    if delete:
        remote_steps.append(_remote_clean_cmd(remote_path, exclude))
    remote_steps.append(f"tar x -C {quoted_remote}")
    ssh_remote_cmd = " && ".join(remote_steps)
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", ssh_target, ssh_remote_cmd]

    # tar's stderr goes to a temp file rather than a PIPE: it is only
    # read after ``ssh`` exits, and a PIPE that fills its ~64 KB kernel
    # buffer (e.g. many "file changed as we read it" warnings on a
    # large tree) would block ``tar`` and deadlock the whole push.
    tar_stderr_file = tempfile.TemporaryFile()  # noqa: SIM115 - closed in finally below
    tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=tar_stderr_file)
    try:
        assert tar_proc.stdout is not None
        ssh_proc = subprocess.run(
            ssh_cmd,
            stdin=tar_proc.stdout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
        tar_proc.stdout.close()
        tar_proc.wait(timeout=timeout)
        tar_stderr_file.seek(0)
        tar_stderr_bytes = tar_stderr_file.read()
    except subprocess.TimeoutExpired as exc:
        tar_proc.kill()
        # Reap the killed child and close its stdout pipe — otherwise the
        # pipe FD and the zombie process leak on this timeout path (the
        # happy path closes/waits, this one did not).
        if tar_proc.stdout is not None:
            with contextlib.suppress(OSError):
                tar_proc.stdout.close()
        with contextlib.suppress(Exception):
            tar_proc.wait(timeout=5)
        raise TimeoutError(
            f"tar/ssh push to {ssh_target} timed out after {timeout}s: "
            f"{_truncate(f'{src_dir} -> {ssh_target}:{remote_path}')}"
        ) from exc
    finally:
        tar_stderr_file.close()

    tar_stderr = tar_stderr_bytes.decode(errors="replace")
    combined_stderr = "\n".join(filter(None, [tar_stderr.strip(), ssh_proc.stderr.strip()]))
    rc = ssh_proc.returncode if ssh_proc.returncode != 0 else tar_proc.returncode

    return subprocess.CompletedProcess(
        args=tar_cmd + ["|"] + ssh_cmd,
        returncode=rc,
        stdout=ssh_proc.stdout,
        stderr=combined_stderr,
    )


def _scp_pull(
    *,
    ssh_target: str,
    remote_path: str,
    remote_subdir: str,
    local_dir: str | Path,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Pull *remote_subdir* to *local_dir* via ``scp -r``.

    Used as the rsync_pull fallback when rsync is absent. The *include*
    filter is not honored (scp has no equivalent); callers passing a
    restrictive include will receive the entire subdirectory. For the
    payloads hpc-agent actually pulls (``_combiner/wave_*.json`` and
    optional per-task summaries), this is acceptable.
    """
    src = f"{ssh_target}:{remote_path.rstrip('/')}/{remote_subdir.strip('/')}/"
    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    dst = str(dst_path)

    scp_cmd = ["scp", "-r", "-o", "BatchMode=yes", src, dst]
    try:
        return subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"scp pull from {ssh_target} timed out after {timeout}s: {_truncate(f'{src} -> {dst}')}"
        ) from exc


def rsync_push(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: str | Path,
    exclude: list[str] | None = None,
    delete: bool = True,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Sync a local directory to a remote host using rsync.

    On hosts where the ``rsync`` binary is not on PATH (typically
    Windows without WSL / MSYS rsync), automatically falls back to a
    ``tar c | ssh tar x`` pipeline. The fallback honors both *exclude*
    and *delete* — ``delete=True`` runs a remote pre-clean step before
    the tar extract so stale remote files do not survive a re-push.

    Parameters
    ----------
    ssh_target:
        ssh destination — either ``user@host`` or an OpenSSH alias.
    remote_path:
        Absolute path on the remote host (e.g. ``/u/home/user/project``).
    local_path:
        Local directory to push. Trailing slash is handled automatically.
    exclude:
        Rsync exclude patterns.  Defaults to :data:`DEFAULT_RSYNC_EXCLUDES`
        if *None*.
    delete:
        If True (default), pass ``--delete`` so removed local files are
        also removed on the remote. On the tar/ssh fallback this is
        emulated by a remote pre-clean step (see :func:`_tar_ssh_push`).
    timeout:
        Per-call subprocess timeout in seconds.  When omitted, the module
        default :data:`RSYNC_TIMEOUT_SEC` is applied.  Pass ``timeout=None``
        explicitly to disable timeout enforcement; the bare ``None`` is
        propagated through to ``subprocess.run``.

    Raises
    ------
    TimeoutError
        If the underlying ``subprocess.run`` exceeds the timeout.
    """
    if exclude is None:
        exclude = DEFAULT_RSYNC_EXCLUDES
    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout

    # Validate the remote path up front so push and pull share one
    # rule. After validation the value flows verbatim through the
    # remote shell that rsync invokes — same posture as the rest of
    # the module.
    validate_remote_path(remote_path.rstrip("/"))

    if not _have_rsync():
        return _tar_ssh_push(
            ssh_target=ssh_target,
            remote_path=remote_path,
            local_path=local_path,
            exclude=exclude,
            delete=delete,
            timeout=effective_timeout,
        )

    exclude_flags: list[str] = []
    for pattern in exclude:
        exclude_flags += ["--exclude", pattern]

    src = str(local_path).rstrip("/\\") + "/"
    dst = f"{ssh_target}:{remote_path.rstrip('/')}/"

    flags = ["rsync", "-az"]
    if delete:
        flags.append("--delete")

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*flags, *exclude_flags, src, dst],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync push to {ssh_target} timed out after {effective_timeout}s: "
                f"{_truncate(f'{src} -> {dst}')}"
            ) from exc

    return _with_ssh_backoff(_run, label=f"rsync push {ssh_target}")


def deploy_runtime(
    *,
    ssh_target: str,
    remote_path: str,
) -> subprocess.CompletedProcess[str]:
    """Deploy framework runtime files to the cluster.

    Two payloads:

    1. **Importable stubs** in ``{remote_path}/hpc_agent/models/mapreduce/``:
       ``metrics_io.py`` so user executors can do
       ``from hpc_agent.models.mapreduce.metrics_io import write_metrics`` on
       compute nodes without installing the full package.
    2. **Framework artifacts** in ``{remote_path}/.hpc/``: the framework
       executor (``_hpc_dispatch.py``), the combiner
       (``_hpc_combiner.py``), and the four job templates under
       ``templates/``. The cluster-side ``.hpc/`` mirrors the experiment's
       local ``.hpc/`` directory layout — ``tasks.py`` and
       ``runs/<id>.json`` come over via :func:`rsync_push`; the framework
       files are placed here by scp.

    Each underlying ssh/scp invocation is bounded by
    :data:`SSH_TIMEOUT_SEC`; if any exceeds it, :class:`TimeoutError` is
    raised that names the target and the basename of the file being copied.

    Must be called **after** :func:`rsync_push` (which uses ``--delete``).
    The default rsync excludes preserve cluster-side framework files
    inside ``.hpc/``, but deploy_runtime is still safe to re-run after
    every push (it overwrites with the package-versioned bytes).
    """
    remote_path_q = shlex.quote(remote_path)
    pkg_dir = Path(__file__).parent.parent

    ssh_run(
        f"mkdir -p {remote_path_q}/hpc_agent/models/mapreduce"
        f" {remote_path_q}/.hpc/templates"
        f" {remote_path_q}/.hpc/templates/common"
        f" && touch {remote_path_q}/hpc_agent/__init__.py"
        f" && touch {remote_path_q}/hpc_agent/models/mapreduce/__init__.py",
        ssh_target=ssh_target,
    )

    def _scp(src: Path, dst_rel: str) -> subprocess.CompletedProcess[str]:
        dst = f"{ssh_target}:{shlex.quote(remote_path)}/{dst_rel}"

        def _run() -> subprocess.CompletedProcess[str]:
            try:
                return subprocess.run(
                    # ``-o BatchMode=yes`` fails fast on missing
                    # credentials instead of blocking on a password
                    # prompt — matches ``_scp_pull`` and ``ssh_run``.
                    ["scp", "-o", "BatchMode=yes", str(src), dst],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=SSH_TIMEOUT_SEC,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"scp to {ssh_target} timed out after {SSH_TIMEOUT_SEC}s: {src.name}"
                ) from exc
            except FileNotFoundError as exc:
                # scp binary missing on the local host. Surface as
                # FileNotFoundError so callers can distinguish "no scp on
                # PATH" from a remote authentication failure.
                raise FileNotFoundError(
                    f"scp binary not found while copying {src.name}: {exc}"
                ) from exc

        return _with_ssh_backoff(_run, label=f"scp {src.name}")

    # Importable stubs (used inside cluster jobs by user code).
    #
    # Cluster-side imports we have to support:
    #   - ``from hpc_agent.models.mapreduce.metrics_io import write_metrics``
    #     in user executor scripts (executor_template.py).
    #   - ``from hpc_agent.executor_cli import flag, generic_args, gpu_args``
    #     in user .hpc/tasks.py (tasks_example.py). The dispatcher loads
    #     tasks.py at task time via importlib; the top-level import has
    #     to resolve or every task ImportErrors before total()/resolve()
    #     are called.
    #
    # Both modules are stdlib-only (verified via AST scan) so they ship
    # safely without dragging in the rest of the package.
    _scp(
        pkg_dir / "models" / "mapreduce" / "metrics_io.py",
        "hpc_agent/models/mapreduce/metrics_io.py",
    )
    _scp(pkg_dir / "executor_cli.py", "hpc_agent/executor_cli.py")

    # Framework executor + combiner inside .hpc/.
    _scp(pkg_dir / "models" / "mapreduce" / "dispatch.py", ".hpc/_hpc_dispatch.py")

    # Job templates inside .hpc/templates/.
    # B5-PR2: drop the inline ``if sched == 'sge'`` ladder; the backend
    # registry owns the canonical extension via ``template_ext``. This
    # keeps remote.py and __init__.py:get_template_path in sync.
    from hpc_agent.infra.backends import template_ext_for

    for sched in ("sge", "slurm"):
        ext = template_ext_for(sched).lstrip(".")
        for kind in ("cpu_array", "gpu_array"):
            _scp(
                pkg_dir
                / "models"
                / "mapreduce"
                / "templates"
                / "runtime"
                / sched
                / f"{kind}.{ext}",
                f".hpc/templates/{kind}.{ext}",
            )

    # Shared preambles sourced by the templates above. Source layout is
    # ``templates/runtime/common/<name>.sh`` (per the templates split);
    # deploy destination is ``.hpc/templates/common/<name>.sh`` to match
    # the ``source "$REPO_DIR/.hpc/templates/common/<name>.sh"`` line in
    # each per-template body and the ``mkdir -p .hpc/templates/common``
    # above.
    for common_name in ("hpc_preamble.sh", "gpu_preamble.sh"):
        _scp(
            pkg_dir / "models" / "mapreduce" / "templates" / "runtime" / "common" / common_name,
            f".hpc/templates/common/{common_name}",
        )

    # Combiner is the last scp; return its CompletedProcess so callers
    # can inspect the trailing returncode.
    return _scp(pkg_dir / "models" / "mapreduce" / "combiner.py", ".hpc/_hpc_combiner.py")


def run_combiner(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Run the on-cluster combiner on the login node for a specific wave.

    Executes ``.hpc/_hpc_combiner.py`` on the remote host via SSH. The
    combiner accepts both CLI flags (preferred) and ``HPC_WAVE`` /
    ``HPC_RUN_ID`` env vars; we pass both.

    Parameters
    ----------
    ssh_target, remote_path:
        SSH target and remote project root.
    wave:
        Wave number (0-based) to combine.
    run_id:
        Run identifier — locates the per-run sidecar at
        ``.hpc/runs/<run_id>.json`` from which the combiner reads
        ``wave_map`` and ``result_dir_template``.
    force:
        If True, pass ``--force`` so the combiner overwrites any existing
        ``_combiner/wave_N.json`` output.
    timeout:
        Per-call subprocess timeout in seconds, threaded through to
        :func:`ssh_run`. Defaults to :data:`SSH_TIMEOUT_SEC` when omitted.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"HPC_WAVE={wave} HPC_RUN_ID={run_id_q} "
        f"python3 .hpc/_hpc_combiner.py --wave {wave} --run-id {run_id_q}{force_flag}"
    )
    if timeout is _DEFAULT:
        return ssh_run(cmd, ssh_target=ssh_target)
    return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)


def run_combiner_checked(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
) -> tuple[bool, str, str]:
    """Run the combiner and return ``(ok, stdout, stderr)``.

    Thin wrapper around :func:`run_combiner` that collapses
    ``CompletedProcess`` into a simple tuple. ``ok`` is ``True`` iff the
    remote combiner exited with returncode ``0``. A timeout propagates
    as :class:`TimeoutError`, not ``ok=False``.
    """
    if timeout is _DEFAULT:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
        )
    else:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            timeout=timeout,
        )
    return (
        result.returncode == 0,
        result.stdout or "",
        result.stderr or "",
    )


def rsync_pull(
    *,
    ssh_target: str,
    remote_path: str,
    remote_subdir: str,
    local_dir: str | Path,
    include: list[str] | None = None,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Pull files from a remote host to a local directory.

    When *include* is provided, only matching patterns are transferred
    (all others are excluded).  When *include* is ``None``, the entire
    ``remote_subdir`` is pulled without filtering.

    Parameters
    ----------
    ssh_target:
        ssh destination — either ``user@host`` or an OpenSSH alias.
    remote_path:
        Absolute path of the project root on the remote host.
    remote_subdir:
        Subdirectory under *remote_path* to pull (e.g. ``results/``).
    local_dir:
        Local destination directory.  Created if it does not exist.
    include:
        Optional list of rsync ``--include`` patterns.  When provided,
        ``--include='*/'`` is prepended automatically (to traverse
        directories) and a trailing ``--exclude='*'`` is appended.
    timeout:
        Per-call subprocess timeout in seconds.  When omitted, the module
        default :data:`RSYNC_TIMEOUT_SEC` is applied.  Pass ``timeout=None``
        explicitly to disable timeout enforcement; the bare ``None`` is
        propagated through to ``subprocess.run``.

    Raises
    ------
    TimeoutError
        If the underlying ``subprocess.run`` exceeds the timeout.
    """
    # ``validate_remote_path`` rejects whitespace + shell-metachars up
    # front so the value can flow verbatim through the remote shell that
    # rsync invokes. (The earlier ``shlex.quote`` form was inconsistent
    # with ``rsync_push`` and produced literal single quotes that some
    # rsync builds passed straight to the remote shell.)
    validate_remote_path(remote_path.rstrip("/"))
    if remote_subdir.strip("/"):
        validate_remote_path(remote_subdir.strip("/"))
    src = f"{ssh_target}:{remote_path.rstrip('/')}/{remote_subdir.strip('/')}/"

    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    dst = str(dst_path).rstrip("/\\") + "/"

    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout

    if not _have_rsync():
        return _scp_pull(
            ssh_target=ssh_target,
            remote_path=remote_path,
            remote_subdir=remote_subdir,
            local_dir=local_dir,
            timeout=effective_timeout,
        )

    filter_flags: list[str] = []
    if include is not None:
        filter_flags += ["--include=*/"]
        for pattern in include:
            filter_flags += [f"--include={pattern}"]
        filter_flags += ["--exclude=*"]

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["rsync", "-az", *filter_flags, src, dst],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync pull from {ssh_target} timed out after {effective_timeout}s: "
                f"{_truncate(f'{src} -> {dst}')}"
            ) from exc

    return _with_ssh_backoff(_run, label=f"rsync pull {ssh_target}")
