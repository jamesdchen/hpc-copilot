"""Shared SSH-shim mixin for remote scheduler backends.

Both :class:`hpc_agent.infra.backends.sge_remote.RemoteSGEBackend`
and :class:`hpc_agent.infra.backends.slurm_remote.RemoteSlurmBackend`
need the exact same two overrides on top of their local cousin:

- ``_execute_command`` — wrap the scheduler invocation in
  ``cd <remote_repo> && <cmd>`` and run it via the injected ``ssh_run``
  callable.
- ``_setup_log_dir`` — ``mkdir -p`` the remote log dir over SSH.

This module exposes :class:`RemoteHPCBackend` as a mixin (placed FIRST
in the MRO) so each Remote backend simply does::

    class RemoteSGEBackend(RemoteHPCBackend, SGEBackend): ...

and inherits ``_build_command`` / ``_build_dependency_flag`` /
``JOB_ID_REGEX`` from the local class while overriding the two SSH
hooks here.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


# --- S5: single-source REPO_DIR ↔ deploy target ----------------------------
#
# Every remote operation in the submit pipeline anchors on ONE string,
# ``remote_path``:
#
#   * ``rsync_push(remote_path=...)`` ships the experiment tree to
#     ``{ssh_target}:{remote_path}/`` (transport.rsync_push).
#   * ``deploy_runtime(remote_path=...)`` ships the framework files to the
#     SAME ``{remote_path}/`` (transport.deploy_runtime).
#   * the backend's ``remote_repo`` (where every ``cd "$REPO_DIR" && <cmd>``
#     lands, _execute_command below) is ``remote_path`` verbatim
#     (remote_factory.build_remote_backend).
#   * ``job_env["REPO_DIR"]`` is set to ``remote_path`` (build_submit_spec).
#
# So the rsync DESTINATION and the cluster-side ``REPO_DIR`` are, by
# construction, the same value — UNLESS a caller threads a divergent
# ``REPO_DIR`` (a stale/hand-rolled ``extra_env`` override, or a cached spec
# field) that overrides the one derived from ``remote_path``. That is exactly
# the drift the 2026-06 live canary hit: ``REPO_DIR=…/hpc-demo`` while rsync
# had deployed to ``…/demo-hpc``, so the per-task ``cd "$REPO_DIR" &&
# <executor>`` ran in a directory the executor was never deployed to and the
# dispatch failed.
#
# :func:`deploy_target_for` is the single derivation; build_submit_spec sets
# REPO_DIR from it AND asserts no override diverged. Keeping the derivation
# here (next to ``remote_repo`` / ``_execute_command``, which consume the same
# value) means the REPO_DIR ↔ deploy-target identity has one owner, not two
# independently-maintained call sites that can drift.


def deploy_target_for(remote_path: str) -> str:
    """The canonical cluster-side deploy target derived from *remote_path*.

    This is the single source of truth for "where the rsync lands AND where
    ``cd "$REPO_DIR"`` must point". Both rsync_push/deploy_runtime and the
    backend's ``remote_repo`` strip a trailing slash before use; mirror that
    normalisation here so an equality check against either is exact.

    The function is intentionally the (normalising) identity on *remote_path*:
    the framework has no separate deploy-destination computation that could
    legitimately diverge from ``REPO_DIR``. The point of routing both through
    one helper is that ``build_submit_spec`` can derive ``REPO_DIR`` from it and
    assert nothing overrode the result — so a stale/hand-rolled divergent
    ``REPO_DIR`` is refused at the submission boundary instead of surfacing as a
    cluster ``dispatcher_failed`` a full canary round-trip later.
    """
    return remote_path.rstrip("/")


def executor_script_path(executor: str) -> str | None:
    """The script path an ``EXECUTOR`` command will ``cd "$REPO_DIR"`` then run.

    Reuses the same ``shlex.split`` extraction
    :func:`hpc_agent.infra.executor_guard._check_register_run_executor`
    uses (#292): the per-task command lands relative to ``REPO_DIR``, so the
    file the cluster needs is the first ``<path>.py`` token in the command.

    Returns the relative/absolute script path string, or ``None`` when the
    command carries no checkable ``.py`` script token (the canonical
    ``python3 -c "..."`` one-liner, a ``-m`` module run, a ``run-module``
    dispatch, or an unparseable command) — in which case the remote
    existence preflight has nothing file-shaped to probe and no-ops.
    """
    try:
        parts = shlex.split(executor or "")
    except ValueError:
        return None
    for tok in parts:
        # Skip the interpreter and any flags; the first ``.py`` positional is
        # the script the per-task command runs. A ``-c`` / ``-m`` form has no
        # such token, so we return None (nothing file-shaped to probe).
        if tok.startswith("-"):
            continue
        if tok.endswith(".py"):
            return tok
    return None


def preflight_executor_exists(
    *,
    ssh_target: str,
    remote_path: str,
    executor: str,
    ssh_run: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """Post-deploy, pre-canary: assert the executor file exists under REPO_DIR.

    The missing layer the 2026-06 canary needed (#S5/incident 6). After rsync +
    deploy land the tree at ``remote_path`` and BEFORE any canary/array is
    scheduled, one cheap ``test -f "$REPO_DIR/<executor-file>"`` over SSH
    surfaces a REPO_DIR/deploy mismatch (or a misnamed/absent executor) in
    seconds — versus discovering it only when a scheduled task runs
    ``cd "$REPO_DIR" && <executor>`` and fails ``dispatcher_failed`` a full
    scheduler round-trip later.

    *ssh_run* is injected (the production caller passes
    :func:`hpc_agent.infra.remote.ssh_run`) so the SSH round-trip is mockable in
    tests. It is invoked as ``ssh_run(cmd, ssh_target=ssh_target)`` and is
    expected to return an object with ``.returncode`` (a
    ``subprocess.CompletedProcess``-shaped result).

    No-ops when the executor carries no checkable script path
    (:func:`executor_script_path` is ``None`` — the ``python3 -c "..."``
    one-liner, a ``-m`` / ``run-module`` dispatch): there is no single file to
    probe, so the check would only false-positive. An ABSOLUTE script path is
    probed verbatim; a relative one is anchored at ``REPO_DIR`` (where the
    per-task ``cd "$REPO_DIR"`` runs it).

    Raises :class:`hpc_agent.errors.RemoteCommandFailed` with an
    ``executor_missing_at_repo_dir`` marker when the file is absent.
    """
    from hpc_agent import errors

    script = executor_script_path(executor)
    if script is None:
        return
    repo_dir = deploy_target_for(remote_path)
    full = script if script.startswith("/") else f"{repo_dir}/{script}"
    quoted = shlex.quote(full)
    probe = ssh_run(f"test -f {quoted}", ssh_target=ssh_target)
    if getattr(probe, "returncode", 1) == 0:
        return
    raise errors.RemoteCommandFailed(
        "executor_missing_at_repo_dir: the per-task executor file "
        f"{full!r} does not exist under REPO_DIR ({repo_dir!r}) on "
        f"{ssh_target} after rsync+deploy. The cluster-side dispatch runs "
        '`cd "$REPO_DIR" && <executor>`, so this submission would have failed '
        "every task with dispatcher_failed (the 2026-06 live-canary class: "
        "REPO_DIR diverged from where rsync actually deployed, or the executor "
        "file is misnamed/absent in the deployed tree). Confirm remote_path "
        "matches the rsync deploy target and that the executor's script is part "
        "of the deployed bundle (not stripped by an rsync exclude), then "
        "resubmit."
    )


# stderr marker the first (login-shell) submit uses to piggyback binary
# resolution onto its own round-trip — see RemoteHPCBackend._execute_command.
_BIN_MARKER = "__HPC_SUBMIT_BIN__"


class RemoteHPCBackend:
    """SSH shim for scheduler backends.

    Subclasses are expected to set the following instance attributes
    (typically in their ``__init__`` via ``super().__init__(...)``):

    - ``ssh_run`` — ``Callable[[str], subprocess.CompletedProcess[str]]``
    - ``remote_repo`` — absolute path on the remote host
    - ``log_dir`` — remote log directory
    """

    ssh_run: Callable[[str], subprocess.CompletedProcess[str]]
    remote_repo: str
    log_dir: str

    # Per-instance cache: submit binary name -> absolute path on the remote
    # host, harvested from the first login-shell submit's stderr marker.
    # Instance-scoped (not module) so two backends pointed at different
    # clusters can never cross-pollinate paths.
    _resolved_bins: dict[str, str]

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Execute *cmd* on the remote host via SSH.

        ``cd <remote_repo> && <quoted-cmd>`` is the canonical pattern —
        the job script is referenced by relative path in the local
        backend's ``_build_command``, so we need to land in the right
        directory before invoking qsub/sbatch.

        The cd+submit is wrapped in ``bash -lc`` (LOGIN, non-interactive) so
        the remote shell sources the cluster's login profile sequence
        (``/etc/profile`` → ``/etc/profile.d/*.sh`` → ``~/.bash_profile``).
        Many clusters (Hoffman2/UGE, CARC, etc.) install ``qsub`` / ``sbatch``
        onto ``PATH`` only via that init — the bare ssh command channel is
        non-login and would fail ``bash: qsub: command not found``.

        Do NOT add ``-i`` here. An interactive bash on an ssh *exec* channel
        (no PTY — ``ssh_run`` allocates none) blocks: interactive init expects
        a terminal / job control and hangs until the ``_execute_command``
        120 s timeout fires, which the flow then misreports as
        ``dispatcher_failed`` (proving-run #2, 2026-07: ``bash -lic`` hung the
        Hoffman2 canary submit before qsub was ever reached, so nothing hit
        the scheduler; ``bash -lc`` resolves ``qsub`` at
        ``/u/systems/UGE8.6.4/bin/lx-amd64/qsub`` and returns cleanly). The
        earlier ``-i`` (commit cafb160b) rested on the assumption that some
        clusters expose the scheduler PATH only via an interactivity-guarded
        ``~/.bashrc``; that is empirically false on Hoffman2, and a cluster
        that genuinely needed it must express it through the preamble
        (``conda_source`` / ``modules``), never a globally-hanging ``-i``.

        Login-shell amortisation (proving-run-2 Phase-0 measurement,
        2026-07-04): sourcing that profile chain costs ~1.2 s *server-side*
        per call on Hoffman2 — twice the SSH handshake — with wild variance
        under login-node load (a 22.8 s outlier was observed). So the login
        shell is paid ONCE per (backend instance, binary): the first submit
        piggybacks ``command -v <bin>`` onto its own ``bash -lc`` round-trip
        (a ``__HPC_SUBMIT_BIN__=<path>`` marker on stderr — stderr, never
        stdout, which the job-id regex parses), and every later submit runs
        the cached absolute path with NO login shell. A cached path that
        stops resolving (exit 127 / stale after a cluster upgrade) is
        dropped and that call falls back to the login-shell form — the
        fallback self-heals by re-harvesting the marker.
        """
        cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
        bin_name = cmd[0]
        cached = getattr(self, "_resolved_bins", {}).get(bin_name)
        if cached:
            abs_cmd_str = " ".join([shlex.quote(cached), *(shlex.quote(arg) for arg in cmd[1:])])
            direct = f"cd {shlex.quote(self.remote_repo)} && {abs_cmd_str}"
            proc = self.ssh_run(direct)
            if proc.returncode != 127:
                return proc
            # Stale path (cluster upgrade moved the scheduler tree): drop the
            # cache entry and fall through to the login-shell form below,
            # which re-resolves via the marker.
            self._resolved_bins.pop(bin_name, None)
        inner = (
            f'echo "{_BIN_MARKER}=$(command -v {shlex.quote(bin_name)})" 1>&2; '
            f"cd {shlex.quote(self.remote_repo)} && {cmd_str}"
        )
        remote_cmd = f"bash -lc {shlex.quote(inner)}"
        proc = self.ssh_run(remote_cmd)
        self._harvest_bin_marker(bin_name, proc)
        return proc

    def _harvest_bin_marker(self, bin_name: str, proc: subprocess.CompletedProcess[str]) -> None:
        """Cache the absolute binary path the login-shell call resolved.

        Reads the ``__HPC_SUBMIT_BIN__=<path>`` marker off *proc*'s stderr.
        Absent/empty marker (e.g. an injected test runner that doesn't emit
        it, or ``command -v`` finding nothing) caches nothing — the next call
        just pays the login shell again, never a wrong path.
        """
        stderr = getattr(proc, "stderr", None) or ""
        for line in stderr.splitlines():
            if line.startswith(f"{_BIN_MARKER}="):
                path = line.split("=", 1)[1].strip()
                if path.startswith("/"):
                    if not hasattr(self, "_resolved_bins"):
                        self._resolved_bins = {}
                    self._resolved_bins[bin_name] = path
                return

    def _setup_log_dir(self) -> None:
        """Create the log directory on the remote host via SSH."""
        self.ssh_run(f"mkdir -p {shlex.quote(self.log_dir)}")
