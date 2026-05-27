"""Pure SHA computations over the user's ``tasks.py`` and materialized tasks.

Extracted from :mod:`hpc_agent.state.runs` so the run-sidecar lifecycle
module can stay focused on path helpers, sidecar I/O, and lifecycle
(find / prune / update). The two functions here are pure: given a
loaded ``tasks_module`` (or a path), they hash and return.

Re-exported from :mod:`hpc_agent.state.runs` for backwards compatibility
with existing callers (``from hpc_agent.state.runs import compute_cmd_sha``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = ["compute_cmd_sha", "compute_tasks_py_sha"]


def compute_cmd_sha(tasks_module: Any) -> str:
    """Materialize the task list and return a deterministic SHA-256.

    Imports the user's ``tasks.py`` module (already loaded by the caller),
    calls ``total()``, then ``resolve(i)`` for every ``i`` in
    ``range(total())``. Each kwargs dict is normalized to sorted-keys JSON
    and the lines are joined with ``\\n`` before hashing. The resulting
    digest is stable across equivalent task lists and changes whenever any
    kwarg dict changes.

    Returns a 64-char hex string.

    Raises
    ------
    AttributeError
        If *tasks_module* lacks ``total`` or ``resolve``.
    TypeError
        If ``resolve(i)`` does not return a dict.
    """
    n = int(tasks_module.total())
    parts: list[str] = []
    for i in range(n):
        kwargs = tasks_module.resolve(i)
        if not isinstance(kwargs, dict):
            raise TypeError(f"tasks.resolve({i}) must return a dict, got {type(kwargs).__name__}")
        parts.append(json.dumps(kwargs, sort_keys=True, separators=(",", ":")))
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_tasks_py_sha(tasks_py_path: Path) -> str:
    """Return SHA-256 of ``tasks.py``'s bytes — diagnostic only."""
    return hashlib.sha256(Path(tasks_py_path).read_bytes()).hexdigest()
