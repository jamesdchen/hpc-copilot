"""Tree-fingerprint cache for ``discover-runs`` (#264).

``discover-runs`` AST-walks the experiment tree for ``@register_run`` functions
on every call; within a session the same tree is scanned dozens of times. This
caches the result, keyed by a fingerprint of the tree's ``.py`` / ``.ipynb``
files.

Unlike #255 (preflight) / #261 (describe), the key is NOT a single directory
mtime: ``discover-runs`` is a *recursive* source scan, so a single root
``stat`` would miss a nested edit and return stale results. The fingerprint
instead hashes ``(relpath, mtime_ns, size)`` over every candidate file in the
tree — any add / edit / delete changes it. Stat-ing the files is far cheaper
than AST-parsing each one, so a hit still wins.

Opportunistic: ``RunInfo`` carries ``Flag`` specs whose ``type`` is a Python
type object and whose ``default`` / ``choices`` are arbitrary — not always
JSON-round-trippable. Any (de)serialisation problem collapses to "no cache" /
"miss", falling back to the live scan, so the cache is never wrong, only
sometimes skipped. ``HPC_NO_DISCOVER_CACHE=1`` disables it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent.experiment_kit.discover import RunInfo

__all__ = ["cache_disabled", "load", "store"]

# Directories never holding user run-source — pruned from the fingerprint walk
# to keep it cheap. This MUST stay a subset of the set the actual
# ``discover-runs`` source scan skips (``state.discover._SKIP_DIRS`` /
# ``experiment_kit.discover._SKIP_DIRS``): pruning a directory here that the scan
# still reads would let a ``@register_run`` edit under it change live results
# WITHOUT changing the fingerprint, serving a stale cache. The scan skips this
# exact set (vendored / VCS / cache dirs), so the pruning is safe; a contract
# test pins the subset invariant.
_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".hpc", ".claude"}
)
_CANDIDATE_SUFFIXES = (".py", ".ipynb")

# Flag.type is a Python type object; map the JSON-safe common types by name.
_TYPE_TO_NAME = {str: "str", int: "int", float: "float", bool: "bool", type(None): "none"}
_NAME_TO_TYPE = {"str": str, "int": int, "float": float, "bool": bool, "none": type(None)}


def cache_disabled() -> bool:
    """True when ``HPC_NO_DISCOVER_CACHE=1`` disables the cache."""
    return os.environ.get("HPC_NO_DISCOVER_CACHE") == "1"


def _cache_path() -> Path:
    from hpc_agent.state.run_record import _current_homedir

    return _current_homedir() / "_discover_cache.json"


def _lock_path(target: Path) -> Path:
    """Sibling ``.lock`` path for *target* — the same convention as
    :func:`hpc_agent.state.run_record._lock_path` (``<name>.lock``)."""
    return target.with_suffix(target.suffix + ".lock")


def _tree_fingerprint(experiment_dir: Path) -> str:
    """Hash of ``(relpath, mtime_ns, size)`` over every ``.py`` / ``.ipynb`` file.

    Any add / edit / delete anywhere in the tree changes the digest, so a hit is
    only served when the source the scan would read is byte-identical.
    """
    root = Path(experiment_dir)
    parts: list[tuple[str, int, int]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(_CANDIDATE_SUFFIXES):
                continue
            fp = Path(dirpath) / name
            try:
                st = fp.stat()
            except OSError:
                continue
            parts.append((str(fp.relative_to(root)), st.st_mtime_ns, st.st_size))
    parts.sort()
    blob = json.dumps(parts, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def _flag_to_dict(flag: Any) -> dict[str, Any]:
    type_name = _TYPE_TO_NAME.get(flag.type)
    if type_name is None and flag.type is not None:
        # An exotic type object we can't round-trip — refuse to cache this tree.
        raise TypeError(f"uncacheable Flag.type {flag.type!r}")
    return {
        "name": flag.name,
        "type": type_name,
        "default": flag.default,
        "required": flag.required,
        "choices": list(flag.choices) if flag.choices is not None else None,
        "help": flag.help,
        "nargs": flag.nargs,
        "action": flag.action,
    }


def _run_info_to_dict(ri: Any) -> dict[str, Any]:
    return {
        "path": str(ri.path),
        "name": ri.name,
        "gpu": ri.gpu,
        # #293: multi-rank marker. ``getattr`` default keeps an older in-memory
        # RunInfo (pre-field) serialising cleanly; the read side defaults too.
        "mpi": getattr(ri, "mpi", False),
        # Same forward-compat shape as ``mpi``: an older in-memory RunInfo
        # without the field serialises cleanly, and the read side defaults too.
        "has_var_keyword": getattr(ri, "has_var_keyword", False),
        "run_signature_sha": ri.run_signature_sha,
        "flags": [_flag_to_dict(f) for f in ri.flags],
    }


def _dict_to_run_info(d: dict[str, Any]) -> RunInfo:
    from hpc_agent.executor_cli import Flag
    from hpc_agent.experiment_kit.discover import RunInfo

    flags = tuple(
        Flag(
            name=fd["name"],
            type=_NAME_TO_TYPE[fd["type"]] if fd["type"] is not None else None,
            default=fd["default"],
            required=fd["required"],
            choices=tuple(fd["choices"]) if fd["choices"] is not None else None,
            help=fd["help"],
            nargs=fd["nargs"],
            action=fd["action"],
        )
        for fd in d["flags"]
    )
    return RunInfo(
        path=Path(d["path"]),
        name=d["name"],
        gpu=d["gpu"],
        # Default False for a cache written before the mpi field existed (#293);
        # a stale entry stays readable rather than forcing a full re-scan.
        mpi=d.get("mpi", False),
        flags=flags,
        run_signature_sha=d["run_signature_sha"],
        # Default False for a cache written before this field existed; a stale
        # entry stays readable rather than forcing a full re-scan.
        has_var_keyword=d.get("has_var_keyword", False),
    )


def load(experiment_dir: str | Path) -> list[RunInfo] | None:
    """Return the cached ``discover-runs`` result, or ``None`` (miss / disabled / error).

    A hit requires the stored fingerprint to equal the tree's current
    fingerprint; any read / parse / reconstruct problem returns ``None`` so the
    caller does a live scan.
    """
    if cache_disabled():
        return None
    exp = Path(experiment_dir)
    try:
        with open(_cache_path(), encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = cache.get(str(exp.resolve())) if isinstance(cache, dict) else None
    if not isinstance(entry, dict):
        return None
    if entry.get("fingerprint") != _tree_fingerprint(exp):
        return None
    try:
        return [_dict_to_run_info(d) for d in entry["runs"]]
    except (KeyError, TypeError, ValueError):
        return None


def store(experiment_dir: str | Path, infos: list[RunInfo]) -> None:
    """Cache the ``discover-runs`` *infos* for *experiment_dir* (best-effort).

    A no-op when disabled or when any ``RunInfo`` / ``Flag`` won't serialise
    (e.g. an exotic ``Flag.type`` / ``default``) — the live result is still
    returned by the caller; only the caching is skipped.
    """
    if cache_disabled():
        return
    from hpc_agent.infra.io import advisory_flock, atomic_write_json

    exp = Path(experiment_dir)
    try:
        runs = [_run_info_to_dict(ri) for ri in infos]
        entry = {"fingerprint": _tree_fingerprint(exp), "runs": runs}
        # Round-trip through json now so a non-serialisable default/choices
        # fails HERE (caught) rather than corrupting the cache file.
        json.dumps(entry)
    except (TypeError, ValueError):
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hold the advisory flock across BOTH the read and the write so two
        # concurrent stores for DIFFERENT experiment dirs can't lost-update
        # each other — the same locked-RMW fix ``canary_cache`` /
        # ``preflight_cache`` carry on their shared global files. A
        # lock-acquire or write failure degrades to "not cached" (the caller
        # still returns the live result).
        with advisory_flock(_lock_path(path), timeout_sec=120.0):
            cache: dict[str, Any] = {}
            if path.is_file():
                with open(path, encoding="utf-8") as fh:
                    existing = json.load(fh)
                if isinstance(existing, dict):
                    cache = existing
            cache[str(exp.resolve())] = entry
            atomic_write_json(path, cache)
    except (OSError, ValueError):
        pass
