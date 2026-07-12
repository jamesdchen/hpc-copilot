"""Shared I/O primitives for the primitives layer.

Currently exposes :func:`atomic_locked_update`, the read-modify-write
helper used by :mod:`hpc_agent.state.runtime_prior` to mutate JSON
documents under a ``fcntl`` advisory lock with an atomic rename.

The helper deliberately keeps a tight API:

- ``path`` — the JSON document on disk.
- ``mutate`` — a callable receiving the parsed doc (``dict``) or ``None``
  if the file is missing / unreadable / not a JSON object, and returning
  the new document to write.

Callers handle their own schema defaults inside ``mutate``. This keeps
the helper agnostic to per-domain schema fields (``schema_version``,
``profile``, ``cluster``, etc.) and avoids overloading the signature.

The lock is a REAL cross-process exclusion on every platform we ship
to: :func:`advisory_flock` backs it with the ``filelock`` library
(``fcntl.flock`` on POSIX, ``msvcrt`` byte-range locking on native
Windows under the hood), so concurrent writers serialize identically
and no update is silently lost on win32.

Also exposes :func:`atomic_write_json` — the canonical
crash-durable JSON writer (tempfile + fsync + replace + parent-dir
fsync). v2 audit found six modules with subtly different inline
copies; this is the one all of them should call.

Two siblings share that recipe for the non-JSON shapes the
bug-sweep-2026-07 generator G12 ("bare-writes-vs-one-atomic-discipline")
named:

- :func:`atomic_write_text` — a pre-serialized string writer for durable
  artifacts whose exact on-disk bytes are load-bearing (a content-addressed
  pack manifest whose sha IS its identity; external ``settings.json`` /
  ``.claude.json`` the tool does not own and must never truncate). ``newline=""``
  so the bytes round-trip exactly.
- :func:`atomic_replace_path` — a context manager yielding a temp sibling path
  for durable artifacts a *third-party writer* builds by path (the dossier
  ``ZipFile(archive_path, "w")`` seal), swapping it into place atomically on a
  clean exit so a crash mid-build never destroys the previously-sealed file.

The lint ``scripts/lint_atomic_durable_writes.py`` is the enforcement row: a
truncating ``write_text``/``ZipFile(_, "w")`` to a durable artifact must route
through one of these three.
"""

from __future__ import annotations

__all__ = [
    "atomic_locked_update",
    "advisory_flock",
    "append_jsonl_line",
    "atomic_write_json",
    "atomic_write_text",
    "atomic_replace_path",
]

