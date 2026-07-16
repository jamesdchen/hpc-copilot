"""Content-hash PULL engine: the rsync-less pull analogue of the batched push.

The push side got server-side-nothing-wasted transfers (content-hash delta,
bounded resumable batches, a remote hash manifest) in the run-#11/12/13 work;
the pull side was still a monolithic ``scp -r`` that ignores the include filter
and re-pays the whole transfer on any failure (latency-audit ranks 2 + 7). This
module is the inverse of :mod:`hpc_agent.infra.transport._delta` +
``_tar_ssh_push``, built for the direction where bytes LAND locally:

* **server-side filtered enumeration** — a remote ``find | tar c`` honors an
  include allowlist so a "filtered" pull transfers only the matching files (the
  1000x lever: a 2700-task ``metrics.json`` pull is KB, not the GB of CSVs beside
  them), one round trip.
* **content-hash delta** — a remote hash manifest (filtered ``find`` + sha256)
  diffed against the files ALREADY present locally, so an already-identical file
  never re-transfers. Reuses the local quick-check cache machinery from
  :mod:`._delta` by import (read-only) and the pure :func:`manifest_delta`.
* **bounded, resumable batches** — the delta ships in size/count/name-length
  bounded batches that land DIRECTLY in the destination; a died pull's landed
  batches are already present, so the next call's delta re-derives them and pulls
  only the remainder (local-side bookkeeping — the local end is where files land,
  so no remote checkpoint is needed).
* **stream compression** — the tar stream is gzip/zstd compressed by default on
  these VPN legs (:func:`hpc_agent.infra.ssh_options.tar_stream_flag`).

The single frozen export other units build against is :func:`tar_ssh_pull`
returning :class:`PullResult`.
"""

from __future__ import annotations

import base64
import contextlib
import fnmatch
import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from hpc_agent.infra.bounded_subprocess import run_capture_bounded
from hpc_agent.infra.remote import RSYNC_TIMEOUT_SEC, _env_int, _truncate
from hpc_agent.infra.ssh_circuit import guarded_call
from hpc_agent.infra.ssh_options import (
    connect_failure_retry_delays,
    is_retry_safe,
    run_with_named_pipe_retry,
    ssh_argv,
    tar_stream_codec,
    tar_stream_flag,
)
from hpc_agent.infra.ssh_throttle import throttle_connection
from hpc_agent.infra.ssh_validation import validate_remote_path

from ._delta import _DELTA_MANIFEST_FILE_CAP
from ._disclose import _pump_with_progress, disclose_child_failure

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = ["PullResult", "tar_ssh_pull"]


@dataclass(frozen=True)
class PullResult:
    """The outcome of a :func:`tar_ssh_pull` — the frozen seam other units read.

    * ``ok`` — every planned file landed (or nothing needed pulling).
    * ``files_pulled`` — files actually transferred this call.
    * ``bytes_pulled`` — bytes actually transferred this call.
    * ``skipped_unchanged`` — remote files already content-identical locally
      (the delta saving; 0 on the manifest-less fallback path).
    * ``stderr_tail`` — a bounded child-stderr tail on failure ("" on success).
    """

    ok: bool
    files_pulled: int
    bytes_pulled: int
    skipped_unchanged: int
    stderr_tail: str


# --- pull ship-batch caps (the pull analogue of _delta's ship batches) -------
#
# A batch closes when adding the next file would exceed the file-count, the
# byte, OR the NAME-length cap. The name cap is pull-specific: a delta batch
# ships its exact member list to the remote as a base64 blob INSIDE the ssh
# command string, and a login shell caps a single command string at ~128 KiB
# (Linux MAX_ARG_STRLEN); base64 expands 4/3, so cap the raw names well under
# that. Env-overridable for ops + tests.
_PULL_BATCH_MAX_FILES: Final[int] = 2000
_PULL_BATCH_MAX_BYTES: Final[int] = 256 * 1024 * 1024  # 256 MiB
_PULL_BATCH_MAX_NAME_BYTES: Final[int] = 64 * 1024  # base64 -> ~85 KiB command


def _pull_batch_caps() -> tuple[int, int, int]:
    """The (max_files, max_bytes, max_name_bytes) pull ship-batch caps, env-tunable."""
    return (
        max(1, _env_int("HPC_PULL_BATCH_MAX_FILES", _PULL_BATCH_MAX_FILES)),
        max(1, _env_int("HPC_PULL_BATCH_MAX_BYTES", _PULL_BATCH_MAX_BYTES)),
        max(1, _env_int("HPC_PULL_BATCH_MAX_NAME_BYTES", _PULL_BATCH_MAX_NAME_BYTES)),
    )


