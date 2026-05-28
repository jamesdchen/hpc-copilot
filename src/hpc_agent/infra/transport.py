"""File-transport helpers: rsync push/pull, scp/tar fallbacks, runtime deploy.

Extracted from :mod:`hpc_agent.infra.remote` so the remote-IO module can
stay focused on the bare ``ssh_run`` + throttle-detection plumbing. The
helpers here orchestrate ``rsync`` / ``scp`` / ``tar | ssh`` subprocesses
to move files between the local machine and the cluster.

Re-exported from :mod:`hpc_agent.infra.remote` for backwards
compatibility with existing callers (``from hpc_agent.infra.remote
import rsync_push``).
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

from hpc_agent.infra.remote import (
    RSYNC_TIMEOUT_SEC,
    SSH_TIMEOUT_SEC,
    _truncate,
    _with_ssh_backoff,
    ssh_run,
)
from hpc_agent.infra.ssh_options import _rsync_rsh_env, _scp_binary, _ssh_binary
from hpc_agent.infra.ssh_validation import validate_remote_path

__all__ = [
    "DEFAULT_RSYNC_EXCLUDES",
    "MANDATORY_RSYNC_EXCLUDES",
    "deploy_runtime",
    "rsync_pull",
    "rsync_push",
    "run_combiner",
    "run_combiner_checked",
]


# Sentinel marker meaning "caller did not specify a timeout". Mirrors the
# one in :mod:`hpc_agent.infra.remote` — both modules expose the same
# ``timeout=`` contract on their public functions and need a distinct
# value from ``None`` (which is the "disable enforcement" escape hatch).
_DEFAULT: Final[Any] = object()

DEFAULT_RSYNC_EXCLUDES: list[str] = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".claude/",
    # Virtualenvs / package caches: gigabytes that get re-diffed and
    # re-sent on every submit, and that the cluster job never reads (it
    # builds its own env from MODULES / CONDA_ENV / `uv sync`).
    ".venv/",
    "venv/",
    "node_modules/",
    "hpc_agent/",  # protect deployed runtime stubs from --delete
    # Protect framework files scp'd into the cluster-side .hpc/ from the
    # local rsync's --delete pass.  The local .hpc/ contains only
    # tasks.py + runs/<id>.json; the cluster also holds _hpc_dispatch.py,
    # _hpc_combiner.py, and templates/ placed there by deploy_runtime.
    ".hpc/_hpc_dispatch.py",
    ".hpc/_hpc_combiner.py",
    ".hpc/templates/",
]

# Patterns that must NEVER ship to the cluster, regardless of what
# ``exclude`` a caller passes. ``clusters.yaml`` holds real cluster
# credentials (user/host/scratch paths) and is gitignored locally for
# exactly that reason; when it lives inside the experiment dir (the
# documented demo layout puts it at the repo root with
# ``HPC_CLUSTERS_CONFIG`` pointing there) a default push would rsync it
# onto a shared cluster filesystem. These are unioned into every
# transfer's exclude set so an explicit ``rsync_excludes`` cannot drop
# the protection. Bare names (no ``/``) so rsync/tar match the file at
# any depth in the tree.
MANDATORY_RSYNC_EXCLUDES: list[str] = [
    "clusters.yaml",
]


def _effective_excludes(exclude: list[str] | None) -> list[str]:
    """Resolve the exclude list, always enforcing :data:`MANDATORY_RSYNC_EXCLUDES`.

    ``None`` selects :data:`DEFAULT_RSYNC_EXCLUDES`. The mandatory
    credential-protecting patterns are appended (de-duplicated) so a
    caller-supplied list can never re-expose ``clusters.yaml``.
    """
    base = DEFAULT_RSYNC_EXCLUDES if exclude is None else list(exclude)
    out = list(base)
    for pat in MANDATORY_RSYNC_EXCLUDES:
        if pat not in out:
            out.append(pat)
    return out


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
    ssh_cmd = [_ssh_binary(), "-o", "BatchMode=yes", ssh_target, ssh_remote_cmd]

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

    scp_cmd = [_scp_binary(), "-r", "-o", "BatchMode=yes", src, dst]
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
        if *None*.  :data:`MANDATORY_RSYNC_EXCLUDES` (the credential file
        ``clusters.yaml``) is always unioned in — a caller cannot drop it
        by passing an explicit list.
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
    exclude = _effective_excludes(exclude)
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

    rsync_env = {**os.environ, **_rsync_rsh_env()}

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [*flags, *exclude_flags, src, dst],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
                env=rsync_env,
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

    # The deployed ``hpc_agent/`` is a PEP 420 namespace package — NO
    # ``__init__.py`` anywhere in the tree. ``hpc_preamble.sh`` prepends
    # ``$REPO_DIR`` to PYTHONPATH; if this directory had an ``__init__.py``
    # it would bind ``hpc_agent`` to the two-module stub and *shadow* a
    # real ``pip install``ed hpc_agent in the cluster env, breaking every
    # import outside the stub (e.g. ``hpc_agent.experiment_kit``). As a
    # namespace portion it instead merges with / yields to the installed
    # regular package, so the install wins when present and the stub still
    # resolves ``metrics_io`` + ``executor_cli`` when it isn't.
    #
    # ``rm -f`` clears stale ``__init__.py`` files left by pre-fix deploys
    # (rsync's ``--delete`` excludes ``hpc_agent/`` so they would persist).
    ssh_run(
        f"mkdir -p {remote_path_q}/hpc_agent/models/mapreduce"
        f" {remote_path_q}/.hpc/templates"
        f" {remote_path_q}/.hpc/templates/common"
        f" && rm -f {remote_path_q}/hpc_agent/__init__.py"
        f" {remote_path_q}/hpc_agent/models/__init__.py"
        f" {remote_path_q}/hpc_agent/models/mapreduce/__init__.py"
        # Purge stale compiled artifacts in the deployed tree. A Py2.7
        # ``__init__.pyc`` left *beside* the (now-absent) ``__init__.py`` is
        # imported directly by Py3 as the package init -> ``bad magic
        # number``, shadowing the conda install and killing every
        # cluster-side verb. rsync ``--delete`` excludes ``hpc_agent/`` (see
        # DEFAULT_RSYNC_EXCLUDES) so nothing else ever cleans this dir; the
        # ``.py`` removal above doesn't touch ``.pyc`` / ``__pycache__``.
        f" && find {remote_path_q}/hpc_agent -name '*.pyc' -delete"
        f" && find {remote_path_q}/hpc_agent -depth -type d -name __pycache__"
        f" -exec rm -rf {{}} +",
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
                    [_scp_binary(), "-o", "BatchMode=yes", str(src), dst],
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
    remote_activation: str = "",
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
        f"{remote_activation}"
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
    remote_activation: str = "",
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
            remote_activation=remote_activation,
        )
    else:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            timeout=timeout,
            remote_activation=remote_activation,
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

    rsync_env = {**os.environ, **_rsync_rsh_env()}

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                ["rsync", "-az", *filter_flags, src, dst],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
                env=rsync_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync pull from {ssh_target} timed out after {effective_timeout}s: "
                f"{_truncate(f'{src} -> {dst}')}"
            ) from exc

    return _with_ssh_backoff(_run, label=f"rsync pull {ssh_target}")
