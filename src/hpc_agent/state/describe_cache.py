"""Disk cache of ``hpc-agent describe <name>`` output, keyed by version (#261).

``describe`` output is *framework-stable*: for a given installed ``hpc_agent``
version, ``describe <name>`` returns the same bytes every time — there is no
per-invocation input beyond the name. Yet the orchestrator issues many
``describe`` calls per workflow, each forking a Python subprocess + loading the
registry (~100-500ms). This memoizes the resolved ``data`` payload to disk,
keyed by ``(pkg_version, name)``:

    ~/.claude/hpc/describe_cache/<pkg_version>/<name>.json

A hit skips the registry load entirely. Keying by package version means a
``pip install -U`` lands in a fresh directory (automatic invalidation); old
version dirs are harmless kilobyte debris. ``HPC_NO_DESCRIBE_CACHE=1`` bypasses
the cache (for development on the describe path itself). The cache is
opportunistic — any I/O error falls through to the live path, never raising.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["cache_disabled", "load", "store"]


def cache_disabled() -> bool:
    """True when ``HPC_NO_DESCRIBE_CACHE=1`` opts the cache out."""
    return os.environ.get("HPC_NO_DESCRIBE_CACHE") == "1"


def _pkg_version() -> str:
    """Installed ``hpc-agent`` version, or a stable placeholder when absent."""
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("hpc-agent", "hpc_agent"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0+unknown"


# describe names are validated (lowercase letters / digits / hyphens) before we
# ever reach here, but sanitise defensively so the name can never escape the
# version dir into a traversal path.
_SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def _cache_path(name: str) -> Path | None:
    """Cache file for *name* under the version dir, or ``None`` if name is unsafe."""
    if not _SAFE_NAME.match(name):
        return None
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "describe_cache" / _pkg_version() / f"{name}.json"


def load(name: str) -> dict[str, Any] | None:
    """Return the cached ``describe`` data payload for *name*, or ``None``.

    ``None`` on cache-disabled, miss, unsafe name, or any read/parse error —
    every "not a clean hit" case collapses to "compute it live".
    """
    if cache_disabled():
        return None
    path = _cache_path(name)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def store(name: str, data: dict[str, Any]) -> None:
    """Cache the ``describe`` data payload for *name* (best-effort, no-op if disabled)."""
    if cache_disabled():
        return
    path = _cache_path(name)
    if path is None:
        return
    from hpc_agent.infra.io import atomic_write_json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except OSError:
        pass