def _pull_ship_batches(
    pull: list[str],
    sizes: dict[str, int],
    *,
    max_files: int,
    max_bytes: int,
    max_name_bytes: int,
) -> Iterator[list[str]]:
    """Partition the ordered *pull* list into bounded batches.

    A batch closes when adding the next file would exceed the file-count, byte,
    or cumulative-name-length cap; an oversized single file (or a single very
    long name) still forms its own batch. Pure + deterministic so the batching
    is unit-testable without a transfer.
    """
    batch: list[str] = []
    batch_bytes = 0
    batch_name_bytes = 0
    for path in pull:
        size = sizes.get(path, 0)
        # ``./<path>\n`` is what rides the remote names list.
        name_cost = len(path.encode("utf-8")) + 3
        if batch and (
            len(batch) >= max_files
            or batch_bytes + size > max_bytes
            or batch_name_bytes + name_cost > max_name_bytes
        ):
            yield batch
            batch, batch_bytes, batch_name_bytes = [], 0, 0
        batch.append(path)
        batch_bytes += size
        batch_name_bytes += name_cost
    if batch:
        yield batch


# --- server-side filtered hash manifest (the remote half of the delta) -------
#
# Stdlib-only python the cluster runs to enumerate + hash its own filtered tree
# in one bounded ssh round-trip, base64-piped into ``python3`` (no source
# quoting). Emits the :meth:`Manifest.from_dict` shape ``{"files": [...]}`` with
# POSIX relpaths under the pulled dir; prints nothing (routing the caller to the
# find-driven full-copy fallback) on any error, an absent tree, or a file count
# past the cap. Include = an allowlist (a bare pattern matches the basename at
# any depth, a slashed pattern matches the relpath); exclude = a denylist with
# the same shape.
_REMOTE_PULL_MANIFEST_SNIPPET = textwrap.dedent(
    """
    import os, sys, json, hashlib, fnmatch
    try:
        inc = [str(p) for p in json.loads(os.environ.get('HPC_PULL_INCLUDES', 'null') or 'null')] \
            if os.environ.get('HPC_PULL_INCLUDES') else None
        exc = [str(p).rstrip('/') for p in json.loads(os.environ.get('HPC_PULL_EXCLUDES', '[]'))]
        cap = int(os.environ.get('HPC_PULL_CAP', '100000'))

        def matches(pats, rel, name):
            for pat in pats:
                if '/' in pat:
                    # A slashed pattern matches the relpath at ANY depth (the
                    # downstream rglob(summary_name) semantics), not only rooted
                    # at the results root: pat itself, pat as a subtree prefix,
                    # or pat nested under any parent dirs.
                    if (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat + '/*')
                            or fnmatch.fnmatch(rel, '*/' + pat)):
                        return True
                elif fnmatch.fnmatch(name, pat):
                    return True
            return False

        files = []
        for dp, dirs, names in os.walk('.'):
            base = '' if dp == '.' else os.path.relpath(dp, '.').replace(os.sep, '/')
            for n in names:
                rel = (base + '/' + n) if base else n
                if exc and matches(exc, rel, n):
                    continue
                if inc is not None and not matches(inc, rel, n):
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
                files.append({'path': rel, 'size': size, 'sha256': h.hexdigest()})
                if len(files) > cap:
                    sys.exit(0)  # too big -> no output -> caller falls back
        sys.stdout.write(json.dumps({'files': files}))
    except Exception:
        pass
    """
).strip()


