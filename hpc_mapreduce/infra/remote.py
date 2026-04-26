"""SSH and rsync utilities for remote HPC operations.

Provides thin wrappers around ssh/rsync so cluster commands can be
executed from a local machine without paramiko or other dependencies.

All functions require explicit ``host``, ``user``, and ``remote_path``
parameters - there are no hardcoded defaults.  Callers obtain these
values from ``clusters.yaml`` + ``hpc.yaml`` via :mod:`hpc_mapreduce.job.manifest`.

Every subprocess invocation in this module enforces a timeout so a flaky
cluster connection or paused rsync cannot block ``/submit``, ``/monitor``,
or ``/aggregate`` indefinitely.  The defaults are :data:`SSH_TIMEOUT_SEC`
for SSH/scp commands and :data:`RSYNC_TIMEOUT_SEC` for rsync transfers.
Callers may override per-call by passing ``timeout=`` (in seconds), or
disable enforcement entirely by passing ``timeout=None``.  When the
underlying child exceeds the timeout, the wrapper raises
:class:`TimeoutError` with a message that names the host and a snippet of
the command being run.
"""

from __future__ import annotations

__all__ = [
    "SSH_TIMEOUT_SEC",
    "RSYNC_TIMEOUT_SEC",
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
    "run_combiner",
    "run_combiner_checked",
]

import shlex
import subprocess
from pathlib import Path
from typing import Any, Final

# Default subprocess timeouts (in seconds).  ``ssh_run`` covers login-node
# commands, including the status-reporter SSH calls that exec python and may
# need a few seconds; 60s is a generous ceiling for those.  ``rsync`` runs
# may legitimately move large repos over slow links, so we allow up to 30
# minutes before declaring the transfer hung.
SSH_TIMEOUT_SEC = 60
RSYNC_TIMEOUT_SEC = 1800

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
    "hpc_mapreduce/",  # protect deployed runtime stubs from --delete
]


def _target(user: str, host: str) -> str:
    """Return ``user@host`` connection string."""
    return f"{user}@{host}"