import contextlib
import json
import os
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def _replace_with_retry(src: str, dst: Path) -> None:
    """``os.replace(src, dst)`` with a short bounded retry on Windows.

    POSIX renames atomically over an open destination; Windows has no
    equivalent, so if *dst* is momentarily held open by another
    thread/process the replace raises ``PermissionError`` ([WinError 5],
    a sharing violation). A brief backoff lets the colliding handle close.
    Outside win32 the first failure propagates unchanged — byte-identical
    to a bare ``os.replace``.
    """
    for attempt in range(5):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def _fsync_dir(directory: Path) -> None:
    """Best-effort ``fsync`` of *directory* so a rename into it is durable.

    NFS and some other network filesystems refuse to open a directory for
    fsync; the OSError is suppressed (best-effort), matching the historical
    inline copies this consolidates.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write_json(path: Path, payload: Any, *, fsync: bool = True) -> None:
    """Write *payload* as JSON to *path* atomically and (optionally) durably.

    Recipe: serialize to a ``tempfile.mkstemp``-allocated sibling,
    ``flush()`` + ``fsync(fd)`` the data, ``os.replace`` to swap, then
    ``fsync`` the parent directory so the rename is durable across a
    kernel panic / power loss.

    Tempfile name is randomised by ``mkstemp`` so concurrent writers
    don't collide. Parent-dir fsync is best-effort: NFS and some other
    network filesystems don't support it; we suppress the OSError.

    Durability tradeoff (``fsync``)
    -------------------------------
    Set ``fsync=False`` ONLY when the file is a non-authoritative
    derived cache that can be regenerated from a separately-persisted
    source of truth, and you've already paid (or are about to pay) the
    fsync cost on that source.

    With ``fsync=False`` the call is still **atomic** (mkstemp + replace
    means readers never observe a half-written file) but no longer
    **durable**: on a kernel panic / power loss between the
    ``os.replace`` and the OS background-flush, the file may revert to
    its previous contents (or to a missing-file state if it's a new
    write). The page-cache view stays consistent, so a process that
    survives the crash sees the new content immediately.

    The motivating case is the hot monitor-tick path: each tick writes
    a journal record (durable, must fsync) AND a denormalized
    ``<run_id>.last_status.json`` cache file (a strict subset of the
    journal record's ``last_status`` field). On networked filesystems
    each fsync is hundreds of ms; pairing a durable write with a
    no-fsync cache write halves the per-tick fsync cost. If the cache
    file is lost to a crash, the next status poll rewrites it from the
    still-durable journal record.

    Used by:
      * ``state/run_record._atomic_write_json`` (deprecated forwarder
        — back-compat for callers that still import it).
      * ``state/runs.py`` for per-run sidecars.
      * ``ops/monitor/update_constraints.py`` for the journal hand-off.
      * ``ops/monitor/status.py`` for the ``last_status.json`` cache
        (``fsync=False`` — see tradeoff above).
      * ``infra/inspect/_persist.py`` for the cluster-history file.
      * ``mapreduce/metrics_io.py`` for per-task metrics JSONs.
      * ``mapreduce/combiner.py`` and ``mapreduce/dispatch.py`` keep
        inline copies because they're deployed cluster-side without
        the rest of the package — but their recipes mirror this one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            if fsync:
                with contextlib.suppress(OSError):
                    os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
        if fsync:
            _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str, *, fsync: bool = True) -> None:
    """Write the pre-serialized string *text* to *path* atomically and durably.

    The text sibling of :func:`atomic_write_json`, for durable artifacts whose
    EXACT on-disk bytes are load-bearing and must be preserved verbatim — a
    content-addressed pack manifest whose raw-bytes sha is its bind identity, or
    an external ``settings.json`` / ``.claude.json`` the tool does not own and
    must never leave truncated. The caller has already produced the canonical
    string (its own ``json.dumps(..., sort_keys=..., indent=...)`` + trailing
    newline); this writer does not re-serialize, so the bytes round-trip exactly.

    Recipe is identical to :func:`atomic_write_json` — a ``mkstemp`` sibling,
    ``flush`` + ``fsync`` the data, ``os.replace`` to swap, ``fsync`` the parent
    dir — so a kill / power loss mid-write leaves either the previous file or the
    new one, never a torn one. ``newline=""`` disables newline translation so an
    embedded ``"\n"`` is not rewritten to ``"\r\n"`` on win32.

    ``fsync=False`` keeps the write atomic but drops durability — see the
    :func:`atomic_write_json` tradeoff note; use it only for a regenerable cache.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            if fsync:
                with contextlib.suppress(OSError):
                    os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
        if fsync:
            _fsync_dir(path.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def atomic_replace_path(path: Path, *, fsync: bool = True) -> Iterator[Path]:
    """Yield a temp sibling of *path*; on a clean exit swap it in atomically.

    The by-path sibling of :func:`atomic_write_json`, for durable artifacts a
    third-party writer builds *by path* rather than from a serialized payload —
    the dossier ``ZipFile(archive_path, "w")`` seal is the motivating case. The
    caller writes the whole artifact to the yielded temp path (``ZipFile`` on it,
    ``shutil.copy`` into it, …); on a clean exit the temp file is ``fsync``-ed and
    ``os.replace``-d over *path*, then the parent dir is ``fsync``-ed. On ANY
    exception (or a SIGKILL — the temp path is randomized and left for the OS to
    reap) the previously-sealed *path* is untouched, closing the truncate window
    that ``ZipFile(path, "w")`` opens the instant it truncates in place.

    ``mkstemp`` allocates and opens the temp file; the fd is closed immediately
    so the caller can re-open the path (``ZipFile`` needs a path, not an fd). The
    temp file therefore exists (empty) when yielded, so a caller that writes
    nothing still produces a valid empty replacement rather than failing the
    swap.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    tmp_path = path.parent / os.path.basename(tmp)
    try:
        yield tmp_path
        if fsync:
            with contextlib.suppress(OSError):
                fh = os.open(str(tmp_path), os.O_RDONLY)
                try:
                    os.fsync(fh)
                finally:
                    os.close(fh)
        _replace_with_retry(str(tmp_path), path)
        if fsync:
            _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def append_jsonl_line(path: Path, record: dict[str, Any], *, sort_keys: bool = True) -> None:
    """Append one JSON object as a line to *path* under an exclusive flock.

    The canonical JSONL-append discipline every append-only ledger in the
    package routes through — the decision journal / decision briefs / scope
    look ledger (``state/*``) AND the guaranteed-harvest marker
    (``ops/monitor/harvest_guard``). One definition so the torn-line hazard
    is fixed once:

    * **append-only** — opens in ``"a"`` mode, so a write can never rewrite
      or truncate a prior record.
    * **whole-line-atomic** — the advisory ``flock`` (real cross-process
      exclusion on POSIX and win32, see :func:`advisory_flock`) serializes
      concurrent appenders so two writers can't interleave bytes on one line.
    * **crash-durable** — the line is ``flush``-ed and ``fsync``-ed so a
      source-of-truth record survives a crash mid-write.

    ``sort_keys`` defaults to True (stable on-disk key order); pass ``default=str``
    is applied unconditionally so non-JSON-native values (``Path``, datetimes)
    serialize rather than raising. Can raise ``OSError`` — a caller whose
    contract is never-raise (e.g. a ``finally``-time harvest marker) must wrap
    the call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=sort_keys, default=str) + "\n"
    lock_path = path.with_suffix(path.suffix + ".lock")
    with advisory_flock(lock_path, timeout_sec=120.0), path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())


def _read_json_doc(path: Path) -> dict[str, Any] | None:
    """Read *path* and return the parsed JSON dict, or ``None`` on any
    read / decode / shape error. Mirrors the ``_read_doc`` fall-through
    behaviour of runtime_prior — we never raise on a corrupt file
    because refusing to plan is worse than ignoring it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def atomic_locked_update(
    path: Path,
    mutate: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> dict[str, Any]:
    """Acquire ``path``'s advisory lock, read the current doc, apply
    ``mutate``, and atomically replace ``path`` with the returned doc.

    The lock is taken via :func:`advisory_flock` on a sibling
    ``<path>.lock`` sentinel, so it is a REAL cross-process exclusion on
    every platform: the ``filelock`` library dispatches to
    ``fcntl.flock`` on POSIX and ``msvcrt`` byte-range locking on native
    Windows. Concurrent writers therefore serialize identically on
    win32 — no update is silently lost.

    The read happens **inside** the lock so concurrent writers see a
    serialized view. Returns the new document so callers can use the
    written value without a second read.

    ``mutate`` receives the parsed document (``dict``) or ``None`` if
    the file is absent / unreadable / malformed. The callable must
    return a fresh ``dict`` to write — mutating the input in place and
    returning it is also fine.

    The atomic-replace path uses :class:`tempfile.NamedTemporaryFile`
    + :func:`os.fsync` (file + parent dir) + :func:`os.replace` so a
    crash mid-write leaves either the previous doc or the new doc on
    disk, never a partial one. The parent-dir fsync mirrors the
    :func:`atomic_write_json` recipe — without it, a power loss between
    ``os.replace`` and the kernel's background dirent flush could
    silently revert the rename, surfacing as a lost campaign-cursor
    bump / lost sidecar mutation / lost runtime-prior sample.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    # Route the exclusion through advisory_flock — the one cross-platform
    # lock (filelock: fcntl.flock on POSIX, msvcrt byte-range on win32).
    # Blocking, so it always yields True; the read-modify-write happens
    # inside the hold.
    with advisory_flock(lock_path, timeout_sec=120.0):
        existing = _read_json_doc(path)
        new_doc = mutate(existing)
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - manual cleanup in try/finally below
            "w",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            encoding="utf-8",
        )
        try:
            json.dump(new_doc, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            _replace_with_retry(tmp.name, path)
            _fsync_dir(path.parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise
        finally:
            if not tmp.closed:
                tmp.close()
        return new_doc


@contextlib.contextmanager
def advisory_flock(
    lock_path: Path,
    *,
    blocking: bool = True,
    timeout_sec: float | None = None,
) -> Iterator[bool]:
    """Per-process advisory exclusive lock around a code block.

    Yields ``True`` if the lock was acquired, ``False`` if *blocking*
    is False and another process held it. Backed by the ``filelock``
    library, which dispatches to ``fcntl.flock`` on POSIX and
    ``msvcrt.locking`` (byte-range lock) on native Windows — both give
    real cross-process exclusion, so the lock is NOT a no-op on any
    platform we ship to.

    Why the OSS library instead of the hand-rolled msvcrt/fcntl branches
    it replaces: this lock is commodity substrate with a maintained
    library and a two-incident local history. The win32 branch was a
    permissions-only **no-op** until ``12043d0d`` added the msvcrt
    byte-range lock (the campaign multi-cluster deploy race), and
    :func:`atomic_locked_update` was entirely **lockless** on win32 until
    ``1f368163`` routed it through here — both silent cross-process
    serialization losses that only fired on Windows. The same doctrine
    that outsourced SSH to ``asyncssh`` applies: hand-rolled platform
    locking earns its way out to ``filelock``.

    Use case: serialize parallel ``submit-flow`` calls from different
    shells targeting the same cluster (``~/.claude/hpc/<repo>/.submit_lock``).
    The lock is advisory — only callers who lock the same path
    coordinate; the underlying file system operations remain unprotected.

    Semantics preserved exactly across the migration:

    - **Blocking** (``blocking=True``) waits indefinitely
      (``filelock`` ``timeout=-1``), matching the old ``fcntl.LOCK_EX`` /
      msvcrt spin.
    - **Non-blocking** (``blocking=False``) tries once and yields
      ``False`` on contention (``filelock`` ``timeout=0`` → ``Timeout``).
    - **Not re-entrant across calls.** A fresh :class:`filelock.FileLock`
      is built per call, so a second ``advisory_flock`` on the same path
      — same process or cross-process — contends at the OS lock exactly
      like the old separate-fd design (verified: two same-process
      instances on one path refuse each other; the two guard-fires tests
      ``test_advisory_flock_serializes_cross_process_win32`` and
      ``test_concurrent_writers_serialize`` pin this). ``filelock``'s
      *per-instance* reentrancy counter is therefore never engaged; no
      caller nests a blocking acquire on the same path (the old code would
      have self-deadlocked, so none can rely on reentrancy).

    The lock file itself is created (``mkdir -p`` on the parent) and, as
    with the old code, left in place as a sentinel — never read or
    written. ``filelock``'s Windows backend deletes the file on release,
    so the release path re-touches it to preserve the historical
    lingering-sentinel contract (pinned by
    ``tests/state/test_session.py::test_lock_file_skipped_by_loader`` —
    run-dir loaders must see and skip ``*.lock`` siblings). The touch
    never truncates and the sentinel is never read, so a concurrent
    fresh holder is unaffected. If the process dies, the OS releases the
    lock automatically.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    from filelock import FileLock, Timeout  # noqa: PLC0415 — keep import lazy/local

    # A FRESH instance per call (never reused / singleton): filelock's
    # reentrancy is per-instance, so a new instance opens its own fd and
    # contends with any other holder of the same path — the non-reentrant
    # cross-/same-process exclusion the callers and tests depend on.
    lock = FileLock(str(lock_path))
    # timeout=-1 blocks forever; timeout=0 tries exactly once. Using the
    # numeric timeouts (not the newer ``blocking=`` kwarg) keeps us on the
    # >=3.13 floor's API. ``timeout_sec`` bounds the BLOCKING wait for
    # short-critical-section callers (run-#12 finding 16: a worker sat 15
    # minutes at 0 CPU behind a wedged holder's lock, invisible) — expiry is
    # a LOUD, path-naming TimeoutError, never a silent forever-wait. ``None``
    # keeps the historical infinite wait for legitimately-long holds (the
    # cross-shell .submit_lock spans a whole staging).
    if blocking:
        try:
            lock.acquire(timeout=-1 if timeout_sec is None else timeout_sec)
        except Timeout as exc:
            raise TimeoutError(
                f"advisory lock {lock_path} not acquired within {timeout_sec}s — "
                "the holder is likely wedged or leaked; inspect the sibling "
                ".lease.json for a holder pid, and delete the stale lock only "
                "after confirming that pid is dead"
            ) from exc
    else:
        try:
            lock.acquire(timeout=0)
        except Timeout:
            yield False
            return
    try:
        yield True
    finally:
        lock.release()
        # filelock's Windows backend unlinks the lock file on release;
        # today's contract leaves the sentinel in place (see docstring).
        # O_CREAT without truncate — harmless if a fresh holder re-created
        # it already, no-op on POSIX where filelock keeps the file.
        with contextlib.suppress(OSError):
            lock_path.touch()
