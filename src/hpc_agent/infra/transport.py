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

import base64
import contextlib
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Final

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

__all__ = [
    "DEFAULT_RSYNC_EXCLUDES",
    "MANDATORY_RSYNC_EXCLUDES",
    "PROTECTED_OUTPUT_DIRS",
    "PROTECTED_RUNTIME_FILES",
    "deploy_runtime",
    "rsync_pull",
    "rsync_push",
    "run_combiner",
    "run_combiner_checked",
    "run_final_reduce",
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

# Cluster-side RUN OUTPUT directories — written by the job on the compute
# nodes, NOT part of the local deploy tree. A deploy push's ``--delete``
# (rsync) or tar-fallback remote pre-clean must NEVER delete or even traverse
# these (#173): deleting them destroys the user's results, and traversing a
# crash-loop's debris (10^5+ ``_wip_*`` dirs under ``results/``) wedges the push
# past its transfer timeout. Unioned into every push's exclude set (like
# :data:`MANDATORY_RSYNC_EXCLUDES`) so an incomplete caller ``exclude`` can't
# expose them. ``result_dir_template`` defaults to ``results/``; ``_combiner/``
# holds the wave-combiner output. A non-default output dir must be added to the
# caller's ``exclude``. Bare names (trailing slash documents "directory") so
# rsync/tar/find match the dir at any depth.
PROTECTED_OUTPUT_DIRS: list[str] = [
    "results/",
    "_combiner/",
    # The scheduler's per-task stdout/stderr dir (qsub/sbatch ``-o <remote>/logs``,
    # default name ``logs``). Written by the job on the compute nodes, NOT part of
    # the local deploy tree — so a re-submit's ``--delete`` pre-clean would wipe it,
    # and the scheduler then recreates ``logs`` as a *file* (its ``-o`` target with
    # no directory present), losing per-task log separation. Protect it like the
    # other run-output dirs. Empirical 2026-06-09 demo: a re-deploy left ``logs`` a
    # 24KB file instead of a dir of ``*.o<job>.<task>`` entries.
    "logs/",
]

# Framework runtime files placed on the cluster by ``deploy_runtime`` (scp'd
# into ``.hpc/`` separately from the user-repo push): the per-scheduler job
# scripts + shared preamble under ``.hpc/templates/``, the dispatcher, the
# combiner, and the ``hpc_agent/`` runtime stub. The local deploy tree does NOT
# contain them, so a push's ``--delete`` / pre-clean would wipe them — and
# protecting them only via :data:`DEFAULT_RSYNC_EXCLUDES` is not enough,
# because an explicit caller ``exclude`` (the ``rsync_excludes`` spec field)
# *replaces* that default. Empirically (2026-06-08 Windows demo) a push whose
# exclude set lacked these deleted ``.hpc/templates/`` on the cluster; every
# array task then died at preamble-source time with ``hpc_preamble.sh: No such
# file or directory`` — a ~26ms exit-1 on SGE — while the canary that ran
# before the wipe passed. Unioned into every push's exclude set, exactly like
# MANDATORY / PROTECTED_OUTPUT, so no caller can drop the protection.
PROTECTED_RUNTIME_FILES: list[str] = [
    "hpc_agent/",
    ".hpc/_hpc_dispatch.py",
    ".hpc/_hpc_combiner.py",
    ".hpc/templates/",
    # deploy_runtime-placed bookkeeping (never in the local push tree): the
    # deploy-cache manifest (:data:`_DEPLOY_MANIFEST_REL`) and the push manifest
    # (:data:`_PUSH_MANIFEST_REL`). A push's ``--delete`` / tar pre-clean would
    # wipe them on every standard push-then-deploy cycle, so the #242 content-
    # hash deploy cache would ALWAYS miss (re-ship every file) and the ruling-6
    # manifest prune would lose its record of what we shipped (#66).
    ".hpc/.deploy_state.json",
    ".hpc/.push_manifest.json",
]

# The remote ``--delete`` pre-clean (tar fallback) gets its OWN timeout,
# distinct from — and shorter than — the (30-min) transfer timeout, so a
# pathological clean fails loud fast instead of silently eating the transfer
# budget and wedging the push (#173). Override via ``HPC_PRECLEAN_TIMEOUT_SEC``.
PRECLEAN_TIMEOUT_SEC: Final[int] = _env_int("HPC_PRECLEAN_TIMEOUT_SEC", 300)


def _effective_excludes(exclude: list[str] | None) -> list[str]:
    """Resolve the exclude list, always enforcing the mandatory patterns.

    ``None`` selects :data:`DEFAULT_RSYNC_EXCLUDES`. Three mandatory groups are
    then appended (de-duplicated) so a caller-supplied list can never drop
    them: :data:`MANDATORY_RSYNC_EXCLUDES` (the credential file
    ``clusters.yaml`` — never ship), :data:`PROTECTED_OUTPUT_DIRS` (cluster
    run output — never ``--delete``/pre-clean; see #173), and
    :data:`PROTECTED_RUNTIME_FILES` (the ``deploy_runtime``-placed framework
    files — never ``--delete``, or every array task loses its preamble).
    """
    base = DEFAULT_RSYNC_EXCLUDES if exclude is None else list(exclude)
    out = list(base)
    for pat in (*MANDATORY_RSYNC_EXCLUDES, *PROTECTED_OUTPUT_DIRS, *PROTECTED_RUNTIME_FILES):
        if pat not in out:
            out.append(pat)
    return out


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


#: Payload size above which the pre-push disclosure escalates to a WARN line —
#: run-#10 finding F-E: a 3.8G artifact tree rode a deploy silently into a
#: 30-minute timeout. Disclosure only (never blocking): the no-silent-caps rule.
_PAYLOAD_WARN_BYTES = 200 * 1024 * 1024
#: Walk bound so disclosure itself stays cheap on pathological trees.
_PAYLOAD_WALK_CAP = 50_000


def _path_excluded(parts: tuple[str, ...], pats: list[str]) -> bool:
    """Would the transfer's exclude set drop the file at *parts*?

    The shared exclude-match core used by both :func:`_disclose_payload` (the
    ship-size WARN) and :func:`_pushable_relpaths` (the delta manifest's local
    file set), so the disclosure, the local manifest, and the remote manifest
    snippet all agree on exactly which files ship. *pats* are patterns already
    stripped of any trailing ``/``.

    Semantics mirror tar/rsync as the codebase applies them: a bare pattern
    matches ANY path component (match-any-depth); an anchored ``./name`` /
    ``^name`` pattern (the F-I dialects) matches only the TOP-LEVEL component.
    An internal-slash pattern (e.g. ``.hpc/templates/``) matches no single
    component and is therefore inert here — the same as in the pre-existing
    disclosure walk, which keeps the local and remote manifests consistent.
    """
    for i, part in enumerate(parts):
        for pat in pats:
            if pat.startswith("./") or pat.startswith("^"):
                anchored = pat[2:] if pat.startswith("./") else pat[1:]
                if i == 0 and fnmatch.fnmatch(part, anchored):
                    return True
                continue
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _disclose_payload(local_path: str | Path, exclude: list[str]) -> int:
    """One stderr line naming what this push is about to ship (F-E).

    Approximates the transfer's own filtering: a path is skipped when ANY
    path part (or its relpath) fnmatch-es an exclude pattern — the same
    bare-name-at-any-depth semantics tar/rsync apply (the semantics whose
    misreading cost the run-#10 src/data drop; the disclosure makes them
    VISIBLE before the bytes move). Best-effort and fail-open: a disclosure
    error never blocks a push.

    Returns the total payload size in bytes (0 on any error) so the caller can
    reuse it as the transfer-progress denominator (queue item 10) without a
    second tree walk. A walk-capped total is a lower bound; the progress line's
    ``~`` prefix already reads as an estimate.
    """
    try:
        pats = [p.rstrip("/") for p in exclude]
        total = 0
        count = 0
        capped = False
        # Bare-pattern collision detector (run-#10 F-H): a bare name matches
        # at ANY depth, so excluding "data" also drops "src/data" from the
        # ship. Record every DISTINCT subtree each bare pattern hits; >1
        # subtree = the collision warning below.
        bare_hits: dict[str, set[str]] = {}
        root = Path(local_path)
        for p in root.rglob("*"):
            if count >= _PAYLOAD_WALK_CAP:
                capped = True
                break
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            excluded = _path_excluded(parts, pats)
            if excluded:
                # Record which bare pattern(s) hit which subtree, for the
                # anchor-collision WARN below (only bare names alias across
                # subtrees; anchored patterns are top-level by construction).
                for i, part in enumerate(parts):
                    for pat in pats:
                        if pat.startswith("./") or pat.startswith("^"):
                            continue
                        if fnmatch.fnmatch(part, pat) and "/" not in pat and "\\" not in pat:
                            bare_hits.setdefault(pat, set()).add("/".join(parts[: i + 1]))
                continue
            if p.is_file():
                count += 1
                with contextlib.suppress(OSError):
                    total += p.stat().st_size
        mb = total / (1024 * 1024)
        prefix = "WARN deploy payload" if total > _PAYLOAD_WARN_BYTES else "deploy payload"
        suffix = " (walk capped; true size is larger)" if capped else ""
        print(
            f"[transport] {prefix}: {count} files, {mb:.1f} MB{suffix}; "
            f"excludes: {', '.join(sorted(pats)) or '(none)'}",
            file=sys.stderr,
        )
        for pat, subtrees in sorted(bare_hits.items()):
            if len(subtrees) > 1:
                named = ", ".join(sorted(subtrees)[:4])
                print(
                    f"[transport] WARN bare exclude {pat!r} matches {len(subtrees)} "
                    f"distinct subtrees ({named}) — a bare name excludes at ANY "
                    f"depth; anchor it (e.g. './{pat}') if you meant only the "
                    "top-level one.",
                    file=sys.stderr,
                )
        return total
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        return 0


def _disclose_no_rsync(total_bytes: int, *, reason: str = "") -> None:
    """One WARN naming the tar full-copy fallback's cost (queue item 6a).

    Fired at transfer start whenever the push takes the full-copy tar path,
    alongside the :func:`_disclose_payload` WARN. The run-#11 evidence: an 8.4 GB
    tree silently re-shipped to CARC in full because no rsync was on PATH — the
    tar fallback has NO delta, so every byte crosses the wire even when the
    remote is byte-identical, and nothing said so. This makes the cause visible
    before the multi-hour transfer, in the same ``[transport]`` style as the
    payload WARN.

    *reason* (queue item 6b) names WHY the full copy ran rather than the
    content-hash delta — a first deploy, a pre-delta cluster runtime, or the
    kill-switch — so the disclosure says which mode ran and why. Fail-open
    (ASCII arrows so a cp1252 console can't raise): disclosure never blocks a
    push.
    """
    try:
        mb = total_bytes / (1024 * 1024)
        why = f" ({reason})" if reason else ""
        print(
            f"[transport] WARN no rsync on PATH -> tar full-copy fallback -> NO DELTA "
            f"-> the full {mb:.1f} MB re-ships even if the remote is identical{why} "
            f"(install rsync, or WSL/MSYS rsync on Windows, to ship only changed bytes).",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


def _disclose_delta_mode(
    *, shipped_bytes: int, total_bytes: int, n_ship: int, n_local: int, n_reused: int
) -> None:
    """One line naming the content-hash DELTA the rsync-less push took (item 6b).

    Fired when a remote hash manifest WAS available, so the tar fallback ships
    only the changed/new files instead of the whole tree (the run-#11 8.4 GB
    re-ship). Says which mode ran (delta) and its saving, and that the delta is
    additive — stale remote files are not pruned (deletion is out of scope; an
    rsync ``--delete`` is the tool for that). Fail-open like the sibling
    disclosures.
    """
    try:
        if n_ship == 0:
            print(
                f"[transport] no rsync on PATH -> content-hash DELTA: the remote is "
                f"already identical for all {n_local} files; shipping 0 bytes.",
                file=sys.stderr,
            )
            return
        mb_ship = shipped_bytes / (1024 * 1024)
        mb_total = total_bytes / (1024 * 1024)
        print(
            f"[transport] no rsync on PATH -> content-hash DELTA: {n_reused}/{n_local} files "
            f"already on the remote by content-hash; shipping {n_ship} changed/new "
            f"({mb_ship:.1f} MB of {mb_total:.1f} MB). Additive only: stale remote files are "
            f"NOT pruned (install rsync for --delete).",
            file=sys.stderr,
        )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


#: Cap on the remote hash manifest's file count. A delta needs one sha per
#: file shipped back over the (slow) link; past this the manifest stops being
#: "bounded output" and the push falls back to the full tar (disclosed) rather
#: than pull a pathological payload back. The pushable code/data tree is small
#: (run output dirs are excluded), so a real push never approaches this.
_DELTA_MANIFEST_FILE_CAP: Final[int] = 100_000

#: Env kill-switch: set ``HPC_NO_DEPLOY_DELTA=1`` to force the whole-tree tar
#: copy on rsync-less hosts even when a remote manifest is available (mirrors
#: ``HPC_NO_DEPLOY_CACHE`` for :func:`deploy_runtime`). The full-copy disclosure
#: then names this as the reason.
_DELTA_ENV_KILL = "HPC_NO_DEPLOY_DELTA"

#: The self-contained python the DEPLOYED runtime runs cluster-side to hash its
#: own tree — the "remote side hashes its deployed tree, shipped back as a hash
#: manifest" half of item 6b. Stdlib-only so it runs under any cluster ``python3``
#: without the framework installed; base64-piped over one ssh round-trip so no
#: quoting of the source is needed. It mirrors :func:`_path_excluded` and
#: :class:`Manifest`'s content-hash exactly, so local and remote agree on both
#: the file set and each file's identity. Emits ``{"files": [...]}`` (the
#: :meth:`Manifest.from_dict` shape); prints nothing — routing the caller to the
#: full-copy fallback — on any error, a first/absent tree, or a file count past
#: the cap.
_REMOTE_MANIFEST_SNIPPET = textwrap.dedent(
    """
    import os, sys, json, hashlib, fnmatch
    try:
        pats = [str(p).rstrip('/') for p in json.loads(os.environ.get('HPC_DELTA_EXCLUDES', '[]'))]
        cap = int(os.environ.get('HPC_DELTA_CAP', '100000'))

        def excluded(parts):
            for i, part in enumerate(parts):
                for pat in pats:
                    if pat.startswith('./') or pat.startswith('^'):
                        a = pat[2:] if pat.startswith('./') else pat[1:]
                        if i == 0 and fnmatch.fnmatch(part, a):
                            return True
                        continue
                    if fnmatch.fnmatch(part, pat):
                        return True
            return False

        files = []
        for dp, dirs, names in os.walk('.'):
            rel = '' if dp == '.' else os.path.relpath(dp, '.').replace(os.sep, '/')
            base = tuple(rel.split('/')) if rel else ()
            dirs[:] = [d for d in dirs if not excluded(base + (d,))]
            for n in names:
                parts = base + (n,)
                if excluded(parts):
                    continue
                full = os.path.join(dp, n)
                if not os.path.isfile(full):
                    continue
                try:
                    h = hashlib.sha256()
                    with open(full, 'rb') as fh:
                        for chunk in iter(lambda: fh.read(1048576), b''):
                            h.update(chunk)
                    size = os.path.getsize(full)
                except OSError:
                    continue
                files.append({'path': '/'.join(parts), 'size': size, 'sha256': h.hexdigest()})
                if len(files) > cap:
                    sys.exit(0)  # too big -> no output -> caller ships the whole tree
        sys.stdout.write(json.dumps({'files': files}))
    except Exception:
        pass
    """
).strip()


def _pushable_relpaths(root: Path, exclude: list[str]) -> list[str]:
    """POSIX relpaths of every file under *root* the push would ship.

    The exclude-filtered file set that the local delta manifest is built over,
    using the same :func:`_path_excluded` test the disclosure and the remote
    snippet use — so the two manifests describe the same tree.
    """
    pats = [p.rstrip("/") for p in exclude]
    rels: list[str] = []
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if _path_excluded(rel.parts, pats):
            continue
        if p.is_file():
            rels.append(rel.as_posix())
    return rels


def _local_push_manifest(local_path: str | Path, exclude: list[str]) -> Any:
    """Content manifest of the local push tree (exclude-filtered) — item 6b.

    Returns a :class:`hpc_agent.ops.transfer.manifest.Manifest`; imported lazily
    to keep this low-level infra module import-light.
    """
    from hpc_agent.ops.transfer.manifest import build_manifest

    root = Path(local_path)
    paths = _pushable_relpaths(root, exclude)
    # Phase disclosure (run-#12 finding 3): hashing a multi-GB tree is
    # MINUTES of silence otherwise — the 8.7GB scan read as a hang twice in
    # one night. One line in, one line out, same stderr surface as the
    # transfer heartbeat.
    print(
        f"[transport] content-hash scan: hashing {len(paths)} local file(s) "
        "for the push delta (minutes on a large tree; transfer follows)",
        file=sys.stderr,
    )
    manifest = build_manifest(root, paths=paths)
    print(
        f"[transport] content-hash scan done ({len(paths)} file(s)); "
        "comparing against the remote manifest",
        file=sys.stderr,
    )
    return manifest


def _parse_remote_push_manifest(stdout: str) -> Any | None:
    """Parse the cluster-side hash manifest, or ``None`` on any problem.

    An absent/empty tree (snippet printed nothing), corrupt JSON, a wrong shape,
    or a cap breach all collapse to ``None`` — which routes the push to the
    full-copy tar fallback (disclosed). The safe direction: never claim a remote
    file is present unless the manifest proves it.
    """
    from hpc_agent.ops.transfer.manifest import Manifest

    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not (isinstance(data, dict) and isinstance(data.get("files"), list)):
        return None
    try:
        return Manifest.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _remote_push_manifest(
    *, ssh_target: str, remote_path: str, exclude: list[str], timeout: float | None
) -> Any | None:
    """One bounded ssh round-trip: the deployed runtime hashes the remote tree.

    Ships :data:`_REMOTE_MANIFEST_SNIPPET` base64-piped into ``python3`` under
    ``remote_path`` and parses the JSON manifest it prints. Returns a
    :class:`Manifest` of the remote tree, or ``None`` when the remote can't
    produce one — a first deploy (``cd`` fails, absent tree), a pre-delta
    runtime, a python/base64 gap, a cap breach, or a timeout. ``None`` routes to
    the full-copy fallback (disclosed), so this is never worse than the prior
    whole-tree behavior. *remote_path* is ``shlex.quote``-d; the snippet is
    base64 (no shell metacharacters) so no source quoting is needed.
    """
    b64 = base64.b64encode(_REMOTE_MANIFEST_SNIPPET.encode("utf-8")).decode("ascii")
    excludes_json = json.dumps([p.rstrip("/") for p in exclude])
    remote_cmd = (
        f"cd {shlex.quote(remote_path)} && printf %s {shlex.quote(b64)} | base64 -d | "
        f"HPC_DELTA_EXCLUDES={shlex.quote(excludes_json)} "
        f"HPC_DELTA_CAP={_DELTA_MANIFEST_FILE_CAP} python3"
    )
    try:
        proc = _ssh_bounded(
            ssh_target,
            remote_cmd,
            timeout=timeout,
            what=f"remote hash manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
        return None
    return _parse_remote_push_manifest(getattr(proc, "stdout", "") or "")


# ── bounded auto-prune of manifest-known remote extras (data-manifest ruling 6) ──
#
# The rsync-less delta push is additive: it never pruned the remote's ``extra``
# (files present remotely, absent locally), so a file we shipped in a PRIOR push
# and later dropped from the deploy set lingered on the cluster forever. The
# ruling (docs/design/data-manifest.md foot, 2026-07-10) lets us auto-delete the
# only class we can PROVE is ours — a remote extra recorded in the prior push
# manifest — under a disclosed twin cap (count + bytes). Anything NOT
# manifest-known is an ANOMALY: never deleted, surfaced to ask.
#
# This rides the SAME delete=True delta push that already holds the dial (the
# zero-unattended-cold-SSH discipline: prune never opens a new cold connection).

#: Remote-relative path of the push manifest — the record of what THIS control
#: plane last shipped to ``remote_path``. Read at the start of the next delta
#: push to decide which remote extras are manifest-known (ours to prune) vs
#: anomalies (foreign, never touched). Lives under ``.hpc/`` beside the deploy
#: cache; it is our own bookkeeping, so it is never itself treated as an extra.
_PUSH_MANIFEST_REL: Final[str] = ".hpc/.push_manifest.json"

#: Env kill-switch: ``HPC_NO_DEPLOY_PRUNE=1`` disables the auto-prune entirely
#: (the push stays additive, as it was before the ruling). Mirrors the
#: ``HPC_NO_DEPLOY_DELTA`` / ``HPC_NO_DEPLOY_CACHE`` opt-outs.
_PRUNE_ENV_KILL = "HPC_NO_DEPLOY_PRUNE"


def _prune_max_files() -> int:
    from hpc_agent.ops.transfer.prune import DEFAULT_PRUNE_MAX_FILES

    return _env_int("HPC_DEPLOY_PRUNE_MAX_FILES", DEFAULT_PRUNE_MAX_FILES)


def _prune_max_bytes() -> int:
    from hpc_agent.ops.transfer.prune import DEFAULT_PRUNE_MAX_BYTES

    return _env_int("HPC_DEPLOY_PRUNE_MAX_BYTES", DEFAULT_PRUNE_MAX_BYTES)


def _read_prior_push_manifest(
    *, ssh_target: str, remote_path: str, timeout: float | None
) -> set[str]:
    """The set of paths our LAST push recorded at :data:`_PUSH_MANIFEST_REL`.

    One bounded ssh ``cat`` (rides the push's dial). Returns an EMPTY set on any
    problem — a first push (no manifest), a read/parse error, a wrong shape — so
    a manifest we cannot prove routes every remote extra to the ANOMALY branch
    (never deleted). The safe direction: only a path we can PROVE we shipped is
    ever prunable.
    """
    quoted = shlex.quote(f"{remote_path.rstrip('/')}/{_PUSH_MANIFEST_REL}")
    try:
        proc = _ssh_bounded(
            ssh_target,
            f"cat {quoted} 2>/dev/null",
            timeout=timeout,
            what=f"read push manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
        return set()
    raw = (getattr(proc, "stdout", "") or "").strip()
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    paths = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(paths, list):
        return set()
    return {str(p) for p in paths}


def _write_push_manifest(
    *, ssh_target: str, remote_path: str, paths: list[str], timeout: float | None
) -> None:
    """Persist the current push's shipped path set at :data:`_PUSH_MANIFEST_REL`.

    Base64-piped so no path needs shell quoting (mirrors the remote-manifest
    snippet). Fail-open: a write error only loses the NEXT push's prune ability
    (extras degrade to anomalies), never breaks this push.
    """
    doc = json.dumps({"paths": sorted(paths), "pkg_version": _pkg_version()})
    b64 = base64.b64encode(doc.encode("utf-8")).decode("ascii")
    root = shlex.quote(remote_path.rstrip("/"))
    dst = shlex.quote(_PUSH_MANIFEST_REL)
    cmd = f"cd {root} && mkdir -p .hpc && printf %s {shlex.quote(b64)} | base64 -d > {dst}"
    with contextlib.suppress(TimeoutError, OSError):
        _ssh_bounded(
            ssh_target,
            cmd,
            timeout=timeout,
            what=f"write push manifest of {remote_path}",
        )


def _journal_deploy_prune(local_path: str | Path, record: dict[str, Any]) -> None:
    """Append one prune record to ``<experiment>/.hpc/deploy_prune.jsonl``.

    The tier-0 "what we auto-deleted from the cluster, why, and its old sha"
    timeline (the data-manifest mint-journal pattern). Fail-open — a journal
    write must never break a push.
    """
    from hpc_agent.infra.io import append_jsonl_line
    from hpc_agent.infra.time import utcnow_iso

    try:
        path = Path(local_path) / ".hpc" / "deploy_prune.jsonl"
        append_jsonl_line(path, {"ts": utcnow_iso(), **record})
    except OSError:
        pass


def _disclose_prune(plan: Any, *, remote_path: str) -> None:
    """One ``[transport]`` line per prune outcome (disclosure, never blocking).

    Names the manifest-known deletes, the refusal (over-bound), and every
    ANOMALY the push refuses to touch — the "surface to ask" half of the ruling.
    Fail-open like the sibling delta disclosures.
    """
    try:
        if plan.refused:
            print(
                f"[transport] WARN deploy prune REFUSED: {plan.refuse_reason} "
                f"({len(plan.prunable)} manifest-known extras, {plan.prune_bytes} bytes, "
                f"on {remote_path}). Nothing pruned — review and re-push, or raise the cap "
                f"(HPC_DEPLOY_PRUNE_MAX_FILES / HPC_DEPLOY_PRUNE_MAX_BYTES).",
                file=sys.stderr,
            )
        elif plan.to_prune:
            print(
                f"[transport] deploy prune: deleting {len(plan.to_prune)} manifest-known "
                f"remote extra(s) ({plan.prune_bytes} bytes) no longer in the deploy set "
                f"(journaled to .hpc/deploy_prune.jsonl).",
                file=sys.stderr,
            )
        if plan.anomalies:
            named = ", ".join(plan.anomalies[:5])
            more = "" if len(plan.anomalies) <= 5 else f" (+{len(plan.anomalies) - 5} more)"
            print(
                f"[transport] WARN deploy prune ANOMALY: {len(plan.anomalies)} remote file(s) "
                f"not manifest-known — NOT deleted, needs a human decision: {named}{more}.",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 — disclosure is never load-bearing
        pass


def _is_runtime_placed(relpath: str) -> bool:
    """True when *relpath* is a ``deploy_runtime``-placed framework file.

    Those files ride their own deploy leg OUTSIDE the repo push, so the push
    manifest never knows them — they are the framework's own, never prune
    candidates and never anomalies to nag a human about (run-#12: six eternal
    "needs a human decision" lines for the dispatcher + templates the
    framework itself deployed). Matches the same :data:`PROTECTED_RUNTIME_FILES`
    set every push's exclude union protects.
    """
    for prot in PROTECTED_RUNTIME_FILES:
        if prot.endswith("/"):
            if relpath == prot.rstrip("/") or relpath.startswith(prot):
                return True
        elif relpath == prot:
            return True
    return False


def _execute_prune(
    *, ssh_target: str, remote_path: str, paths: list[str], timeout: float | None
) -> bool:
    """Delete exactly *paths* under *remote_path* via one bounded ssh ``rm``.

    Each path is ``shlex.quote``-d and the list is the vetted manifest-known set
    (never anomalies, never over-bound). ``rm -f --`` is 0 even if a path already
    vanished. Returns True on a clean delete. Fail-open on any transport error —
    the manifest-known extra simply survives to the next push.
    """
    if not paths:
        return False
    root = shlex.quote(remote_path.rstrip("/"))
    quoted_paths = " ".join(shlex.quote(p) for p in paths)
    try:
        proc = _ssh_bounded(
            ssh_target,
            f"cd {root} && rm -f -- {quoted_paths}",
            timeout=timeout,
            what=f"prune {len(paths)} manifest-known extra(s) from {remote_path}",
        )
    except (TimeoutError, OSError):
        return False
    return getattr(proc, "returncode", 1) == 0


def _prune_manifest_known_extras(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: str | Path,
    remote_manifest: Any,
    extra: tuple[str, ...],
    timeout: float | None,
) -> None:
    """Plan + disclose + journal + execute the bounded auto-prune (ruling 6).

    Called only from the delete=True delta push (holds the dial). Fully
    fail-open: any error leaves the remote untouched — a prune we cannot do
    cleanly is a prune we skip, never a broken push.
    """
    if os.environ.get(_PRUNE_ENV_KILL) == "1":
        return
    try:
        # Our own bookkeeping file is a remote extra (never shipped locally); it
        # is neither ours-to-prune nor an anomaly — filter it out up front.
        # Same for deploy_runtime's own placed files (:func:`_is_runtime_placed`).
        candidates = [p for p in extra if p != _PUSH_MANIFEST_REL and not _is_runtime_placed(p)]
        if not candidates:
            return

        from hpc_agent.ops.transfer.manifest import FileEntry
        from hpc_agent.ops.transfer.prune import plan_prune

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
            return
        if not plan.to_prune:
            return

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
        _execute_prune(
            ssh_target=ssh_target,
            remote_path=remote_path,
            paths=list(plan.to_prune),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — the prune is never load-bearing on a push
        pass


#: Transfer-progress heartbeat cadence (queue item 10). The tar|ssh pipe emits
#: nothing until it exits, so a multi-hour full re-ship looked hung; a line every
#: ~15s to the detached-worker log makes the transfer observable. Override for
#: tests via the ``interval_sec`` arg on :func:`_pump_with_progress`.
_PROGRESS_INTERVAL_SEC: Final[float] = 15.0
#: Pump read/write granularity. 1 MiB balances syscall overhead against the
#: heartbeat's byte-count resolution; binary-safe regardless of value.
_PUMP_CHUNK_BYTES: Final[int] = 1024 * 1024


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, looping over partial ``os.write``s.

    ``os.write`` may write fewer bytes than offered (a full pipe buffer), so a
    single call can silently truncate the stream. The memoryview slice avoids
    re-copying the tail on each iteration.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _pump_with_progress(
    src: IO[bytes],
    dst_fd: int,
    *,
    total_bytes: int,
    interval_sec: float = _PROGRESS_INTERVAL_SEC,
    chunk_size: int = _PUMP_CHUNK_BYTES,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """Copy *src* to *dst_fd* in chunks, emitting a progress heartbeat (item 10).

    Interposed on the ``tar c | ssh tar x`` pipe so the otherwise-silent transfer
    reports ``[transport] progress: X MB / ~Y MB (Z%), elapsed Ts`` every
    ~*interval_sec* to stderr (the detached-worker log — the tail-able surface).
    *total_bytes* is the estimate :func:`_disclose_payload` already computed; a 0
    total prints ``0%`` rather than dividing by zero.

    Transfer-semantics-preserving: reads/writes raw bytes (binary-safe), and
    :func:`_write_all` blocks on a full pipe so backpressure flows to ``tar``
    exactly as a direct fd hand-off would. Returns the byte count forwarded.
    Always closes *dst_fd* on exit — that EOF is what tells the remote ``tar x``
    the stream is complete.
    """
    start = now()
    last_emit = start
    sent = 0
    try:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            _write_all(dst_fd, chunk)
            sent += len(chunk)
            current = now()
            if current - last_emit >= interval_sec:
                _emit_progress(sent, total_bytes, start, current)
                last_emit = current
    finally:
        with contextlib.suppress(OSError):
            os.close(dst_fd)
    return sent


def _emit_progress(sent: int, total_bytes: int, start: float, current: float) -> None:
    """Print one ``[transport] progress: ...`` heartbeat line to stderr."""
    sent_mb = sent / (1024 * 1024)
    total_mb = total_bytes / (1024 * 1024)
    pct = (100 * sent / total_bytes) if total_bytes > 0 else 0.0
    elapsed = current - start
    print(
        f"[transport] progress: {sent_mb:.0f} MB / ~{total_mb:.0f} MB "
        f"({pct:.0f}%), elapsed {elapsed:.0f}s",
        file=sys.stderr,
    )


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
            from hpc_agent.ops.transfer.manifest import manifest_delta

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
            if ship:
                pushed = guarded_call(
                    ssh_target,
                    lambda: _tar_ssh_push(
                        ssh_target=ssh_target,
                        remote_path=remote_path,
                        local_path=local_path,
                        exclude=exclude,
                        delete=False,
                        timeout=effective_timeout,
                        total_bytes=shipped_bytes,
                        only_paths=ship,
                    ),
                )
                if pushed.returncode != 0:
                    # Transfer failed — leave the remote as-is (no prune, no
                    # manifest rewrite) so the next push retries cleanly.
                    return pushed
            else:
                pushed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

            # Bounded auto-prune of MANIFEST-KNOWN remote extras (ruling 6): the
            # delta tar cannot prune, but a file WE shipped in a prior push and
            # since dropped is safe to delete under a disclosed cap. Rides this
            # same delete=True dial (no new cold SSH); anomalies are never
            # touched. Then persist the push manifest so the NEXT push knows what
            # is ours. Both fail-open — neither can break a successful transfer.
            _prune_manifest_known_extras(
                ssh_target=ssh_target,
                remote_path=remote_path,
                local_path=local_path,
                remote_manifest=remote_manifest,
                extra=delta.extra,
                timeout=effective_timeout,
            )
            _write_push_manifest(
                ssh_target=ssh_target,
                remote_path=remote_path,
                paths=[e.path for e in local_manifest.entries],
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


# Remote path (relative to ``remote_path``) of the content-hash cache the
# deploy step keys on to skip re-shipping unchanged files (#242). It maps
# each deployed file's remote-relative path to the sha256 of the bytes last
# placed there, alongside the package version that produced them.
_DEPLOY_MANIFEST_REL: Final[str] = ".hpc/.deploy_state.json"


def _sha256_bytes(data: bytes) -> str:
    """Hex sha256 of *data* — the content identity used by the deploy cache."""
    return hashlib.sha256(data).hexdigest()


def _pkg_version() -> str:
    """Installed ``hpc-agent`` version, or a stable placeholder when absent.

    Keys the deploy cache (#242) so a ``pip install -U`` invalidates the
    whole manifest even when individual file bytes look unchanged. The
    placeholder is process-stable, so a source checkout that is not
    pip-installed still produces a consistent (always-self-consistent) key.
    """
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("hpc-agent", "hpc_agent"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0+unknown"


@dataclass(frozen=True)
class _DeployItem:
    """One file the runtime deploy ships, with its content identity.

    Exactly one of *src_path* (a verbatim package file, scp'd directly so
    error/backoff labels name the real path) or *content* (text rendered at
    deploy time, e.g. a per-scheduler array script) is set. *sha* is the
    sha256 of the bytes that land on the cluster — file bytes for *src_path*,
    UTF-8 of *content* for *content* — and is what the deploy cache compares.
    """

    dst_rel: str
    sha: str
    src_path: Path | None
    content: str | None


def _build_deploy_items(*, scheduler: str | None) -> list[_DeployItem]:
    """Enumerate every file :func:`deploy_runtime` ships, hashed for caching.

    The single source of truth for *what* the deploy places and the content
    sha of each piece, shared by :func:`deploy_runtime` and
    :func:`_local_deploy_manifest` so the cache key the deploy writes is the
    same key it later compares against. Order is deterministic but no longer
    load-bearing — copies are fired concurrently (#245).
    """
    from hpc_agent.infra.backends import get_backend_class, template_ext_for

    pkg_dir = Path(__file__).parent.parent
    items: list[_DeployItem] = []

    def add_file(src: Path, dst_rel: str) -> None:
        items.append(_DeployItem(dst_rel, _sha256_bytes(src.read_bytes()), src, None))

    def add_text(content: str, dst_rel: str) -> None:
        items.append(_DeployItem(dst_rel, _sha256_bytes(content.encode("utf-8")), None, content))

    # Importable stubs (used inside cluster jobs by user code):
    #   - ``from hpc_agent.execution.mapreduce.metrics_io import write_metrics``
    #     in user executor scripts (executor_template.py).
    #   - ``from hpc_agent.executor_cli import flag, generic_args, gpu_args``
    #     in user .hpc/tasks.py. Both modules are stdlib-only (AST-scanned)
    #     so they ship without dragging in the rest of the package.
    add_file(
        pkg_dir / "execution" / "mapreduce" / "metrics_io.py",
        "hpc_agent/execution/mapreduce/metrics_io.py",
    )
    add_file(pkg_dir / "executor_cli.py", "hpc_agent/executor_cli.py")

    # Framework executor inside .hpc/.
    add_file(pkg_dir / "execution" / "mapreduce" / "dispatch.py", ".hpc/_hpc_dispatch.py")

    # Per-scheduler cpu/gpu array scripts, RENDERED from the profile rather
    # than shipped verbatim. Remote paths are preserved exactly
    # (``.hpc/templates/cpu_array.{sh,slurm}`` etc.) so downstream submit code
    # keeps resolving them. Deploy only the cluster's own family when
    # *scheduler* is known; fall back to sge+slurm otherwise. A single-family
    # deploy is what keeps pbspro/torque (shared ``.pbs`` ext) from colliding.
    schedulers = (scheduler,) if scheduler else ("sge", "slurm")
    for sched in schedulers:
        backend_cls = get_backend_class(sched)
        ext = template_ext_for(sched).lstrip(".")
        # ``mpi`` (single multi-rank job, #293) ships alongside the cpu/gpu
        # array bodies so a submit with an ``mpi`` block finds its template.
        for basename, kind in (("cpu_array", "cpu"), ("gpu_array", "gpu"), ("mpi", "mpi")):
            add_text(backend_cls.render_script(kind=kind), f".hpc/templates/{basename}.{ext}")

    # Shared preambles sourced by the templates above.
    for common_name in ("hpc_preamble.sh", "gpu_preamble.sh"):
        add_file(
            pkg_dir / "execution" / "mapreduce" / "templates" / "runtime" / "common" / common_name,
            f".hpc/templates/common/{common_name}",
        )

    # Combiner inside .hpc/.
    add_file(pkg_dir / "execution" / "mapreduce" / "combiner.py", ".hpc/_hpc_combiner.py")

    # Status REPORTER + its stdlib-only EAGER (import-time) closure (#349).
    #
    # The reporter runs cluster-side as
    # ``python -m hpc_agent.execution.mapreduce.reduce.status`` (reconcile's
    # remote_activation path, 0.10.12). Shipping its module-load closure here
    # lets the *deployed* copy satisfy that import under any python, so the
    # framework's runtime no longer needs a full ``hpc_agent`` in the job
    # conda env. This is ADDITIVE: the deployed tree is a PEP 420 namespace
    # package (no ``__init__.py``), so when the env DOES carry a regular
    # ``hpc_agent`` install it wins by namespace-package precedence and these
    # files are inert.
    #
    # SCOPE: only the *eager* (module-load) closure is shipped — every module
    # ``status`` imports at top level, transitively. It is stdlib-only
    # (verified by tests/contracts/test_cluster_runtime_self_contained.py,
    # which imports the reporter under ``python -S`` with the installed
    # ``hpc_agent`` invisible). The reporter's *function-local* runtime
    # closure (``state.runs``, ``infra.backends``, ``infra.clusters``,
    # ``recovery.registry``, and ``hpc_agent/__init__.py`` via
    # ``from hpc_agent import load_tasks_module``) pulls in pydantic / yaml /
    # jsonschema and is deliberately NOT shipped — those are the experiment
    # env's job, and flipping the env to python-only is the separate,
    # cluster-gated half of #349.
    reporter_closure = (
        # The reporter entry module itself.
        ("execution/mapreduce/reduce/status.py", "hpc_agent/execution/mapreduce/reduce/status.py"),
        # Eager intra-package deps of status (all stdlib-only):
        ("execution/mapreduce/reduce/rollup.py", "hpc_agent/execution/mapreduce/reduce/rollup.py"),
        ("_kernel/contract/task_id.py", "hpc_agent/_kernel/contract/task_id.py"),
        ("_kernel/contract/vocabulary.py", "hpc_agent/_kernel/contract/vocabulary.py"),
        ("errors.py", "hpc_agent/errors.py"),
        ("infra/time.py", "hpc_agent/infra/time.py"),
        # The #159 import-sanity guard the reporter's _main() invokes (after
        # arg-parse, so ``--help`` never reaches it). Stdlib-only; shipped so
        # a real reporter run resolves it from the deployed copy too.
        ("execution/mapreduce/_guard.py", "hpc_agent/execution/mapreduce/_guard.py"),
    )
    for src_rel, dst_rel in reporter_closure:
        add_file(pkg_dir / Path(src_rel), dst_rel)
    return items


def _local_deploy_manifest(*, scheduler: str | None) -> dict[str, Any]:
    """The deploy-cache manifest the CURRENT local sources would produce.

    ``{"pkg_version": <version>, "files": {dst_rel: sha256, ...}}`` — exactly
    what :func:`deploy_runtime` writes to :data:`_DEPLOY_MANIFEST_REL` after a
    deploy. Comparing it against the manifest read back from the cluster is
    how the cache decides which files (if any) actually need re-shipping.
    """
    items = _build_deploy_items(scheduler=scheduler)
    return {
        "pkg_version": _pkg_version(),
        "files": {it.dst_rel: it.sha for it in items},
    }


def _parse_remote_manifest(stdout: str) -> dict[str, Any] | None:
    """Parse the cluster-side deploy manifest, or ``None`` on any problem.

    A missing file (``cat`` printed nothing), truncated/corrupt JSON, or a
    shape that isn't ``{"files": {...}}`` all collapse to ``None`` — which
    :func:`deploy_runtime` treats as a full cache miss (re-deploy everything),
    the safe fallback the issue's risk note calls for (mitigation b).
    """
    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data
    return None


def _rsync_deploy(*, ssh_target: str, remote_path: str, staging: Path) -> None:
    """rsync the staged deploy tree to the cluster — one invocation, delta only.

    ``-az --inplace`` ships only the changed bytes of changed files. The cache
    already filtered to changed *files*; rsync's delta narrows it further to
    changed *bytes*. NO ``--delete``: deploy merges its subset into the cluster
    tree and must never remove the user's run output or sibling framework
    files. rsync invokes its own ssh, so :func:`ssh_env` pins the binary +
    crypto/multiplex opts, mirroring :func:`rsync_push`.
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
                ["rsync", "-az", "--inplace", src, dst],
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
    tree in ONE invocation: an ``rsync -az --inplace`` delta where rsync is on
    PATH, else a single ``tar c | ssh tar x`` stream (``delete=False`` — merge,
    never remove). Same transport detection :func:`rsync_push` uses.
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
    an ``rsync -az --inplace`` delta where rsync is on PATH — so only the
    *changed bytes* of changed files cross the wire, which matters for the
    framework artifacts that grow over time (combiner.py, dispatch.py, the
    templates) — falling back to one ``tar c | ssh tar x`` stream on hosts
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

    # Record the new manifest only when it actually changed (a full cache hit
    # leaves it identical, so we skip the write — and the whole transfer — and
    # its round-trip). The manifest rides the SAME batched transfer as the
    # files it describes, written last in the staging tree; a failed transfer
    # raises before anything is recorded, so a stale manifest never claims a
    # file landed that didn't.
    manifest_changed = use_cache and remote_manifest != new_manifest
    transfer_items = list(to_deploy)
    if manifest_changed:
        manifest_json = json.dumps(new_manifest, indent=2, sort_keys=True)
        transfer_items.append(
            _DeployItem(
                dst_rel=_DEPLOY_MANIFEST_REL,
                sha=_sha256_bytes(manifest_json.encode("utf-8")),
                src_path=None,
                content=manifest_json,
            )
        )
    if transfer_items:
        _deploy_transfer(ssh_target=ssh_target, remote_path=remote_path, items=transfer_items)


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


def run_final_reduce(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the cluster-side FINAL cross-wave reduce on the login node (#254).

    Invokes ``.hpc/_hpc_combiner.py --final --run-id <id>`` over SSH. The
    combiner merges every ``_combiner/wave_*.json`` into a single
    ``_aggregated/<run_id>/metrics_aggregate.json`` on the cluster, so the
    caller pulls one kilobyte-scale file instead of hundreds of wave partials.
    Mirrors :func:`run_combiner` (same activation + timeout contract); pass
    ``force=True`` to overwrite an existing aggregate.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{remote_activation}"
        f"HPC_RUN_ID={run_id_q} "
        f"python3 .hpc/_hpc_combiner.py --final --run-id {run_id_q}{force_flag}"
    )
    if timeout is _DEFAULT:
        return ssh_run(cmd, ssh_target=ssh_target)
    return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)


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
