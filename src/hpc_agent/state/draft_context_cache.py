"""Content-keyed disk cache for ``notebook-draft-context`` (describe-cache kin).

The draft-context projection is a pure function of its inputs — the template + the
files under the declared roots. Recomputing it (parse every engine file, walk
every source tree for call sites, hash every inventoried file, render markdown) is
cheap-ish but not free, and the drafting loop reads it repeatedly. This memoizes
the resolved result to disk, keyed by a CONTENT FINGERPRINT of every input:

    ~/.claude/hpc/draft_context_cache/<fingerprint>.json

The fingerprint is a sha256 over the spec, the package version, and a ``(size,
mtime_ns)`` stat of the template + every file under the declared roots + the data
manifest (the ``(size, mtime)`` fast-path the data-manifest cache also uses —
re-checks never re-hash unchanged gigabytes). A stat change moves the fingerprint,
so the cache is recompute-on-read: a stale key simply misses and the caller
recomputes. Disposable — an old fingerprint file is harmless kilobyte debris.

``HPC_NO_DRAFT_CONTEXT_CACHE=1`` bypasses the cache (development on the projection
itself). Opportunistic: any I/O error falls through to the live path, never raises.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

__all__ = ["cache_disabled", "fingerprint", "load", "store"]

_CACHE_SUBDIR = "draft_context_cache"


def cache_disabled() -> bool:
    """True when ``HPC_NO_DRAFT_CONTEXT_CACHE=1`` opts the cache out."""
    return os.environ.get("HPC_NO_DRAFT_CONTEXT_CACHE") == "1"


def _pkg_version() -> str:
    """Installed ``hpc-agent`` version, or a stable placeholder when absent.

    Keying the fingerprint on the version means a code change to the render /
    resolution invalidates every entry on ``pip install -U`` (the describe-cache
    posture) without any explicit bump.
    """
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("hpc-agent", "hpc_agent"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0+unknown"


def _stat_pair(path: Path) -> list[int] | None:
    """``[size, mtime_ns]`` for *path*, or ``None`` if it cannot be stat'd."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def fingerprint(spec_key: dict[str, Any], stat_files: list[Path]) -> str:
    """Content fingerprint over the spec + package version + file stats.

    *spec_key* is the resolved spec (template + effective roots + audit_id);
    *stat_files* is every file whose bytes feed the projection (template, engine
    files, inventoried files, data manifest). Missing files stat to ``None`` — a
    file appearing or vanishing moves the fingerprint too.
    """
    stats = {path.as_posix(): _stat_pair(path) for path in stat_files}
    payload = {
        "spec": spec_key,
        "pkg_version": _pkg_version(),
        "stats": stats,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _cache_path(key: str) -> Path | None:
    """Cache file for fingerprint *key*, or ``None`` if the key is unsafe."""
    if not (len(key) == 64 and all(c in "0123456789abcdef" for c in key)):
        return None
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / _CACHE_SUBDIR / f"{key}.json"


def load(key: str) -> dict[str, Any] | None:
    """Return the cached result payload for fingerprint *key*, or ``None``.

    ``None`` on cache-disabled, miss, unsafe key, or any read/parse error — every
    "not a clean hit" case collapses to "compute it live".
    """
    if cache_disabled():
        return None
    path = _cache_path(key)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store(key: str, data: dict[str, Any]) -> None:
    """Cache the result payload for fingerprint *key* (best-effort, no-op if disabled)."""
    if cache_disabled():
        return
    path = _cache_path(key)
    if path is None:
        return
    from hpc_agent.infra.io import atomic_write_json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except OSError:
        pass
