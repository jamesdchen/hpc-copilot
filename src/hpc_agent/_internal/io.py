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

On systems without ``fcntl`` (e.g. native Windows), the helper falls
back to a no-lock atomic write — same behaviour as the original
``_with_locked_doc`` copies.

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
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


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
      * ``_internal/session/run_record._atomic_write_json`` (forwarder
        below — back-compat for callers that still import it).
      * ``state/runs.py`` for per-run sidecars.
      * ``runner/update_constraints.py`` for the journal hand-off.
      * ``runner/status.py`` for the ``last_status.json`` cache
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
        os.replace(tmp, path)
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
    """Acquire ``path``'s flock, read the current doc, apply ``mutate``,
    and atomically replace ``path`` with the returned doc.

    The read happens **inside** the lock so concurrent writers see a
    serialized view. Returns the new document so callers can use the
    written value without a second read.

    ``mutate`` receives the parsed document (``dict``) or ``None`` if
    the file is absent / unreadable / malformed. The callable must
    return a fresh ``dict`` to write — mutating the input in place and
    returning it is also fine.

    The atomic-replace path uses :class:`tempfile.NamedTemporaryFile`
    + :func:`os.fsync` + :func:`os.replace` so a crash mid-write leaves
    either the previous doc or the new doc on disk, never a partial
    one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        import fcntl  # noqa: PLC0415 — POSIX-only import
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
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
            os.replace(tmp.name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise
        finally:
            if not tmp.closed:
                tmp.close()
        return new_doc
    finally:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def advisory_flock(
    lock_path: Path,
    *,
    blocking: bool = True,
) -> Iterator[bool]:
    """Per-process advisory ``fcntl.flock`` around a code block.

    Yields ``True`` if the lock was acquired, ``False`` if *blocking*
    is False and another process held it. On non-POSIX platforms (no
    ``fcntl``), always yields ``True`` — the lock degrades to a
    permissions-only sentinel since we have no real cross-process
    serialization, and the caller is expected to tolerate the race.

    Use case: serialize parallel ``submit-flow`` calls from different
    shells targeting the same cluster (``~/.claude/hpc/<repo>/.submit_lock``).
    The lock is advisory — only callers who flock the same path
    coordinate; the underlying file system operations remain unprotected.

    The lock file itself is created (``mkdir -p`` on the parent) and
    left in place; it's a sentinel, never read or written. If the
    process dies, the kernel releases the flock automatically.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl  # noqa: PLC0415 — POSIX-only import
    except ImportError:
        # Windows / no-fcntl: degrade to no-op. We still touch the file
        # so callers can rely on it existing (e.g. for ad-hoc inspection).
        with contextlib.suppress(OSError):
            lock_path.touch(exist_ok=True)
        yield True
        return

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
