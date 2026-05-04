"""Shared I/O primitives for the primitives layer.

Currently exposes :func:`atomic_locked_update`, the read-modify-write
helper used by :mod:`claude_hpc.orchestrator.runtime_prior` to mutate JSON
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
"""

from __future__ import annotations

__all__ = ["atomic_locked_update"]

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _read_json_doc(path: Path) -> dict[str, Any] | None:
    """Read *path* and return the parsed JSON dict, or ``None`` on any
    read / decode / shape error. Mirrors the ``_read_doc`` fall-through
    behaviour of runtime_prior — we never raise on a corrupt file
    because refusing to plan is worse than ignoring it.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
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
        tmp = tempfile.NamedTemporaryFile(
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
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
        finally:
            if not tmp.closed:
                tmp.close()
        return new_doc
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