def _truncate(text: str, limit: int = 120) -> str:
    """Return *text* truncated to *limit* characters with an ellipsis suffix."""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def ssh_run(
    cmd: str,
    *,
    host: str,
    user: str,
    capture: bool = True,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* on a remote host via SSH.

    Parameters
    ----------
    cmd:
        Shell command string to execute remotely.
    host:
        Cluster hostname (e.g. ``hoffman2.idre.ucla.edu``).
    user:
        SSH username on the cluster.
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
    argv = ["ssh", _target(user, host), cmd]
    try:
        return subprocess.run(
            argv,
            capture_output=capture,
            text=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"ssh to {user}@{host} timed out after {effective_timeout}s: {_truncate(cmd)}"
        ) from exc


def rsync_push(
    *,
    host: str,
    user: str,
    remote_path: str,
    local_path: str | Path,
    exclude: list[str] | None = None,
    delete: bool = True,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Sync a local directory to a remote host using rsync.

    Parameters
    ----------
    host:
        Cluster hostname.
    user:
        SSH username on the cluster.
    remote_path:
        Absolute path on the remote host (e.g. ``/u/home/user/project``).
    local_path:
        Local directory to push. Trailing slash is handled automatically.
    exclude:
        Rsync exclude patterns.  Defaults to :data:`DEFAULT_RSYNC_EXCLUDES`
        if *None*.
    delete:
        If True (default), pass ``--delete`` so removed local files are
        also removed on the remote.
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

    exclude_flags: list[str] = []
    for pattern in exclude:
        exclude_flags += ["--exclude", pattern]

    src = str(local_path).rstrip("/\\") + "/"
    dst = f"{_target(user, host)}:{remote_path.rstrip('/')}/"

    flags = ["rsync", "-az"]
    if delete:
        flags.append("--delete")

    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout
    try:
        return subprocess.run(
            [*flags, *exclude_flags, src, dst],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"rsync push to {host} timed out after {effective_timeout}s: "
            f"{_truncate(f'{src} -> {dst}')}"
        ) from exc


def deploy_runtime(
    *,
    host: str,
    user: str,
    remote_path: str,
) -> subprocess.CompletedProcess[str]:
    """Deploy minimal ``hpc_mapreduce`` runtime package to the cluster.

    Creates ``{remote_path}/hpc_mapreduce/map/`` with ``__init__.py`` stubs
    and copies of ``context.py`` and ``metrics_io.py`` so that
    ``from hpc_mapreduce.map.context import map_context`` and
    ``from hpc_mapreduce.map.metrics_io import write_metrics`` both work
    inside HPC jobs without installing the full claude-hpc package.

    Each underlying ssh/scp invocation is bounded by
    :data:`SSH_TIMEOUT_SEC`; if any of them exceeds it, a
    :class:`TimeoutError` is raised that names the host and the basename
    of the file being copied.

    Must be called **after** :func:`rsync_push` (which uses ``--delete``).
    """
    target = _target(user, host)
    remote_path_q = shlex.quote(remote_path)

    ssh_run(
        f"mkdir -p {remote_path_q}/hpc_mapreduce/map"
        f" && touch {remote_path_q}/hpc_mapreduce/__init__.py"
        f" && touch {remote_path_q}/hpc_mapreduce/map/__init__.py",
        host=host,
        user=user,
    )

    src = str(Path(__file__).parent.parent / "map" / "context.py")
    dst = f"{target}:{shlex.quote(remote_path)}/hpc_mapreduce/map/context.py"
    try:
        subprocess.run(
            ["scp", src, dst],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"scp to {host} timed out after {SSH_TIMEOUT_SEC}s: {Path(src).name}"
        ) from exc

    # Deploy the per-task metrics sidecar writer so executors can `from
    # hpc_mapreduce.map.metrics_io import write_metrics` on compute nodes.
    metrics_io_src = str(Path(__file__).parent.parent / "map" / "metrics_io.py")
    metrics_io_dst = f"{target}:{shlex.quote(remote_path)}/hpc_mapreduce/map/metrics_io.py"
    try:
        subprocess.run(
            ["scp", metrics_io_src, metrics_io_dst],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"scp to {host} timed out after {SSH_TIMEOUT_SEC}s: {Path(metrics_io_src).name}"
        ) from exc

    # Deploy the on-cluster combiner script.
    combiner_src = str(Path(__file__).parent.parent / "map" / "combiner.py")
    combiner_dst = f"{target}:{shlex.quote(remote_path)}/_hpc_combiner.py"
    try:
        return subprocess.run(
            ["scp", combiner_src, combiner_dst],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"scp to {host} timed out after {SSH_TIMEOUT_SEC}s: {Path(combiner_src).name}"
        ) from exc


def run_combiner(
    *,
    host: str,
    user: str,
    remote_path: str,
    wave: int,
    manifest_name: str = "_hpc_dispatch.json",
    force: bool = False,
    timeout: float | None = _DEFAULT,
) -> subprocess.CompletedProcess[str]:
    """Run the on-cluster combiner on the login node for a specific wave.

    Executes ``_hpc_combiner.py`` on the remote host via SSH.  The combiner
    accepts both CLI flags (preferred) and ``HPC_WAVE`` / ``HPC_MANIFEST``
    env vars (for back-compat with older deployed copies); we pass both
    so the same helper works against either version.

    Parameters
    ----------
    host:
        Cluster hostname.
    user:
        SSH username on the cluster.
    remote_path:
        Absolute path to the project directory on the remote host.
    wave:
        Wave number (0-based) to combine.
    manifest_name:
        Name of the manifest file (relative to *remote_path*).
    force:
        If True, append ``--force`` so the combiner overwrites any existing
        ``_combiner/wave_N.json`` output.
    timeout:
        Per-call subprocess timeout in seconds, threaded through to
        :func:`ssh_run`.  Defaults to :data:`SSH_TIMEOUT_SEC` when omitted.
        Pass ``timeout=None`` to disable enforcement.

    Raises
    ------
    TimeoutError
        If the underlying SSH call exceeds the timeout.
    """
    force_flag = " --force" if force else ""
    manifest_q = shlex.quote(manifest_name)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"HPC_WAVE={wave} HPC_MANIFEST={manifest_q} "
        f"python3 _hpc_combiner.py --wave {wave} --manifest {manifest_q}{force_flag}"
    )
    if timeout is _DEFAULT:
        return ssh_run(cmd, host=host, user=user)
    return ssh_run(cmd, host=host, user=user, timeout=timeout)


def run_combiner_checked(
    *,
    host: str,
    user: str,
    remote_path: str,
    wave: int,
    manifest_name: str = "_hpc_dispatch.json",
    force: bool = False,
    timeout: float | None = _DEFAULT,
) -> tuple[bool, str, str]:
    """Run the combiner and return ``(ok, stdout, stderr)``.

    Thin wrapper around :func:`run_combiner` that collapses
    ``CompletedProcess`` into a simple tuple, saving callers (especially
    the LLM orchestrator) from having to know the subprocess API.

    ``ok`` is ``True`` iff the remote combiner exited with returncode ``0``.

    *timeout* is threaded through to :func:`run_combiner` (and onward to
    :func:`ssh_run`); see those for semantics.  A timeout propagates as
    :class:`TimeoutError` rather than collapsing into ``ok=False``, so
    callers can distinguish "remote returned non-zero" from "we never
    heard back".
    """
    if timeout is _DEFAULT:
        result = run_combiner(
            host=host,
            user=user,
            remote_path=remote_path,
            wave=wave,
            manifest_name=manifest_name,
            force=force,
        )
    else:
        result = run_combiner(
            host=host,
            user=user,
            remote_path=remote_path,
            wave=wave,
            manifest_name=manifest_name,
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
    host: str,
    user: str,
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
    host:
        Cluster hostname.
    user:
        SSH username on the cluster.
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
    src = (
        f"{_target(user, host)}:"
        f"{shlex.quote(remote_path.rstrip('/'))}/"
        f"{shlex.quote(remote_subdir.strip('/'))}/"
    )

    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    dst = str(dst_path).rstrip("/\\") + "/"

    filter_flags: list[str] = []
    if include is not None:
        filter_flags += ["--include=*/"]
        for pattern in include:
            filter_flags += [f"--include={pattern}"]
        filter_flags += ["--exclude=*"]

    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout
    try:
        return subprocess.run(
            ["rsync", "-az", *filter_flags, src, dst],
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"rsync pull from {host} timed out after {effective_timeout}s: "
            f"{_truncate(f'{src} -> {dst}')}"
        ) from exc
