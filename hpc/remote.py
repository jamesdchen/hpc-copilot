"""SSH and rsync utilities for remote HPC operations.

Provides thin wrappers around ssh/rsync so cluster commands can be
executed from a local machine without paramiko or other dependencies.

All functions require explicit ``host``, ``user``, and ``remote_path``
parameters — there are no hardcoded defaults.  Callers obtain these
values from ``clusters.yaml`` + ``project.yaml`` via :mod:`hpc._config`.
"""

from __future__ import annotations

__all__ = [
    "ssh_run",
    "rsync_push",
    "rsync_pull",
    "deploy_runtime",
]

import subprocess
from pathlib import Path

DEFAULT_RSYNC_EXCLUDES: list[str] = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".claude/",
    "hpc/",  # protect deployed runtime stubs from --delete
]


def _target(user: str, host: str) -> str:
    """Return ``user@host`` connection string."""
    return f"{user}@{host}"


def ssh_run(
    cmd: str,
    *,
    host: str,
    user: str,
    capture: bool = True,
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

    Returns
    -------
    subprocess.CompletedProcess with returncode, stdout, stderr.
    """
    return subprocess.run(
        ["ssh", _target(user, host), cmd],
        capture_output=capture,
        text=True,
    )


def rsync_push(
    *,
    host: str,
    user: str,
    remote_path: str,
    local_path: str | Path,
    exclude: list[str] | None = None,
    delete: bool = True,
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

    return subprocess.run(
        [*flags, *exclude_flags, src, dst],
        capture_output=True,
        text=True,
    )


def deploy_runtime(
    *,
    host: str,
    user: str,
    remote_path: str,
) -> subprocess.CompletedProcess[str]:
    """Deploy minimal ``hpc`` runtime package to the cluster.

    Creates ``{remote_path}/hpc/`` with an empty ``__init__.py`` and a
    copy of ``chunking.py`` so that ``from hpc.chunking import chunk_context``
    works inside HPC jobs without installing the full claude-hpc package.

    Must be called **after** :func:`rsync_push` (which uses ``--delete``).
    """
    target = _target(user, host)

    ssh_run(
        f"mkdir -p {remote_path}/hpc && touch {remote_path}/hpc/__init__.py",
        host=host,
        user=user,
    )

    src = str(Path(__file__).parent / "chunking.py")
    dst = f"{target}:{remote_path}/hpc/chunking.py"
    return subprocess.run(
        ["scp", src, dst],
        capture_output=True,
        text=True,
    )


def rsync_pull(
    *,
    host: str,
    user: str,
    remote_path: str,
    remote_subdir: str,
    local_dir: str | Path,
    include: list[str] | None = None,
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
    """
    src = f"{_target(user, host)}:{remote_path.rstrip('/')}/{remote_subdir.strip('/')}/"

    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    dst = str(dst_path).rstrip("/\\") + "/"

    filter_flags: list[str] = []
    if include is not None:
        filter_flags += ["--include=*/"]
        for pattern in include:
            filter_flags += [f"--include={pattern}"]
        filter_flags += ["--exclude=*"]

    return subprocess.run(
        ["rsync", "-az", *filter_flags, src, dst],
        capture_output=True,
        text=True,
    )
