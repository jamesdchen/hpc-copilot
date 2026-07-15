"""File-transport helpers: rsync push/pull, scp/tar fallbacks, runtime deploy.

Extracted from :mod:`hpc_agent.infra.remote` so the remote-IO module can
stay focused on the bare ``ssh_run`` + throttle-detection plumbing. The
helpers here orchestrate ``rsync`` / ``scp`` / ``tar | ssh`` subprocesses
to move files between the local machine and the cluster.

Re-exported from :mod:`hpc_agent.infra.remote` for backwards
compatibility with existing callers (``from hpc_agent.infra.remote
import rsync_push``).

This ``__init__`` is the transfer/deploy ENGINE seat: every function that
drives a subprocess under a live ``transport.*`` patch — ``rsync_push`` /
``rsync_pull`` / ``_tar_ssh_push`` / ``_scp_pull`` / ``deploy_runtime`` and
their helpers — stays here, together with the module-level names tests patch
through the namespace (``subprocess`` / ``shutil`` / ``sys`` / ``Path`` /
``run_capture_bounded`` / ``ssh_run``). Six private leaf submodules
(``_excludes`` / ``_disclose`` / ``_delta`` / ``_prune`` / ``_deploy_items`` /
``_combiner``) plus ``_shared`` carve out the cohesive clusters; the re-export
block below pulls every one of their symbols back onto this namespace so the
public import path AND every cross-imported/patched private
(``transport._build_deploy_items`` etc.) resolve exactly as before.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Final

from hpc_agent.infra.bounded_subprocess import run_capture_bounded
from hpc_agent.infra.remote import (
    RSYNC_TIMEOUT_SEC,
    SSH_TIMEOUT_SEC,
    _env_int,
    _truncate,
    _with_ssh_backoff,
    ssh_run,
)
from hpc_agent.infra.ssh_circuit import guarded_call
from hpc_agent.infra.ssh_options import run_with_named_pipe_retry, ssh_argv, ssh_env
from hpc_agent.infra.ssh_throttle import throttle_connection
from hpc_agent.infra.ssh_validation import validate_remote_path

# ── re-export the leaf submodules' symbols onto this package namespace ──
#
# The engine functions below call these moved collaborators by their bare name,
# and tests reach many of the privates via ``hpc_agent.infra.transport.<name>``
# (patch seams + direct attribute reads). Importing every submodule symbol here
# keeps both resolving exactly as they did when this was one flat module. The
# four public exclude constants + the three combiner verbs are surfaced through
# ``__all__``; each private carries a per-line F401 suppression because it is a
# re-export, not a local use (the privates are deliberately NOT promoted into the
# public ``__all__``).
from ._combiner import (
    run_combiner,
    run_combiner_checked,
    run_final_reduce,
)
from ._delta import (
    _DELTA_ENV_KILL,  # noqa: F401
    _DELTA_MANIFEST_FILE_CAP,  # noqa: F401
    _PUSH_HASH_CACHE_REL,  # noqa: F401
    _REMOTE_MANIFEST_SNIPPET,  # noqa: F401
    _build_local_manifest_cached,  # noqa: F401
    _delta_batch_caps,  # noqa: F401
    _delta_ship_batches,  # noqa: F401
    _disclose_delta_batch,  # noqa: F401
    _load_hash_cache,  # noqa: F401
    _local_push_manifest,  # noqa: F401
    _parse_remote_push_manifest,  # noqa: F401
    _remote_push_manifest,  # noqa: F401
    _store_hash_cache,  # noqa: F401
)
from ._deploy_items import (
    _DEPLOY_MANIFEST_REL,  # noqa: F401
    _build_deploy_items,  # noqa: F401
    _DeployItem,  # noqa: F401
    _local_deploy_manifest,  # noqa: F401
    _parse_remote_manifest,  # noqa: F401
    _pkg_version,  # noqa: F401
    _sha256_bytes,  # noqa: F401
)
from ._disclose import (
    _PAYLOAD_WALK_CAP,  # noqa: F401
    _PAYLOAD_WARN_BYTES,  # noqa: F401
    _PROGRESS_INTERVAL_SEC,  # noqa: F401
    _PUMP_CHUNK_BYTES,  # noqa: F401
    DeployPayloadSummary,
    _disclose_delta_mode,  # noqa: F401
    _disclose_no_rsync,  # noqa: F401
    _disclose_payload,  # noqa: F401
    _disclose_prune,  # noqa: F401
    _emit_progress,  # noqa: F401
    _pump_with_progress,  # noqa: F401
    _write_all,  # noqa: F401
    deploy_payload_summary,
    disclose_child_failure,  # noqa: F401
)
from ._excludes import (
    _GENERATED_SHIPPABLE,  # noqa: F401
    DEFAULT_RSYNC_EXCLUDES,
    MANDATORY_RSYNC_EXCLUDES,
    PROTECTED_OUTPUT_DIRS,
    PROTECTED_RUNTIME_FILES,
    _effective_excludes,  # noqa: F401
    _is_runtime_placed,  # noqa: F401
    _path_excluded,  # noqa: F401
    _pushable_relpaths,  # noqa: F401
)
from ._prune import (
    _PRUNE_ENV_KILL,  # noqa: F401
    _PUSH_MANIFEST_REL,  # noqa: F401
    _PUSH_MANIFEST_TMP_REL,  # noqa: F401
    _execute_prune,  # noqa: F401
    _journal_deploy_prune,  # noqa: F401
    _prune_max_bytes,  # noqa: F401
    _prune_max_files,  # noqa: F401
    _read_prior_push_manifest,  # noqa: F401
    _write_push_manifest,  # noqa: F401
)
from ._shared import _DEFAULT

__all__ = [
    "DEFAULT_RSYNC_EXCLUDES",
    "MANDATORY_RSYNC_EXCLUDES",
    "PROTECTED_OUTPUT_DIRS",
    "PROTECTED_RUNTIME_FILES",
    "DeployPayloadSummary",
    "deploy_payload_summary",
    "deploy_runtime",
    "rsync_pull",
    "rsync_push",
    "run_combiner",
    "run_combiner_checked",
    "run_final_reduce",
]


# The remote ``--delete`` pre-clean (tar fallback) gets its OWN timeout,
# distinct from — and shorter than — the (30-min) transfer timeout, so a
# pathological clean fails loud fast instead of silently eating the transfer
# budget and wedging the push (#173). Override via ``HPC_PRECLEAN_TIMEOUT_SEC``.
PRECLEAN_TIMEOUT_SEC: Final[int] = _env_int("HPC_PRECLEAN_TIMEOUT_SEC", 300)


def _have_rsync() -> bool:
    """Return True if an ``rsync`` binary is on PATH.

    Detection at runtime via :func:`shutil.which`. Activates the scp/tar
    fallback when False (typically Windows hosts without WSL/MSYS rsync).
    """
    return shutil.which("rsync") is not None


def _msys_local(p: str) -> str:
    """Translate a native-Windows local path into the ``/c/...`` form MSYS rsync
    parses as a LOCAL operand.

    On win32 an MSYS/cygwin rsync parses a drive colon (``C:/x``) as a REMOTE
    host spec (host ``C``) and dies with "source and destination cannot both be
    remote" (run-#12 finding 17). Every Windows rsync build accepts the
    ``/c/...`` form. The trailing slash (and any other structure) is preserved;
    off win32, or for a path without a drive colon, *p* is returned unchanged.

    Shared by :func:`rsync_push` (src), :func:`_rsync_deploy` (local staging),
    and :func:`rsync_pull` (dst) so all three local operands get the fix (#10,
    #11) — the translation used to live inline in ``rsync_push`` only.
    """
    if sys.platform == "win32" and len(p) > 1 and p[1] == ":":
        return ("/" + p[0].lower() + "/" + p[3:]).replace("\\", "/")
    return p


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

    Two passes, files then dirs, because a protected subtree does not
    protect its PARENT directory from ``find``'s pre-order print — a
    single ``rm -rf`` over every printed path deleted ``.hpc/`` wholesale
    (templates, dispatcher and all) on every re-push, defeating
    :data:`PROTECTED_RUNTIME_FILES` (audit 2026-07-09). Pass 1 removes
    non-directories only, so a dir with protected descendants is never
    ``rm -rf``-d through. Pass 2 removes the now-empty directories,
    children-first (``sort -rz``: a parent path is a strict prefix of its
    children's, so reverse-lexicographic order is depth-first), with
    ``rmdir --ignore-fail-on-non-empty`` leaving any dir that still holds
    protected content standing. Empty stale dirs must go, not linger: a
    leftover package dir is importable as a PEP 420 namespace package and
    can shadow a real module.
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
    # -print0 / xargs -0 / sort -z keep paths with spaces intact; -r skips
    # the delete verb on empty input; -- stops a dash-led name reading as a
    # flag. Each pipeline's exit status is its delete verb's: rm -f is 0
    # even if find races a just-deleted entry, and rmdir's non-empty
    # failures are explicitly ignored.
    files_pass = f"{find_cmd} ! -type d -print0 | xargs -0 -r rm -f --"
    dirs_pass = (
        f"{find_cmd} -type d -print0 | sort -rz | xargs -0 -r rmdir --ignore-fail-on-non-empty --"
    )
    return f"{files_pass} && {dirs_pass}"


def _stage_swap_cmd(stage_path: str, remote_path: str) -> str:
    """Build the remote command that merges the staged tree into the live one.

    The swap must MERGE, not move: the pre-clean deliberately preserves
    protected paths (``.hpc/templates/``, ``results/``, ...), so the live
    tree's directories are non-empty on every re-push — and ``mv`` cannot
    move a directory onto an existing non-empty one (``Directory not
    empty``, which used to kill every re-push AFTER the pre-clean had
    already deleted the unprotected files). ``cp -a`` merges into existing
    directories, preserving modes/times, and is purely additive — a failure
    mid-copy leaves the staging dir intact (the ``&&`` skips the cleanup)
    and never deletes anything the bounded clean didn't. The deployed tree
    is small (the big output dirs are excluded from the push), so the local
    remote-side copy stays within the same seconds-scale exposure window
    the same-filesystem move had.
    """
    quoted_stage = shlex.quote(stage_path)
    quoted_remote = shlex.quote(remote_path)
    # ``<stage>/.`` copies the staged tree's CONTENTS (dotfiles included)
    # into the live root.
    return f"cp -a {quoted_stage}/. {quoted_remote}/ && rm -rf {quoted_stage}"


def _remote_preclean(
    *,
    ssh_target: str,
    remote_path: str,
    exclude: list[str],
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run the remote ``--delete`` pre-clean as its OWN bounded ssh call (#173).

    Split out from the tar extract so the clean and the transfer carry DISTINCT
    timeouts and DISTINCT failures. A pathological clean — e.g. a crash-loop's
    debris tree under a path the prune set doesn't cover — now fails loud on its
    own (short) timeout with an actionable message, instead of silently
    consuming the (30-min) transfer budget and wedging the whole push.

    The prune set (*exclude*, which always carries :data:`PROTECTED_OUTPUT_DIRS`)
    keeps ``find`` from ever descending into ``results/`` — the actual
    quarter-million-inode debris source — so a healthy clean touches only the
    small deployed code/runtime tree.

    Uses :func:`subprocess.run` directly (mirroring the extract leg below)
    rather than :func:`ssh_run` so the timeout is enforced per this single
    invocation. *remote_path* was already ``validate_remote_path``-d by the
    caller and every interpolated value is ``shlex.quote``-d in
    :func:`_remote_clean_cmd`.
    """
    quoted_remote = shlex.quote(remote_path)
    clean_cmd = f"mkdir -p {quoted_remote} && {_remote_clean_cmd(remote_path, exclude)}"

    def _attempt() -> subprocess.CompletedProcess[str]:
        # Rebuild ssh_cmd inside so a named-pipe retry picks up the
        # updated :func:`_ssh_multiplex_opts` after
        # :func:`mark_named_pipe_broken`. The remote ``clean_cmd`` itself
        # is constant — only the ssh-side opts change.
        ssh_cmd = [*ssh_argv("ssh"), ssh_target, clean_cmd]
        try:
            return run_capture_bounded(ssh_cmd, timeout_sec=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"remote --delete pre-clean of {remote_path} on {ssh_target} timed out "
                f"after {timeout}s, before the transfer could start. This usually means a "
                "large debris tree (e.g. crash-loop WIP dirs under results/) under a path "
                "the pre-clean still traverses. Clean it manually (e.g. "
                f"`rm -rf {remote_path.rstrip('/')}/results/<run_id>`) or push with delete=False."
            ) from exc

    # Auto-fallback on the syscall-layer named-pipe ControlMaster failure
    # mode (Windows OpenSSH version probe can't catch it; 2026-06-04). The
    # preclean is the LAST ssh-touching surface that wasn't wrapped —
    # ``ssh_run`` and ``rsync_push``/``_tar_ssh_push`` already had the
    # retry helper. Without this wrap a preclean that hit the marker would
    # surface a hard failure even if the actual cluster connectivity was
    # fine via the legacy ControlMaster=no path.
    return run_with_named_pipe_retry(_attempt)


def _ssh_bounded(
    ssh_target: str,
    remote_cmd: str,
    *,
    timeout: float | None,
    what: str,
) -> subprocess.CompletedProcess[str]:
    """One bounded remote command, named-pipe-retry wrapped (the #173 shape).

    The small helper for the stage-then-swap legs (stage drop, post-extract
    move): each runs as its OWN short ssh call so it can never eat the
    transfer budget, and a timeout surfaces loud with *what* named.
    """

    def _attempt() -> subprocess.CompletedProcess[str]:
        ssh_cmd = [*ssh_argv("ssh"), ssh_target, remote_cmd]
        try:
            return run_capture_bounded(ssh_cmd, timeout_sec=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{what} on {ssh_target} timed out after {timeout}s") from exc

    return run_with_named_pipe_retry(_attempt)


def _tar_ssh_push(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: str | Path,
    exclude: list[str],
    delete: bool = False,
    timeout: float | None,
    total_bytes: int = 0,
    only_paths: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Push *local_path* to *remote_path* via ``tar c | ssh tar x``.

    *total_bytes* is the payload estimate (from :func:`_disclose_payload`) used
    as the progress-heartbeat denominator (queue item 10); 0 means "unknown"
    and the heartbeat prints ``0%``.

    *only_paths* (queue item 6b — the content-hash delta) restricts the archive
    to an EXACT set of POSIX relpaths under *local_path* instead of the whole
    tree (``.``). It is always the additive (``delete=False``) extract — a delta
    never prunes the remote — so callers pass it together with ``delete=False``.
    The paths are already the exclude-filtered delta set, so no ``--exclude`` is
    applied on top of them. ``None`` (the default) archives the whole tree as
    before.

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
    The pre-clean runs as its OWN bounded ssh call ahead of the extract
    (see :func:`_remote_preclean`) so it can't eat the transfer budget
    (#173); the extract is then a clean ``mkdir -p && tar x``.
    """
    src_dir = str(local_path).rstrip("/\\")

    # tar excludes mirror rsync's pattern shape (relative paths under src) —
    # with the F-I dialect translation (run-#10, live): an ANCHORED caller
    # pattern (leading ``./``) means "top level only", but GNU tar and bsdtar
    # (the native-Windows tar) anchor differently: GNU honors ``./name``,
    # bsdtar treats it as match-any-component and needs the undocumented
    # ``^name`` form. Emit BOTH — each dialect ignores the other's spelling
    # (it matches no component literally), so the union is exact on both.
    # Bare patterns keep their match-any-depth meaning unchanged.
    tar_excludes: list[str] = []
    for pattern in exclude:
        pat = pattern.rstrip("/")
        if pat.startswith("./"):
            tar_excludes += [f"--exclude={pat}", f"--exclude=^{pat[2:]}"]
        else:
            tar_excludes += [f"--exclude={pat}"]

    # Delta mode (only_paths): archive exactly the given relpaths, no excludes
    # (the list is already the exclude-filtered delta). Otherwise archive the
    # whole tree with the exclude flags.
    names_file_path: str | None = None
    if only_paths is not None:
        # Windows caps a process command line at ~32k chars, so a large delta
        # as per-path ARGUMENTS dies with WinError 206 exactly when this
        # fallback IS the native-Windows live path (run-#12 finding 17).
        # Stream the member list through a temp file instead — GNU tar and
        # bsdtar both accept ``-T <file>``. Each name rides ``./``-prefixed:
        # members extract identically, and a literal name can never collide
        # with bsdtar's special ``@archive`` -T syntax.
        fd, names_file_path = tempfile.mkstemp(prefix="hpc-tar-names-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for rel in only_paths:
                fh.write(f"./{rel}\n")
        tar_cmd = ["tar", "c", "-C", src_dir, "-T", names_file_path]
    else:
        tar_cmd = ["tar", "c", *tar_excludes, "-C", src_dir, "."]
    quoted_remote = shlex.quote(remote_path)

    # delete=True: STAGE-THEN-SWAP (run-#10 finding F-G rewrote #173's order).
    # The old sequence pre-cleaned the live tree and THEN transferred — so a
    # transfer that timed out mid-flight left the remote gutted (data/ emptied,
    # src/ partial). New sequence: extract into a sibling STAGING dir (a failed
    # or timed-out transfer leaves the live tree untouched), and only after a
    # fully successful extract run the bounded clean + a merge-copy swap
    # (seconds, not transfer-length — the destructive window collapses).
    # delete=False keeps the direct overwrite extract (never destructive).
    stage_path = remote_path.rstrip("/") + ".hpc_stage"
    quoted_stage = shlex.quote(stage_path)
    small_timeout = None if timeout is None else min(PRECLEAN_TIMEOUT_SEC, timeout)
    if delete:
        # Drop any stale staging dir from a previously interrupted push.
        pre = _ssh_bounded(
            ssh_target,
            f"rm -rf {quoted_stage}",
            timeout=small_timeout,
            what=f"stage-dir drop ({stage_path})",
        )
        if pre.returncode != 0:
            return pre
        ssh_remote_cmd = f"mkdir -p {quoted_stage} && tar x -C {quoted_stage}"
    else:
        # Extract: ``mkdir -p`` (idempotent) + ``tar x``, fed by tar's stdout
        # over the pipe into ssh's stdin.
        ssh_remote_cmd = f"mkdir -p {quoted_remote} && tar x -C {quoted_remote}"

    def _attempt() -> subprocess.CompletedProcess[str]:
        # Rebuild ssh_cmd each attempt: a named-pipe-failure retry needs to
        # pick up the updated _ssh_multiplex_opts() after
        # mark_named_pipe_broken(). The tar half is rebuilt too because
        # subprocess.Popen consumes its arg list — but tar_cmd doesn't
        # depend on multiplex opts, so this is just rerunning the same
        # command.
        ssh_cmd = [*ssh_argv("ssh"), ssh_target, ssh_remote_cmd]

        # tar's stderr goes to a temp file rather than a PIPE: it is only
        # read after ``ssh`` exits, and a PIPE that fills its ~64 KB kernel
        # buffer (e.g. many "file changed as we read it" warnings on a
        # large tree) would block ``tar`` and deadlock the whole push.
        tar_stderr_file = tempfile.TemporaryFile()  # noqa: SIM115 - closed in finally below
        tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=tar_stderr_file)
        # Byte-counting pump between tar and ssh (queue item 10): tar writes into
        # ``pump_w``, a thread copies it to ``pump_r`` (ssh's stdin) chunk by
        # chunk emitting a ~15s heartbeat. ssh reads ``pump_r`` exactly as it
        # read tar's stdout before, so backpressure/binary-safety are unchanged;
        # the pump thread runs concurrently with the blocking run_capture_bounded.
        pump_r, pump_w = os.pipe()
        pump_error: list[BaseException] = []

        def _pump() -> None:
            try:
                assert tar_proc.stdout is not None
                _pump_with_progress(tar_proc.stdout, pump_w, total_bytes=total_bytes)
            except BaseException as exc:  # noqa: BLE001 — surfaced via pump_error
                pump_error.append(exc)
                with contextlib.suppress(OSError):
                    os.close(pump_w)

        pump_thread = threading.Thread(target=_pump, daemon=True)
        pump_thread.start()

        pump_r_open = True

        def _close_pump_r() -> None:
            # Idempotent close of OUR copy of the pipe read end, with a sentinel
            # so a second call (the finally) is a no-op instead of closing a
            # possibly-reused fd.
            nonlocal pump_r_open
            if pump_r_open:
                pump_r_open = False
                with contextlib.suppress(OSError):
                    os.close(pump_r)

        try:
            assert tar_proc.stdout is not None
            ssh_proc = run_capture_bounded(ssh_cmd, timeout_sec=timeout, stdin=pump_r)
            # ssh has EXITED — rc 0, or non-zero (auth refused under
            # BatchMode=yes, host unreachable, remote `mkdir && tar x` failed on
            # permissions/disk). Close our read end NOW, BEFORE joining the pump.
            # ssh dup'd its own copy of pump_r (Popen does not close the parent
            # copy), so while a multi-GB tar is still pumping, the parent's open
            # pump_r keeps the pipe from ever EPIPE-ing and an unbounded
            # pump_thread.join() would wedge forever past every transport
            # deadline (#9). With the last reader gone, the pump's next os.write
            # raises BrokenPipeError, which _pump catches into pump_error and
            # closes pump_w, so the join below completes promptly.
            _close_pump_r()
            # Defense-in-depth: bound the join (never unbounded) and, if the pump
            # somehow still hasn't observed the broken pipe, kill tar so its
            # stdout EOFs — mirroring the TimeoutExpired branch below.
            join_timeout = 30.0 if timeout is None else timeout
            pump_thread.join(timeout=join_timeout)
            if pump_thread.is_alive():
                tar_proc.kill()
                pump_thread.join(timeout=5)
            tar_proc.stdout.close()
            tar_proc.wait(timeout=timeout)
            tar_stderr_file.seek(0)
            tar_stderr_bytes = tar_stderr_file.read()
        except subprocess.TimeoutExpired as exc:
            tar_proc.kill()
            # Killing tar EOFs the pump's source so the pump thread unwinds and
            # closes pump_w; join it (bounded) then reap tar and close pipes —
            # otherwise the pump thread, pipe FDs and the zombie leak on this
            # timeout path (the happy path closes/waits, this one did not).
            pump_thread.join(timeout=5)
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
            # Close our copy of the read end (ssh dup'd its own); the pump owns
            # and closes the write end. Idempotent — the happy path already
            # closed it right after ssh exited (#9).
            _close_pump_r()
            tar_stderr_file.close()

        tar_stderr = tar_stderr_bytes.decode(errors="replace")
        combined_stderr = "\n".join(filter(None, [tar_stderr.strip(), ssh_proc.stderr.strip()]))
        # Exit-code check is unchanged (ssh wins, else tar). A pump-side failure
        # (e.g. ssh died mid-stream -> BrokenPipeError on the write) truncates the
        # byte stream; ssh/tar normally then exit non-zero on their own, but if
        # BOTH somehow reported 0 we must NOT report success on a truncated
        # transfer — fold the pump error into the returncode + stderr so the
        # caller's existing non-zero branch fires, without changing the contract
        # (still a CompletedProcess, never a new raise).
        rc = ssh_proc.returncode if ssh_proc.returncode != 0 else tar_proc.returncode
        if pump_error and rc == 0:
            rc = 1
            combined_stderr = "\n".join(
                filter(None, [combined_stderr, f"transfer pump error: {pump_error[0]!r}"])
            )

        if rc != 0:
            # ssh/tar died non-zero (auth refused, host unreachable, a severed
            # child): flush the combined stderr tail to the log at death, so the
            # story is on the tail-able surface (run-#13 finding 2).
            disclose_child_failure(what="tar|ssh push", returncode=rc, stderr=combined_stderr)

        return subprocess.CompletedProcess(
            args=tar_cmd + ["|"] + ssh_cmd,
            returncode=rc,
            stdout=ssh_proc.stdout,
            stderr=combined_stderr,
        )

    # Auto-fallback on the syscall-layer named-pipe ControlMaster failure
    # mode (Windows OpenSSH version probe can't catch it; 2026-06-04). The
    # combined_stderr we return includes ssh_proc.stderr, so
    # run_with_named_pipe_retry can detect the getsockname marker. The
    # retry restarts the WHOLE tar | ssh pipeline (tar can be re-spawned
    # cheaply; its inputs are filesystem paths, not stream state).
    try:
        transfer = run_with_named_pipe_retry(_attempt)
    finally:
        # The -T names file must survive every retry's tar re-spawn; gone now.
        if names_file_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(names_file_path)
    if not delete or transfer.returncode != 0:
        return transfer

    # Stage-then-swap tail (F-G): the transfer landed COMPLETE in the staging
    # dir; only now touch the live tree. Clean (bounded, excludes protected)
    # then a merge-copy of the staged tree (see :func:`_stage_swap_cmd` for
    # why it must MERGE, not ``mv``) — seconds of exposure instead of the
    # whole transfer. Any failure here returns loud with the staging dir
    # intact on the remote (the next push drops it), never a half-cleaned
    # live tree.
    clean = _remote_preclean(
        ssh_target=ssh_target,
        remote_path=remote_path,
        exclude=exclude,
        timeout=small_timeout,
    )
    if clean.returncode != 0:
        return clean
    move = _ssh_bounded(
        ssh_target,
        _stage_swap_cmd(stage_path, remote_path),
        timeout=small_timeout,
        what=f"stage swap into {remote_path}",
    )
    if move.returncode != 0:
        return move
    return transfer


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

    ``scp -r`` copies the SOURCE DIRECTORY into the destination — it does NOT
    honor rsync's trailing-slash "contents-only" semantics — so a naive
    ``scp -r remote:.../<sub>/ local/<sub>`` lands the files one level too deep
    at ``local/<sub>/<sub>/``. That is the double-nested ``_combiner/_combiner/``
    that broke ``verify-aggregation-complete`` on Windows (where rsync is
    absent, so the pull falls through here). To match :func:`rsync_pull`'s
    layout, scp the directory into a temp staging dir (scp creates
    ``<staging>/<sub>``) and move that dir's CONTENTS into *local_dir*.
    """
    sub = remote_subdir.strip("/").rsplit("/", 1)[-1]
    # No trailing slash: scp copies the directory itself into the staging dir.
    src = f"{ssh_target}:{remote_path.rstrip('/')}/{remote_subdir.strip('/')}"
    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as staging:
        scp_cmd = [*ssh_argv("scp", extra_opts=["-r"]), src, staging]
        try:
            proc = run_capture_bounded(scp_cmd, timeout_sec=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"scp pull from {ssh_target} timed out after {timeout}s: "
                f"{_truncate(f'{src} -> {dst_path}')}"
            ) from exc
        if proc.returncode != 0:
            # A VPN-severed scp left its "lost connection" story in proc.stderr
            # that nobody recorded (run-#13 finding 2): flush it to the log now.
            disclose_child_failure(what="scp pull", returncode=proc.returncode, stderr=proc.stderr)
            return proc
        # Flatten scp's dir-copy into local_dir so the result matches rsync's
        # contents-only layout (local_dir/wave_*.json, not local_dir/<sub>/...).
        staged = Path(staging) / sub
        if staged.is_dir():
            for item in staged.iterdir():
                target = dst_path / item.name
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.move(str(item), str(target))
        return proc


def _prune_manifest_known_extras(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: str | Path,
    remote_manifest: Any,
    extra: tuple[str, ...],
    timeout: float | None,
) -> set[str]:
    """Plan + disclose + journal + execute the bounded auto-prune (ruling 6).

    Called only from the delete=True delta push (holds the dial). Fully
    fail-open: any error leaves the remote untouched — a prune we cannot do
    cleanly is a prune we skip, never a broken push.

    Returns the set of manifest-known remote extras that this push did NOT
    successfully delete (a cap-REFUSED plan, a failed/racing ``rm``, or an
    error) — the paths that are STILL ours on the remote. The caller folds them
    into the push manifest it writes (#F58): a refused or failed prune must keep
    those extras classified ``manifest-known (prunable)`` for the next push, or
    the disclosure's own ``raise the cap and re-push`` remediation can never
    work (they would silently downgrade to never-touched ANOMALYs).
    """
    if os.environ.get(_PRUNE_ENV_KILL) == "1":
        return set()
    # Provenance we must NOT lose: manifest-known extras still on the remote.
    # Populated as soon as the plan is known and cleared only on a confirmed
    # delete, so an exception at any later point still retains it.
    retained: set[str] = set()
    try:
        # Our own bookkeeping file is a remote extra (never shipped locally); it
        # is neither ours-to-prune nor an anomaly — filter it out up front.
        # Same for deploy_runtime's own placed files (:func:`_is_runtime_placed`).
        candidates = [
            p
            for p in extra
            if p not in (_PUSH_MANIFEST_REL, _PUSH_MANIFEST_TMP_REL) and not _is_runtime_placed(p)
        ]
        if not candidates:
            return set()

        from hpc_agent.infra.manifest import FileEntry
        from hpc_agent.infra.prune import plan_prune

        by_path = {e.path: e for e in remote_manifest.entries}
        extra_entries = [by_path.get(p) or FileEntry(path=p, size=0, sha256="") for p in candidates]
        known = _read_prior_push_manifest(
            ssh_target=ssh_target, remote_path=remote_path, timeout=timeout
        )
        plan = plan_prune(
            extra_entries,
            known,
            max_files=_prune_max_files(),
            max_bytes=_prune_max_bytes(),
        )
        # Until proven deleted, every manifest-known extra keeps its provenance.
        retained = {e.path for e in plan.prunable}
        _disclose_prune(plan, remote_path=remote_path)

        if plan.refused:
            _journal_deploy_prune(
                local_path,
                {
                    "action": "prune-refused",
                    "remote_path": remote_path,
                    "reason": plan.refuse_reason,
                    "manifest_known_count": len(plan.prunable),
                    "manifest_known_bytes": plan.prune_bytes,
                },
            )
            return retained
        if not plan.to_prune:
            return retained

        # Journal BEFORE deleting: record what + why + the old remote sha, so the
        # timeline survives even if the delete itself races or fails.
        for entry in plan.prunable:
            _journal_deploy_prune(
                local_path,
                {
                    "action": "prune",
                    "remote_path": remote_path,
                    "path": entry.path,
                    "reason": "manifest-known",
                    "old_sha256": entry.sha256,
                    "size": entry.size,
                },
            )
        if _execute_prune(
            ssh_target=ssh_target,
            remote_path=remote_path,
            paths=list(plan.to_prune),
            timeout=timeout,
        ):
            # Confirmed gone from the remote — drop them from the manifest.
            retained = set()
        return retained
    except Exception:  # noqa: BLE001 — the prune is never load-bearing on a push
        return retained


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
    # Per-host connection-rate guard (ban-driver): paces this push's connection
    # open(s) so back-to-back transfers can't burst past a cluster rate-limiter.
    # No-op unless HPC_SSH_SAFE_INTERVAL>0. See infra.ssh_throttle.
    throttle_connection(ssh_target)
    exclude = _effective_excludes(exclude)
    # The local push-delta hash cache (run-13 finding 6) is a stack-internal file
    # under ``.hpc/``; union it into the exclude set so no transfer path (delta
    # local manifest, remote hash snippet, full-copy tar, payload disclosure)
    # ever hashes, ships, or prunes it — the same standing as
    # ``.hpc/.push_manifest.json`` (which ``_effective_excludes`` already unions).
    if _PUSH_HASH_CACHE_REL not in exclude:
        exclude = [*exclude, _PUSH_HASH_CACHE_REL]
    payload_bytes = _disclose_payload(local_path, exclude)
    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout

    # Validate the remote path up front so push and pull share one
    # rule. After validation the value flows verbatim through the
    # remote shell that rsync invokes — same posture as the rest of
    # the module.
    validate_remote_path(remote_path.rstrip("/"))

    if not _have_rsync():
        # Content-hash DELTA on rsync-less hosts (queue item 6b). The tar
        # fallback has no delta, so it re-ships the whole tree even when >95% is
        # already remote (the run-#11 8.4 GB re-ship to CARC over a ~1 MB/s VPN).
        # Instead, when the deployed runtime can hash its own tree (one bounded
        # ssh round-trip), diff the two content manifests and tar ONLY the
        # changed/new files. Gated to the ``delete=True`` user-tree push (the big
        # transfer); the additive ``delete=False`` callers keep the simple path.
        # Kill-switch: HPC_NO_DEPLOY_DELTA=1.
        delta_on = delete and os.environ.get(_DELTA_ENV_KILL) != "1"
        remote_manifest = (
            _remote_push_manifest(
                ssh_target=ssh_target,
                remote_path=remote_path,
                exclude=exclude,
                timeout=effective_timeout,
            )
            if delta_on
            else None
        )
        if remote_manifest is not None:
            from hpc_agent.infra.manifest import manifest_delta

            local_manifest = _local_push_manifest(local_path, exclude)
            delta = manifest_delta(local_manifest, remote_manifest)
            ship = list(delta.to_ship)
            sizes = {e.path: e.size for e in local_manifest.entries}
            shipped_bytes = sum(sizes.get(p, 0) for p in ship)
            _disclose_delta_mode(
                shipped_bytes=shipped_bytes,
                total_bytes=payload_bytes,
                n_ship=len(ship),
                n_local=len(local_manifest.entries),
                n_reused=len(local_manifest.entries) - len(ship),
            )
            # Ship the changed/new files (the delta is content-additive — the tar
            # extract runs delete=False and never prunes). ``ship`` may be empty
            # when the remote is already content-identical.
            #
            # Incremental manifest checkpointing (run-13 finding 3): ship the
            # delta in BOUNDED BATCHES instead of one monolithic tar, and
            # checkpoint the push manifest after each batch lands. A single tar
            # that dies mid-stream leaves a truncated archive whose bookkeeping
            # never commits, so a retry re-pays the WHOLE delta (attempt 1 shipped
            # 355 MB of 1181 MB, then re-shipped all 1181 MB). Batching makes each
            # landed batch DURABLE and independently confirmed: a retry's delta —
            # computed from the live remote hash — re-derives the landed files and
            # ships only the remainder, and the per-batch manifest checkpoint keeps
            # the prune bookkeeping honest even if a LATER batch dies.
            if ship:
                ship_set = set(ship)
                # Files already content-identical on the remote (reused, never
                # shipped) are the base of every checkpoint: a checkpoint records
                # ONLY files confirmed on the remote — the reused set plus the
                # batches that have returned success so far.
                base_paths = [e.path for e in local_manifest.entries if e.path not in ship_set]
                max_files, max_bytes = _delta_batch_caps()
                batches = list(
                    _delta_ship_batches(ship, sizes, max_files=max_files, max_bytes=max_bytes)
                )
                landed: list[str] = []
                pushed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
                for i, batch in enumerate(batches, start=1):
                    batch_bytes = sum(sizes.get(p, 0) for p in batch)
                    _disclose_delta_batch(
                        index=i, total=len(batches), n_files=len(batch), batch_bytes=batch_bytes
                    )
                    pushed = guarded_call(
                        ssh_target,
                        functools.partial(
                            _tar_ssh_push,
                            ssh_target=ssh_target,
                            remote_path=remote_path,
                            local_path=local_path,
                            exclude=exclude,
                            delete=False,
                            timeout=effective_timeout,
                            total_bytes=batch_bytes,
                            only_paths=batch,
                        ),
                    )
                    if pushed.returncode != 0:
                        # This batch did not land. Earlier batches DID (each tar|ssh
                        # completed and was checkpointed), so a retry's delta reflects
                        # them and re-ships only the remainder. Leave the remote as-is
                        # (no prune, no final seal) so the next push retries cleanly.
                        return pushed
                    landed.extend(batch)
                    # Checkpoint the push manifest after each batch EXCEPT the last
                    # (the final seal below covers the last batch and folds in the
                    # prune's retained extras). Crash-safe: ``_write_push_manifest``
                    # writes a remote temp then atomically ``mv``-s it into place, so
                    # a torn checkpoint can never corrupt the live manifest.
                    if i < len(batches):
                        _write_push_manifest(
                            ssh_target=ssh_target,
                            remote_path=remote_path,
                            paths=sorted(set(base_paths) | set(landed)),
                            timeout=effective_timeout,
                        )
            else:
                pushed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

            # Bounded auto-prune of MANIFEST-KNOWN remote extras (ruling 6): the
            # delta tar cannot prune, but a file WE shipped in a prior push and
            # since dropped is safe to delete under a disclosed cap. Rides this
            # same delete=True dial (no new cold SSH); anomalies are never
            # touched. Then persist the push manifest so the NEXT push knows what
            # is ours. Both fail-open — neither can break a successful transfer.
            retained_extras = _prune_manifest_known_extras(
                ssh_target=ssh_target,
                remote_path=remote_path,
                local_path=local_path,
                remote_manifest=remote_manifest,
                extra=delta.extra,
                timeout=effective_timeout,
            )
            # Write the manifest as the UNION of the current local paths AND any
            # manifest-known extras this push did NOT delete (#F58): a cap-refused
            # or failed prune must keep its provenance, or those remote extras
            # silently downgrade to never-touched ANOMALYs on the very next push
            # and the "raise the cap and re-push" remediation can never fire.
            _write_push_manifest(
                ssh_target=ssh_target,
                remote_path=remote_path,
                paths=sorted({e.path for e in local_manifest.entries} | retained_extras),
                timeout=effective_timeout,
            )
            return pushed

        # Full-copy fallback: no remote manifest (first deploy / pre-delta
        # runtime), delta disabled, or an additive push. Name the NO-DELTA cost
        # and WHY before the bytes move (queue item 6a).
        if not delete:
            reason = "additive push (delete=False)"
        elif os.environ.get(_DELTA_ENV_KILL) == "1":
            reason = f"delta disabled via {_DELTA_ENV_KILL}=1"
        else:
            reason = "remote content-hash manifest unavailable (first deploy or pre-delta runtime)"
        _disclose_no_rsync(payload_bytes, reason=reason)
        # The tar|ssh fallback returns before the _with_ssh_backoff wrap
        # below, so it must consult the per-host circuit breaker itself —
        # on native Windows (no rsync) this IS the live push path.
        return guarded_call(
            ssh_target,
            lambda: _tar_ssh_push(
                ssh_target=ssh_target,
                remote_path=remote_path,
                local_path=local_path,
                exclude=exclude,
                delete=delete,
                timeout=effective_timeout,
                total_bytes=payload_bytes,
            ),
        )

    exclude_flags: list[str] = []
    for pattern in exclude:
        exclude_flags += ["--exclude", pattern]

    src = _msys_local(str(local_path).rstrip("/\\") + "/")
    dst = f"{ssh_target}:{remote_path.rstrip('/')}/"

    flags = ["rsync", "-az"]
    if delete:
        flags.append("--delete")

    def _attempt() -> subprocess.CompletedProcess[str]:
        # Rebuild env each attempt: ssh_env() is re-resolved after a
        # mark_named_pipe_broken() trigger. Important nuance:
        # _rsync_rsh_env() (the source of RSYNC_RSH) uses
        # _ssh_config_override_opts() — which is already
        # ControlMaster=no / ControlPath=none on Windows — NOT
        # _ssh_multiplex_opts(), so RSYNC_RSH itself is byte-identical
        # before and after the verdict flip. The wrapper is still worth
        # running here for two reasons: (a) it catches the
        # `getsockname failed: Not a socket` marker if it ever surfaces
        # in rsync/ssh stderr; (b) marking the verdict early so any
        # subsequent ssh_run call demotes to legacy ControlMaster=no on
        # its FIRST attempt rather than racing into the same broken
        # master state that brought us here.
        rsync_env = {**os.environ, **ssh_env()}
        try:
            return run_capture_bounded(
                [*flags, *exclude_flags, src, dst],
                timeout_sec=effective_timeout,
                env=rsync_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync push to {ssh_target} timed out after {effective_timeout}s: "
                f"{_truncate(f'{src} -> {dst}')}"
            ) from exc

    def _run() -> subprocess.CompletedProcess[str]:
        # Auto-fallback on the syscall-layer named-pipe ControlMaster failure
        # mode (Windows OpenSSH version probe can't catch it; 2026-06-04).
        return run_with_named_pipe_retry(_attempt)

    return _with_ssh_backoff(_run, label=f"rsync push {ssh_target}", ssh_target=ssh_target)


def _rsync_deploy(*, ssh_target: str, remote_path: str, staging: Path) -> None:
    """rsync the staged deploy tree to the cluster — one invocation, delta only.

    ``-az`` ships only the changed *files* (the cache already narrowed the set)
    with rsync's own delta over the wire. Deliberately NOT ``--inplace`` (#F20):
    ``--inplace`` rewrites the destination file's bytes in place, so an array
    task of an UNRELATED in-flight run in the same ``remote_path`` that execs
    ``.hpc/_hpc_dispatch.py`` (or sources a preamble) mid-transfer reads a torn
    file → ``SyntaxError`` → the retry wrapper stamps a terminal
    ``.hpc_failed`` marker on a task that was healthy. rsync's default
    temp-file-plus-atomic-rename means a concurrent reader sees either the
    complete old file or the complete new one, never a half-written one; the
    delta economy is marginal for the sub-100 KB python files this ships. NO
    ``--delete``: deploy merges its subset into the cluster tree and must never
    remove the user's run output or sibling framework files. rsync invokes its
    own ssh, so :func:`ssh_env` pins the binary + crypto/multiplex opts,
    mirroring :func:`rsync_push`.
    """
    # Per-host connection-rate guard (ban-driver); no-op unless
    # HPC_SSH_SAFE_INTERVAL>0. See infra.ssh_throttle.
    throttle_connection(ssh_target)
    src = _msys_local(str(staging).rstrip("/\\") + "/")
    dst = f"{ssh_target}:{remote_path.rstrip('/')}/"
    rsync_env = {**os.environ, **ssh_env()}

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return run_capture_bounded(
                ["rsync", "-az", src, dst],
                timeout_sec=SSH_TIMEOUT_SEC,
                env=rsync_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync deploy to {ssh_target} timed out after {SSH_TIMEOUT_SEC}s"
            ) from exc

    result = _with_ssh_backoff(_run, label=f"rsync deploy {ssh_target}", ssh_target=ssh_target)
    if result.returncode != 0:
        raise RuntimeError(
            f"rsync deploy to {ssh_target} failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()[:300]}"
        )


def _deploy_transfer(*, ssh_target: str, remote_path: str, items: list[_DeployItem]) -> None:
    """Ship *items* to ``{remote_path}`` in a single batched transfer (#252).

    Stages each item at ``staging/<dst_rel>`` (a verbatim package file is
    copied, rendered ``content`` is written), then transfers the whole staging
    tree in ONE invocation: an ``rsync -az`` delta where rsync is on PATH (no
    ``--inplace`` — #F20: an in-place rewrite tears the live dispatcher under a
    concurrent array), else a single ``tar c | ssh tar x`` stream
    (``delete=False`` — merge, never remove). Same transport detection
    :func:`rsync_push` uses.
    *remote_path* is validated up front so it can flow verbatim into the rsync
    target / remote shell, matching the rest of the module.
    """
    validate_remote_path(remote_path.rstrip("/"))
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        for it in items:
            dst = staging / it.dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if it.src_path is not None:
                shutil.copyfile(it.src_path, dst)
            else:
                dst.write_text(it.content or "", encoding="utf-8", newline="")
        if _have_rsync():
            _rsync_deploy(ssh_target=ssh_target, remote_path=remote_path, staging=staging)
            return
        result = _tar_ssh_push(
            ssh_target=ssh_target,
            remote_path=remote_path,
            local_path=staging,
            exclude=[],
            delete=False,
            timeout=SSH_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"tar/ssh deploy to {ssh_target} failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip()[:300]}"
            )


def _write_deploy_manifest(*, ssh_target: str, remote_path: str, content: str) -> None:
    """Persist the deploy-cache manifest at :data:`_DEPLOY_MANIFEST_REL` in its
    OWN ssh leg, run ONLY after the file transfer has succeeded (#F53).

    The manifest must never ride the batched file transfer: on the rsync leg it
    sorts ahead of ``_hpc_combiner.py`` / ``_hpc_dispatch.py`` / ``templates``
    (``.`` < ``_`` < ``t``) and the tar leg extracts in archive order, so it
    lands FIRST on the wire. An interrupted transfer that delivered the manifest
    but not the code it attests would leave the user's natural retry reading a
    manifest whose shas all "match" — shipping nothing and reporting success
    over stale-or-torn framework code, exactly the version-skew the pkg_version
    cache key exists to prevent. Writing it here, after :func:`_deploy_transfer`
    returned (it raises on any transfer failure), closes that window.

    Base64-piped so the JSON needs no shell quoting (mirrors
    :func:`_write_push_manifest`). Fail-open: a lost manifest write only forces
    the NEXT deploy to re-ship as a full cache miss — never a stale manifest
    attesting a file that did not land.
    """
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    dst = shlex.quote(_DEPLOY_MANIFEST_REL)
    cmd = f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(b64)} | base64 -d > {dst}"
    with contextlib.suppress(TimeoutError, OSError):
        _ssh_bounded(
            ssh_target,
            cmd,
            timeout=SSH_TIMEOUT_SEC,
            what=f"write deploy manifest of {remote_path}",
        )


def deploy_runtime(
    *,
    ssh_target: str,
    remote_path: str,
    scheduler: str | None = None,
    use_cache: bool | None = None,
) -> None:
    """Deploy framework runtime files to the cluster.

    Two payloads:

    1. **Importable stubs** in ``{remote_path}/hpc_agent/execution/mapreduce/``:
       ``metrics_io.py`` so user executors can do
       ``from hpc_agent.execution.mapreduce.metrics_io import write_metrics`` on
       compute nodes without installing the full package.
    2. **Framework artifacts** in ``{remote_path}/.hpc/``: the framework
       executor (``_hpc_dispatch.py``), the combiner
       (``_hpc_combiner.py``), and the four job templates under
       ``templates/``. The cluster-side ``.hpc/`` mirrors the experiment's
       local ``.hpc/`` directory layout — ``tasks.py`` and
       ``runs/<id>.json`` come over via :func:`rsync_push`; the framework
       files are placed here by scp.

    The (cache-filtered) files ship in a **single batched transfer** (#252):
    an ``rsync -az`` delta where rsync is on PATH — so only the *changed files*
    cross the wire (rsync still deltas the bytes over the wire), which matters
    for the framework artifacts that grow over time (combiner.py, dispatch.py,
    the templates). It is deliberately NOT ``--inplace`` (#F20): an in-place
    rewrite tears a live ``.hpc/_hpc_dispatch.py`` under a concurrent in-flight
    array; rsync's default temp-then-rename replaces the file atomically.
    Falls back to one ``tar c | ssh tar x`` stream on hosts
    without rsync (native Windows). This is the same transport detection
    :func:`rsync_push` uses, and replaces the prior N-scp fan-out (#245): a
    re-deploy is now at most one prelude ssh + one transfer. The transfer is
    bounded by :data:`SSH_TIMEOUT_SEC`; a timeout raises :class:`TimeoutError`
    and a non-zero transfer raises :class:`RuntimeError`.

    A **content-hash cache** (#242) skips files already present unchanged: the
    cluster-side manifest at :data:`_DEPLOY_MANIFEST_REL` records each file's
    sha256 and the producing package version; a file is re-shipped only when
    its sha differs OR the package version moved. ``use_cache=False`` (or
    ``HPC_NO_DEPLOY_CACHE=1``) forces a full deploy and skips the manifest
    entirely; any unreadable/corrupt manifest also falls back to a full
    deploy.

    Must be called **after** :func:`rsync_push` (which uses ``--delete``).
    The default rsync excludes preserve cluster-side framework files
    inside ``.hpc/``, but deploy_runtime is still safe to re-run after
    every push (it overwrites with the package-versioned bytes).
    """
    if use_cache is None:
        use_cache = os.environ.get("HPC_NO_DEPLOY_CACHE") != "1"

    remote_path_q = shlex.quote(remote_path)
    manifest_q = shlex.quote(f"{remote_path}/{_DEPLOY_MANIFEST_REL}")

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
    mkdir_cmd = (
        f"mkdir -p {remote_path_q}/hpc_agent/execution/mapreduce/reduce"
        f" {remote_path_q}/hpc_agent/_kernel/contract"
        f" {remote_path_q}/hpc_agent/infra"
        f" {remote_path_q}/.hpc/templates"
        f" {remote_path_q}/.hpc/templates/common"
        # Strip any ``__init__.py`` a pre-fix deploy may have left so the
        # deployed tree stays a PEP 420 namespace package end-to-end (#349
        # ships reporter modules under reduce/, _kernel/contract/, infra/).
        f" && find {remote_path_q}/hpc_agent -name '__init__.py' -delete"
        f" && rm -f {remote_path_q}/hpc_agent/__init__.py"
        f" {remote_path_q}/hpc_agent/execution/__init__.py"
        f" {remote_path_q}/hpc_agent/execution/mapreduce/__init__.py"
        # Purge stale compiled artifacts in the deployed tree. A Py2.7
        # ``__init__.pyc`` left *beside* the (now-absent) ``__init__.py`` is
        # imported directly by Py3 as the package init -> ``bad magic
        # number``, shadowing the conda install and killing every
        # cluster-side verb. rsync ``--delete`` excludes ``hpc_agent/`` (see
        # DEFAULT_RSYNC_EXCLUDES) so nothing else ever cleans this dir; the
        # ``.py`` removal above doesn't touch ``.pyc`` / ``__pycache__``.
        f" && find {remote_path_q}/hpc_agent -name '*.pyc' -delete"
        f" && find {remote_path_q}/hpc_agent -depth -type d -name __pycache__"
        f" -exec rm -rf {{}} +"
    )
    # Fold the cache-manifest read into the prelude ssh so it costs no extra
    # round-trip: the mkdir/rm/find chain prints nothing to stdout, so the
    # trailing ``cat`` (absent file -> empty, never an error) leaves the
    # manifest JSON as the call's entire stdout. ``;`` not ``&&`` so a manifest
    # read is independent of the prep chain.
    if use_cache:
        mkdir_cmd += f" ; cat {manifest_q} 2>/dev/null || true"
    prelude = ssh_run(mkdir_cmd, ssh_target=ssh_target)

    remote_manifest = _parse_remote_manifest(getattr(prelude, "stdout", "")) if use_cache else None

    items = _build_deploy_items(scheduler=scheduler)
    new_manifest = {
        "pkg_version": _pkg_version(),
        "files": {it.dst_rel: it.sha for it in items},
    }

    # Skip a file only when its content sha matches AND the recorded package
    # version matches — a version bump re-ships everything (issue mitigation a),
    # since the framework artifacts are package-versioned.
    if (
        use_cache
        and remote_manifest is not None
        and remote_manifest.get("pkg_version") == new_manifest["pkg_version"]
    ):
        cached_files = remote_manifest.get("files", {})
        to_deploy = [it for it in items if cached_files.get(it.dst_rel) != it.sha]
    else:
        to_deploy = list(items)

    # Ship the code FIRST, then record the manifest in a SEPARATE ssh leg after
    # the transfer succeeded (#F53). The manifest must NOT ride the batched
    # transfer: it sorts/extracts ahead of the files it attests, so an
    # interrupted deploy could land the manifest over un-shipped code and make
    # the retry a false success. ``_deploy_transfer`` raises on any transfer
    # failure, so the manifest write below is reached only on a fully landed
    # transfer; the write is itself fail-open (a lost manifest just forces the
    # next deploy to re-ship as a full cache miss — never a false attestation).
    manifest_changed = use_cache and remote_manifest != new_manifest
    if to_deploy:
        _deploy_transfer(ssh_target=ssh_target, remote_path=remote_path, items=to_deploy)
    if manifest_changed:
        _write_deploy_manifest(
            ssh_target=ssh_target,
            remote_path=remote_path,
            content=json.dumps(new_manifest, indent=2, sort_keys=True),
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
    # Per-host connection-rate guard (ban-driver); no-op unless
    # HPC_SSH_SAFE_INTERVAL>0. See infra.ssh_throttle.
    throttle_connection(ssh_target)
    validate_remote_path(remote_path.rstrip("/"))
    if remote_subdir.strip("/"):
        validate_remote_path(remote_subdir.strip("/"))
    src = f"{ssh_target}:{remote_path.rstrip('/')}/{remote_subdir.strip('/')}/"

    dst_path = Path(local_dir)
    dst_path.mkdir(parents=True, exist_ok=True)
    dst = _msys_local(str(dst_path).rstrip("/\\") + "/")

    effective_timeout: float | None = RSYNC_TIMEOUT_SEC if timeout is _DEFAULT else timeout

    if not _have_rsync():
        # Early return bypasses the _with_ssh_backoff wrap below, so route
        # the scp fallback through the circuit breaker directly (the live
        # pull path on native Windows, where rsync is absent).
        return guarded_call(
            ssh_target,
            lambda: _scp_pull(
                ssh_target=ssh_target,
                remote_path=remote_path,
                remote_subdir=remote_subdir,
                local_dir=local_dir,
                timeout=effective_timeout,
            ),
        )

    filter_flags: list[str] = []
    if include is not None:
        filter_flags += ["--include=*/"]
        for pattern in include:
            filter_flags += [f"--include={pattern}"]
        filter_flags += ["--exclude=*"]

    rsync_env = {**os.environ, **ssh_env()}

    def _run() -> subprocess.CompletedProcess[str]:
        try:
            return run_capture_bounded(
                ["rsync", "-az", *filter_flags, src, dst],
                timeout_sec=effective_timeout,
                env=rsync_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"rsync pull from {ssh_target} timed out after {effective_timeout}s: "
                f"{_truncate(f'{src} -> {dst}')}"
            ) from exc

    return _with_ssh_backoff(_run, label=f"rsync pull {ssh_target}", ssh_target=ssh_target)
