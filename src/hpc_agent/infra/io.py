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
"""

from __future__ import annotations

__all__ = ["atomic_locked_update", "advisory_flock", "atomic_write_json"]

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
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    with contextlib.suppress(OSError):
                        os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # Some filesystems (notably NFS) don't allow opening a dir
                # for fsync; best-effort.
                pass
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


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
    with advisory_flock(lock_path):
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
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    with contextlib.suppress(OSError):
                        os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                # NFS / some network FSes refuse dir fsync; best-effort.
                pass
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
    # >=3.13 floor's API.
    if blocking:
        lock.acquire(timeout=-1)
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
