"""Cross-worker lock for the one shared MUTABLE test resource: the packaged bake.

``tests/cli/test_fast_dispatch.py::test_seeded_stale_bake_falls_back_to_walk_byte_identical``
poisons ``src/hpc_agent/operations.json`` IN PLACE (restoring in ``finally``)
to prove the content-key trust gate refuses a stale bake. Under xdist every
worker shares that file on disk, so any test whose ASSERTIONS consume bake
content must not overlap the poison window. The 2026-07-17 CI red on
``e41f25e2`` (py3.12 slow tier) was exactly this race: the forced-bake
byte-identity subprocess in another worker read the sentinel mid-window and
reported ``fast/full drift`` with ``STALE-BAKE-POISON-DO-NOT-SERVE`` in the
baked answer — code was innocent both times.

Discipline: the WRITER holds :func:`bake_file_lock` for its whole
mutate→restore span; every content-READER holds it across the reads its
assertions depend on. An ``xdist_group`` mark would only work under
``--dist loadgroup``; a file lock holds under any dist mode and any runner.

The lock file lives in the system temp dir, keyed by a hash of the resolved
bake path, so (a) no stray file lands in the source tree and (b) two
different checkouts never contend with each other.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path

from filelock import FileLock

__all__ = ["bake_file_lock"]

_LOCK_TIMEOUT_SEC = 600  # generous: the poison window spans two subprocesses


def _lock_path() -> str:
    bake = str(files("hpc_agent") / "operations.json")
    key = hashlib.sha256(bake.encode("utf-8")).hexdigest()[:16]
    return str(Path(tempfile.gettempdir()) / f"hpc_agent_bake_{key}.lock")


@contextmanager
def bake_file_lock() -> Iterator[None]:
    """Hold the cross-process bake lock for the enclosed block."""
    with FileLock(_lock_path(), timeout=_LOCK_TIMEOUT_SEC):
        yield