def _ssh_capture(
    ssh_target: str,
    remote_cmd: str,
    *,
    timeout: float | None,
    what: str,
) -> subprocess.CompletedProcess[str]:
    """One bounded ssh command capturing stdout, named-pipe-retry wrapped.

    Used for the small manifest round-trip (bounded output). A timeout surfaces
    loud with *what* named; a spawn error propagates as ``OSError`` (the caller
    classifies it :func:`is_retry_safe`-false).
    """

    def _attempt() -> subprocess.CompletedProcess[str]:
        ssh_cmd = [*ssh_argv("ssh"), ssh_target, remote_cmd]
        try:
            return run_capture_bounded(ssh_cmd, timeout_sec=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{what} on {ssh_target} timed out after {timeout}s") from exc

    return run_with_named_pipe_retry(_attempt)


def _remote_pull_manifest(
    *,
    ssh_target: str,
    remote_path: str,
    include_globs: Sequence[str] | None,
    exclude: Sequence[str] | None,
    timeout: float | None,
) -> Any | None:
    """One bounded ssh round-trip: the cluster hashes its own filtered tree.

    Returns a :class:`Manifest` of the include/exclude-filtered files under
    *remote_path*, or ``None`` when the remote can't produce one (an absent tree,
    no ``python3``/``base64``, a cap breach, a timeout, or a spawn error).
    ``None`` routes the pull to the find-driven full-copy fallback (still
    server-side filtered), so this is never worse than a plain pull.
    """
    from hpc_agent.infra.manifest import Manifest

    b64 = base64.b64encode(_REMOTE_PULL_MANIFEST_SNIPPET.encode("utf-8")).decode("ascii")
    inc_json = "null" if include_globs is None else json.dumps([str(p) for p in include_globs])
    exc_json = json.dumps([str(p).rstrip("/") for p in (exclude or [])])
    remote_cmd = (
        f"cd {shlex.quote(remote_path)} && printf %s {shlex.quote(b64)} | base64 -d | "
        f"HPC_PULL_INCLUDES={shlex.quote(inc_json)} "
        f"HPC_PULL_EXCLUDES={shlex.quote(exc_json)} "
        f"HPC_PULL_CAP={_DELTA_MANIFEST_FILE_CAP} python3"
    )
    try:
        proc = _ssh_capture(
            ssh_target,
            remote_cmd,
            timeout=timeout,
            what=f"remote pull manifest of {remote_path}",
        )
    except (TimeoutError, OSError):
        return None
    raw = (getattr(proc, "stdout", "") or "").strip()
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


# --- pull-manifest local stat cache (F2, run-13 finding-13 rows) -------------
#
# The pull's own local quick-check cache — a DEDICATED file (never the push's
# ``.hpc/.push_hash_cache.json``: the two caches key different tree shapes and
# must not collide), keyed per relpath -> (size, mtime_ns, sha256, cmd_sha), so
# a re-aggregate over an unchanged local tree re-hashes NOTHING (a stat-walk).
#
# The three run-13 finding-13 hardenings the push snippet learned, brought to
# the pull side (doctrine D2 / G2):
#   * cmd_sha keying — the dispatcher stamps a ``.hpc_cmd_sha`` sidecar into each
#     result dir on promote. A repair/graft that re-runs a task moves that sidecar
#     but a torn overwrite can leave the sibling summary's (size, mtime_ns)
#     colliding. Stat only ever decides UNCHANGED; a moved cmd_sha MUST evict the
#     cached sha even when size+mtime match, or the pull-delta would judge the
#     stale local copy content-identical and never re-pull it (the finding-13
#     class, pull direction).
#   * skew window — an entry whose file mtime is younger than the skew window
#     counts DIRTY (re-hash): a torn write still in flight can share the prior
#     mtime at coarse fs granularity, so a just-touched file is never trusted from
#     stat alone.
#   * success-only — a read that raises (a vanished/severed file) is dropped from
#     BOTH the manifest and the cache; a failure is never cached as a value.
# The cache is rebuilt fresh each call holding ONLY the current path set, so
# foreign rows (a prior key set, a different run) drop out — a stale row can never
# be served. Written atomically (temp+rename) and fail-open: any cache problem
# degrades to a full re-hash, never a wrong sha.
_PULL_HASH_CACHE_REL: Final[str] = ".hpc/.pull_hash_cache.json"
_PULL_CACHE_VERSION: Final[int] = 1
_PULL_CMD_SHA_FILENAME: Final[str] = ".hpc_cmd_sha"

#: Skew window (ns): a cache entry whose file was modified within this window of
#: NOW counts dirty and is re-hashed — stat alone cannot rule out a torn write in
#: flight. Env-tunable (``HPC_PULL_CACHE_SKEW_SEC``, seconds) for ops + tests;
#: 0 disables the guard (a pure stat cache).
_PULL_CACHE_SKEW_SEC_DEFAULT: Final[float] = 2.0


def _pull_cache_skew_ns() -> int:
    """The pull-cache skew window in nanoseconds, env-tunable (>=0)."""
    raw = os.environ.get("HPC_PULL_CACHE_SKEW_SEC")
    if raw is None:
        secs = _PULL_CACHE_SKEW_SEC_DEFAULT
    else:
        try:
            secs = float(raw)
        except ValueError:
            secs = _PULL_CACHE_SKEW_SEC_DEFAULT
    return max(0, int(secs * 1_000_000_000))


def _load_pull_cache(root: Path) -> dict[str, dict[str, Any]]:
    """Read the pull quick-check cache, or ``{}`` on any problem (fail-open).

    A first pull, an unreadable/corrupt file, a wrong shape, or a schema-version
    mismatch all collapse to an empty cache — the scan then full-re-hashes and
    rewrites the cache. The cache is a pure optimization, never a correctness
    input, so a discarded cache only costs a re-hash (never a wrong sha).
    """
    path = root / _PULL_HASH_CACHE_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _PULL_CACHE_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    return entries


def _store_pull_cache(root: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Persist the pull quick-check cache atomically (temp+rename), fail-open.

    Only inside a real pull tree (``.hpc/`` creatable); a regenerable derived
    artifact, so ``fsync`` is skipped (a crash that loses it only forces a
    re-hash). A store failure never touches the manifest already returned.
    """
    from hpc_agent.infra.io import atomic_write_json

    with contextlib.suppress(OSError):
        atomic_write_json(
            root / _PULL_HASH_CACHE_REL,
            {"version": _PULL_CACHE_VERSION, "entries": entries},
            fsync=False,
        )


def _dir_cmd_sha(dir_path: Path, memo: dict[Path, str]) -> str:
    """The ``.hpc_cmd_sha`` fingerprint stamped in *dir_path*, or ``""`` if absent.

    Memoized per directory so a dir of many summaries reads its sidecar once. An
    unreadable/absent sidecar yields ``""`` (a legacy piece a pre-stamp
    dispatcher wrote): the cmd_sha gate then never fires for it and the size+mtime
    delta stands alone, exactly the finding's documented fallback.
    """
    cached = memo.get(dir_path)
    if cached is not None:
        return cached
    try:
        sha = (dir_path / _PULL_CMD_SHA_FILENAME).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        sha = ""
    memo[dir_path] = sha
    return sha


def _local_present_manifest(local_path: Path, paths: list[str]) -> Any:
    """Content manifest of the files in *paths* that ALREADY exist under *local_path*.

    Only present files are hashed (absent ones are what the pull will fetch), so
    this is the pull inverse of :func:`._delta._build_local_manifest_cached`,
    which hard-errors on a missing path. Backed by the DEDICATED pull quick-check
    cache (:func:`_load_pull_cache`) so a re-pull re-hashes ONLY files whose
    ``(size, mtime_ns)`` moved — with the run-13 finding-13 hardenings: a moved
    ``.hpc_cmd_sha`` evicts the cached sha even on a stat match, a file younger
    than the skew window is re-hashed, and a severed read is cached by neither the
    manifest nor the cache (success-only).
    """
    from hpc_agent.infra.manifest import FileEntry, Manifest, _sha256_of

    cache = _load_pull_cache(local_path)
    new_cache: dict[str, dict[str, Any]] = {}
    entries: list[Any] = []
    skew_ns = _pull_cache_skew_ns()
    now_ns = time.time_ns()
    cmd_sha_memo: dict[Path, str] = {}
    for rel in paths:
        rel_posix = Path(rel).as_posix()
        full = local_path / rel
        if not full.is_file():
            continue
        try:
            st = full.stat()
        except OSError:
            continue  # severed read: neither manifested nor cached (success-only)
        size = st.st_size
        mtime_ns = st.st_mtime_ns
        cmd_sha = _dir_cmd_sha(full.parent, cmd_sha_memo)
        prior = cache.get(rel_posix)
        is_young = skew_ns > 0 and (now_ns - mtime_ns) < skew_ns
        if (
            not is_young
            and isinstance(prior, dict)
            and prior.get("size") == size
            and prior.get("mtime_ns") == mtime_ns
            and prior.get("cmd_sha", "") == cmd_sha  # a moved cmd_sha evicts
            and isinstance(prior.get("sha256"), str)
        ):
            sha = str(prior["sha256"])
        else:
            try:
                sha = _sha256_of(full)
            except OSError:
                continue  # severed read mid-hash: success-only, drop it
        entries.append(FileEntry(path=rel_posix, size=size, sha256=sha))
        new_cache[rel_posix] = {
            "size": size,
            "mtime_ns": mtime_ns,
            "sha256": sha,
            "cmd_sha": cmd_sha,
        }
    entries.sort(key=lambda e: e.path)
    _store_pull_cache(local_path, new_cache)
    return Manifest(entries=tuple(entries))


def _find_filter_predicate(
    include_globs: Sequence[str] | None, exclude: Sequence[str] | None
) -> str:
    """The ``find`` predicate honoring *include_globs* (allowlist) + *exclude*.

    A bare pattern filters the basename (``-name``); a slashed pattern the path
    at ANY depth — both root-anchored (``-path './<pat>'``) and nested under any
    parent (``-path './*/<pat>'``), so a path-shaped summary matches wherever it
    lands under the results root (the downstream rglob semantics), not only at
    the root. Every token is ``shlex.quote``-d so the login shell passes the glob
    literally to ``find`` (find does its own globbing). Returns the predicate
    string to splice after ``find . -type f``.
    """

    def _term(pat: str, negate: bool) -> str:
        pat = pat.rstrip("/")
        if "/" in pat:
            core = pat[2:] if pat.startswith("./") else pat
            rooted = shlex.quote(f"./{core}")
            anydepth = shlex.quote(f"./*/{core}")
            if negate:
                # NOT(rooted OR any-depth) == NOT rooted AND NOT any-depth.
                return f"! -path {rooted} ! -path {anydepth}"
            return f"\\( -path {rooted} -o -path {anydepth} \\)"
        arg = shlex.quote(pat)
        return f"! -name {arg}" if negate else f"-name {arg}"

    parts: list[str] = []
    if include_globs:
        ors = " -o ".join(_term(p, negate=False) for p in include_globs)
        parts.append(f"\\( {ors} \\)")
    for pat in exclude or []:
        parts.append(_term(pat, negate=True))
    return (" " + " ".join(parts)) if parts else ""


def _pull_transfer(
    *,
    ssh_target: str,
    remote_cmd: str,
    local_path: Path,
    codec_flag: str | None,
    total_bytes: int,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """Run one remote ``tar c`` (via *remote_cmd*) and extract it into *local_path*.

    The inverse of ``_tar_ssh_push``: ssh is the SOURCE (its stdout is the tar
    archive) and a local ``tar x`` is the SINK. A byte-counting pump forwards the
    archive from ssh's stdout into local ``tar``'s stdin, emitting the ~15s
    progress heartbeat so a multi-minute VPN pull is observable. Returns a
    :class:`subprocess.CompletedProcess`; raises ``TimeoutError`` on timeout and
    propagates a ``FileNotFoundError`` spawn error to the caller.
    """
    tar_x_cmd = ["tar", "x"]
    if codec_flag:
        tar_x_cmd.append(codec_flag)
    tar_x_cmd += ["-f", "-", "-C", str(local_path)]

    def _attempt() -> subprocess.CompletedProcess[str]:
        ssh_cmd = [*ssh_argv("ssh"), ssh_target, remote_cmd]
        # ssh stderr to a temp file (read only after ssh exits) so a chatty
        # remote cannot fill a PIPE buffer and deadlock the archive stream.
        ssh_stderr_file = tempfile.TemporaryFile()  # noqa: SIM115 - closed in finally
        ssh_proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=ssh_stderr_file,
            stdin=subprocess.DEVNULL,
        )
        pump_r, pump_w = os.pipe()
        pump_error: list[BaseException] = []

        def _pump() -> None:
            try:
                assert ssh_proc.stdout is not None
                _pump_with_progress(ssh_proc.stdout, pump_w, total_bytes=total_bytes)
            except BaseException as exc:  # noqa: BLE001 — surfaced via pump_error
                pump_error.append(exc)
                with contextlib.suppress(OSError):
                    os.close(pump_w)

        pump_thread = threading.Thread(target=_pump, daemon=True)
        pump_thread.start()

        pump_r_open = True

        def _close_pump_r() -> None:
            nonlocal pump_r_open
            if pump_r_open:
                pump_r_open = False
                with contextlib.suppress(OSError):
                    os.close(pump_r)

        try:
            assert ssh_proc.stdout is not None
            tar_x = run_capture_bounded(tar_x_cmd, timeout_sec=timeout, stdin=pump_r)
            # local tar x has EXITED — close our read-end NOW, before joining the
            # pump, so a still-running pump sees EPIPE and unwinds promptly (the
            # #9 deadlock shape, inverted for the pull direction).
            _close_pump_r()
            join_timeout = 30.0 if timeout is None else timeout
            pump_thread.join(timeout=join_timeout)
            if pump_thread.is_alive():
                ssh_proc.kill()
                pump_thread.join(timeout=5)
            ssh_proc.stdout.close()
            ssh_proc.wait(timeout=timeout)
            ssh_stderr_file.seek(0)
            ssh_stderr_bytes = ssh_stderr_file.read()
        except subprocess.TimeoutExpired as exc:
            ssh_proc.kill()
            pump_thread.join(timeout=5)
            if ssh_proc.stdout is not None:
                with contextlib.suppress(OSError):
                    ssh_proc.stdout.close()
            with contextlib.suppress(Exception):
                ssh_proc.wait(timeout=5)
            raise TimeoutError(
                f"tar/ssh pull from {ssh_target} timed out after {timeout}s: "
                f"{_truncate(f'{ssh_target}:{remote_cmd[:60]} -> {local_path}')}"
            ) from exc
        finally:
            _close_pump_r()
            ssh_stderr_file.close()

        ssh_stderr = ssh_stderr_bytes.decode(errors="replace")
        combined_stderr = "\n".join(
            filter(None, [ssh_stderr.strip(), (tar_x.stderr or "").strip()])
        )
        # ssh (the SOURCE) failing truncates the archive; its non-zero wins. If
        # ssh exited 0 but the local tar failed, that rc wins. A pump-side break
        # (ssh died mid-stream) must never read as success on a truncated pull.
        rc = ssh_proc.returncode if ssh_proc.returncode != 0 else tar_x.returncode
        if pump_error and rc == 0:
            rc = 1
            combined_stderr = "\n".join(
                filter(None, [combined_stderr, f"transfer pump error: {pump_error[0]!r}"])
            )
        if rc != 0:
            disclose_child_failure(what="tar|ssh pull", returncode=rc, stderr=combined_stderr)
        return subprocess.CompletedProcess(
            args=ssh_cmd + ["|"] + tar_x_cmd,
            returncode=rc,
            stdout="",
            stderr=combined_stderr,
        )

    return run_with_named_pipe_retry(_attempt)


def _pull_transfer_with_retry(
    *,
    ssh_target: str,
    remote_cmd: str,
    local_path: Path,
    codec_flag: str | None,
    total_bytes: int,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    """:func:`_pull_transfer` under the tight connect-failure retry (rank 25).

    A connect-phase failure (unreachable host, refused/reset dial) is retried on
    the :func:`connect_failure_retry_delays` schedule — two attempts, each dial
    bounded by ``ConnectTimeout`` (15s), so ~2x15s instead of the command
    ladder's 3-5x60s. A local spawn error (ENOENT launching ssh/tar) and any
    non-connect remote-command failure are NOT retried (:func:`is_retry_safe`);
    the remote-command failure is instead resumable at the delta layer (the next
    call re-derives what landed). The whole thing rides the per-host breaker.
    """
    delays = connect_failure_retry_delays()
    attempt = 0
    while True:
        try:
            proc = guarded_call(
                ssh_target,
                lambda: _pull_transfer(
                    ssh_target=ssh_target,
                    remote_cmd=remote_cmd,
                    local_path=local_path,
                    codec_flag=codec_flag,
                    total_bytes=total_bytes,
                    timeout=timeout,
                ),
            )
        except FileNotFoundError as exc:
            # ENOENT spawning ssh/tar — deterministic, never retry-safe.
            return subprocess.CompletedProcess(
                args=[], returncode=127, stdout="", stderr=f"spawn failed (ENOENT): {exc}"
            )
        if proc.returncode == 0 or attempt >= len(delays):
            return proc
        if not is_retry_safe(proc.returncode, proc.stderr):
            return proc
        print(
            f"[transport] pull connect failure on {ssh_target} "
            f"(attempt {attempt + 1}); retrying in {delays[attempt]:.0f}s",
            file=sys.stderr,
        )
        time.sleep(delays[attempt])
        attempt += 1


def _match_globs(
    rel: str, include_globs: Sequence[str] | None, exclude: Sequence[str] | None
) -> bool:
    """Local-side twin of the remote snippet's include/exclude match (fallback count).

    A bare pattern matches the basename at any depth; a slashed pattern the
    relpath. Used only to count/size the fallback pull's landed set (the
    manifest-less path has no per-file sizes to report otherwise).
    """
    name = rel.rsplit("/", 1)[-1]

    def _hit(pats: Sequence[str]) -> bool:
        for pat in pats:
            pat = pat.rstrip("/")
            if "/" in pat:
                # Slashed pattern -> any-depth relpath match (twin of the remote
                # snippet's matches() and _find_filter_predicate's -path terms).
                if (
                    fnmatch.fnmatch(rel, pat)
                    or fnmatch.fnmatch(rel, pat + "/*")
                    or fnmatch.fnmatch(rel, "*/" + pat)
                ):
                    return True
            elif fnmatch.fnmatch(name, pat):
                return True
        return False

    if exclude and _hit(exclude):
        return False
    return include_globs is None or _hit(include_globs)


def _count_landed(
    local_path: Path, include_globs: Sequence[str] | None, exclude: Sequence[str] | None
) -> tuple[int, int]:
    """(files, bytes) of the include/exclude-matching files now under *local_path*.

    Best-effort accounting for the manifest-less fallback (which has no delta and
    pulls the whole filtered set); fail-open to ``(0, 0)`` on a walk error.
    """
    files = 0
    total = 0
    try:
        for dp, _dirs, names in os.walk(local_path):
            base = os.path.relpath(dp, local_path).replace(os.sep, "/")
            base = "" if base == "." else base
            for n in names:
                rel = f"{base}/{n}" if base else n
                if not _match_globs(rel, include_globs, exclude):
                    continue
                files += 1
                with contextlib.suppress(OSError):
                    total += (local_path / rel).stat().st_size
    except OSError:
        return (0, 0)
    return (files, total)


def _disclose_pull_mode(*, n_pull: int, n_remote: int, n_skip: int, pull_bytes: int) -> None:
    """One ``[transport]`` line naming the content-hash pull delta (rank 2)."""
    with contextlib.suppress(Exception):
        if n_pull == 0:
            print(
                f"[transport] content-hash PULL delta: all {n_remote} remote file(s) "
                "already identical locally; pulling 0 bytes.",
                file=sys.stderr,
            )
            return
        mb = pull_bytes / (1024 * 1024)
        print(
            f"[transport] content-hash PULL delta: {n_skip}/{n_remote} file(s) already "
            f"identical locally; pulling {n_pull} changed/new ({mb:.1f} MB) in bounded "
            "resumable batches (a died pull re-derives landed files, pulls only the rest).",
            file=sys.stderr,
        )


def _disclose_pull_batch(*, index: int, total: int, n_files: int, batch_bytes: int) -> None:
    """One ``[transport]`` line per pull ship-batch."""
    with contextlib.suppress(Exception):
        mb = batch_bytes / (1024 * 1024)
        print(
            f"[transport] content-hash PULL: fetching batch {index}/{total} "
            f"({n_files} file(s), {mb:.1f} MB); landed batches are durable so a "
            "died-mid-pull retry fetches only the remainder.",
            file=sys.stderr,
        )


def _batch_remote_cmd(remote_path: str, batch: list[str], codec_flag: str | None) -> str:
    """The remote command that tars EXACTLY *batch*'s files to stdout.

    The member list rides as a base64 blob in the command string (bounded by the
    pull batch's name cap), decoded into a remote temp file that ``tar c -T``
    consumes — one ssh connection, so it is one login node even on a round-robin
    cluster (a two-call names-then-tar split could land the temp file on a
    different node). The temp file is removed regardless of the tar exit.
    """
    names = "".join(f"./{rel}\n" for rel in batch)
    b64 = base64.b64encode(names.encode("utf-8")).decode("ascii")
    q_rp = shlex.quote(remote_path)
    codec = f" {codec_flag}" if codec_flag else ""
    return (
        f'cd {q_rp} && T="$(mktemp)" && printf %s {shlex.quote(b64)} | base64 -d > "$T" && '
        f'tar c{codec} -C {q_rp} -T "$T" -f - ; rc=$?; rm -f "$T"; exit $rc'
    )


def _fallback_remote_cmd(
    remote_path: str,
    include_globs: Sequence[str] | None,
    exclude: Sequence[str] | None,
    codec_flag: str | None,
) -> str:
    """The remote command for the manifest-less fallback: server-side filtered ``find | tar c``.

    Still honors the include allowlist server-side (the 1000x lever) even when
    the delta manifest is unavailable — just without the delta/resume. ``find``
    enumerates null-delimited names into a temp file that ``tar c --null -T``
    consumes (archive to stdout).

    The enumeration lands in a temp file rather than piping ``find | tar`` so a
    failed enumeration cannot be masked: a pipe's exit status is the RIGHT side's
    (login shells default ``pipefail`` off), so a find that hits a
    permission-denied / stale-NFS subdir SKIPS that subtree, exits non-zero, yet
    the pipe reads as success — a PARTIAL archive lands as ``ok=True`` and
    finished tasks silently vanish from the aggregate. The ``&&`` short-circuit
    makes a non-zero find abort BEFORE tar streams a truncated set, and
    ``exit $rc`` propagates find's (or tar's) real status. Still one ssh
    connection — one login node — like ``_batch_remote_cmd``.
    """
    q_rp = shlex.quote(remote_path)
    predicate = _find_filter_predicate(include_globs, exclude)
    codec = f" {codec_flag}" if codec_flag else ""
    return (
        f'cd {q_rp} && T="$(mktemp)" && '
        f'find . -type f{predicate} -print0 > "$T" && '
        f'tar c{codec} --null --no-recursion -T "$T" -f - ; rc=$?; rm -f "$T"; exit $rc'
    )


def tar_ssh_pull(
    *,
    ssh_target: str,
    remote_path: str,
    local_path: Path,
    include_globs: Sequence[str] | None = None,
    exclude: Sequence[str] | None = None,
    timeout: float | None = None,
) -> PullResult:
    """Pull *remote_path*'s (filtered) contents into *local_path* — the batched, resumable pull.

    The rsync-less pull analogue of the batched push (latency ranks 2 + 7):

    1. A remote hash manifest enumerates + hashes only the include/exclude
       matching files under *remote_path* (server-side filtering — the 1000x
       lever — in one round trip).
    2. That manifest is diffed against the files already present under
       *local_path*; already-identical files are skipped.
    3. The remainder is fetched in bounded, resumable batches that land directly
       in *local_path* — a died pull's landed batches are already present, so a
       re-call's delta pulls only what is still missing.

    When the remote can't produce a manifest (no ``python3``, an absent tree, a
    cap breach), it falls back to a single server-side-filtered ``find | tar c``
    pull (still filtered, no delta). The tar stream is gzip/zstd compressed by
    default (:func:`hpc_agent.infra.ssh_options.tar_stream_flag`).

    *local_path* receives the CONTENTS of *remote_path* (relpaths preserved) —
    the same contents-only layout ``rsync_pull``'s trailing-slash source uses.
    Returns a :class:`PullResult`; never raises for a remote-command failure
    (reported via ``ok=False`` + ``stderr_tail``), only ``TimeoutError`` on a
    hard client timeout.
    """
    from hpc_agent.infra.manifest import manifest_delta

    # Per-host connection-rate guard (ban-driver); no-op unless HPC_SSH_SAFE_INTERVAL>0.
    throttle_connection(ssh_target)
    validate_remote_path(remote_path.rstrip("/"))
    local_path = Path(local_path)
    local_path.mkdir(parents=True, exist_ok=True)
    effective_timeout = RSYNC_TIMEOUT_SEC if timeout is None else timeout
    codec_flag = tar_stream_flag()

    remote_manifest = _remote_pull_manifest(
        ssh_target=ssh_target,
        remote_path=remote_path,
        include_globs=include_globs,
        exclude=exclude,
        timeout=effective_timeout,
    )

    if remote_manifest is None:
        # Fallback: manifest-less, but STILL server-side filtered (no delta).
        remote_cmd = _fallback_remote_cmd(remote_path, include_globs, exclude, codec_flag)
        with contextlib.suppress(Exception):
            print(
                f"[transport] pull: remote hash manifest unavailable — server-side "
                f"filtered find|tar {tar_stream_codec()} pull of {remote_path} (no delta).",
                file=sys.stderr,
            )
        proc = _pull_transfer_with_retry(
            ssh_target=ssh_target,
            remote_cmd=remote_cmd,
            local_path=local_path,
            codec_flag=codec_flag,
            total_bytes=0,
            timeout=effective_timeout,
        )
        if proc.returncode != 0:
            return PullResult(
                ok=False,
                files_pulled=0,
                bytes_pulled=0,
                skipped_unchanged=0,
                stderr_tail=(proc.stderr or "")[-4000:],
            )
        files, nbytes = _count_landed(local_path, include_globs, exclude)
        return PullResult(
            ok=True,
            files_pulled=files,
            bytes_pulled=nbytes,
            skipped_unchanged=0,
            stderr_tail="",
        )

    # Delta: diff the remote (filtered) manifest against what is already local.
    remote_paths = [e.path for e in remote_manifest.entries]
    local_manifest = _local_present_manifest(local_path, remote_paths)
    # manifest_delta(local=remote, remote=local).to_ship == remote files not
    # already identical locally == exactly the set to PULL.
    delta = manifest_delta(remote_manifest, local_manifest)
    pull = list(delta.to_ship)
    sizes = {e.path: e.size for e in remote_manifest.entries}
    pull_bytes = sum(sizes.get(p, 0) for p in pull)
    n_remote = len(remote_manifest.entries)
    _disclose_pull_mode(
        n_pull=len(pull),
        n_remote=n_remote,
        n_skip=n_remote - len(pull),
        pull_bytes=pull_bytes,
    )

    if not pull:
        return PullResult(
            ok=True,
            files_pulled=0,
            bytes_pulled=0,
            skipped_unchanged=n_remote,
            stderr_tail="",
        )

    max_files, max_bytes, max_name_bytes = _pull_batch_caps()
    batches = list(
        _pull_ship_batches(
            pull,
            sizes,
            max_files=max_files,
            max_bytes=max_bytes,
            max_name_bytes=max_name_bytes,
        )
    )
    landed_files = 0
    landed_bytes = 0
    for i, batch in enumerate(batches, start=1):
        batch_bytes = sum(sizes.get(p, 0) for p in batch)
        _disclose_pull_batch(
            index=i, total=len(batches), n_files=len(batch), batch_bytes=batch_bytes
        )
        remote_cmd = _batch_remote_cmd(remote_path, batch, codec_flag)
        proc = _pull_transfer_with_retry(
            ssh_target=ssh_target,
            remote_cmd=remote_cmd,
            local_path=local_path,
            codec_flag=codec_flag,
            total_bytes=batch_bytes,
            timeout=effective_timeout,
        )
        if proc.returncode != 0:
            # This batch did not land; earlier batches DID (each landed directly
            # in the destination). A retry's delta re-derives them and fetches
            # only the remainder — so return the partial progress, not zero.
            return PullResult(
                ok=False,
                files_pulled=landed_files,
                bytes_pulled=landed_bytes,
                skipped_unchanged=n_remote - len(pull),
                stderr_tail=(proc.stderr or "")[-4000:],
            )
        landed_files += len(batch)
        landed_bytes += batch_bytes

    return PullResult(
        ok=True,
        files_pulled=landed_files,
        bytes_pulled=landed_bytes,
        skipped_unchanged=n_remote - len(pull),
        stderr_tail="",
    )
