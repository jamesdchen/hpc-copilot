"""Per-cluster bad-node blacklist with TTL.

Two halves of the contract:

- **Writer** — ``/hpc-monitor`` (and any other detector) calls
  :func:`record_segv` when a task SEGVs (or otherwise hits a deterministic
  node-fault signature). The function appends an evidence record to
  ``<repo>/.hpc/bad_nodes.<cluster>.json`` and refreshes the entry's
  expiry. Multiple writers are safe via ``fcntl.flock`` + atomic
  rename.

- **Reader** — ``/hpc-submit`` (Phase 4 planner) calls :func:`get_active`
  to obtain the currently-blacklisted nodes for a cluster. Expired
  entries are filtered out and pruned from disk on the next write.

Schema lives in this module (``SCHEMA_VERSION = 1``) and is documented
in the file's top comment + ``docs/cli-spec.md``-class fields. The disk
layout is intentionally simple JSON so a human can audit / hand-edit it
between submits.
"""

from __future__ import annotations

__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_TTL_DAYS",
    "MAX_EVIDENCE_PER_NODE",
    "blacklist_path",
    "record_segv",
    "get_active",
    "read_raw",
    "prune_expired",
]

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION: int = 1
DEFAULT_TTL_DAYS: int = 7
MAX_EVIDENCE_PER_NODE: int = 5


def blacklist_path(experiment_dir: Path, cluster: str) -> Path:
    """Return the canonical blacklist file path for *cluster*."""
    return Path(experiment_dir) / ".hpc" / f"bad_nodes.{cluster}.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _empty_doc() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def _read_doc(path: Path) -> dict[str, Any]:
    """Read the blacklist document; return an empty doc on any read error.

    Refusing to plan because of a corrupt blacklist file would be worse
    than ignoring it. We log nothing here — callers can re-read with
    :func:`read_raw` if they want to inspect the failure mode.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return _empty_doc()
    except OSError:
        return _empty_doc()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return _empty_doc()
    if not isinstance(doc, dict):
        return _empty_doc()
    doc.setdefault("schema_version", SCHEMA_VERSION)
    if not isinstance(doc.get("entries"), list):
        doc["entries"] = []
    return doc


def _atomic_write_locked(path: Path, doc: dict[str, Any]) -> None:
    """Atomically write *doc* to *path* with a flock-guarded swap.

    1. Acquire an exclusive lock on a sidecar lock file.
    2. Write JSON to a temp file in the same directory.
    3. Rename over the target (POSIX-atomic).
    4. Release the lock.

    The lock file is left on disk; subsequent writers reuse it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    # Open the lock; on Windows fcntl is unavailable but this codebase
    # targets POSIX clusters / WSL, so we depend on it.
    try:
        import fcntl  # noqa: PLC0415 — POSIX-only import
    except ImportError:
        fcntl = None  # type: ignore[assignment]
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        # Write to temp + rename.
        tmp = tempfile.NamedTemporaryFile(
            "w",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
            encoding="utf-8",
        )
        try:
            json.dump(doc, tmp, indent=2, sort_keys=True)
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
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _filter_expired(entries: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Drop entries whose ``expires_at`` is unparseable or in the past.

    A missing / corrupt ``expires_at`` is treated as expired (drop it),
    not as immortal — otherwise a single bad write would create a
    permanent blacklist entry.
    """
    kept: list[dict[str, Any]] = []
    for e in entries:
        exp = _parse_iso(e.get("expires_at", ""))
        if exp is not None and exp > now:
            kept.append(e)
    return kept


def read_raw(experiment_dir: Path, cluster: str) -> dict[str, Any]:
    """Return the on-disk document untouched (no TTL filtering).

    Use :func:`get_active` for planner consumption — this helper exists
    for diagnostics and tests.
    """
    return _read_doc(blacklist_path(experiment_dir, cluster))


def prune_expired(experiment_dir: Path, cluster: str) -> int:
    """Drop expired entries from the file. Returns count removed."""
    path = blacklist_path(experiment_dir, cluster)
    doc = _read_doc(path)
    before = len(doc["entries"])
    doc["entries"] = _filter_expired(doc["entries"], _now())
    after = len(doc["entries"])
    if after != before:
        _atomic_write_locked(path, doc)
    return before - after


def get_active(
    experiment_dir: Path,
    cluster: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return active blacklist entries (TTL-filtered, no disk side-effects).

    Each entry includes ``node``, ``added_at``, ``expires_at``, and the
    full ``evidence`` list so the planner / Claude can show "added 8h
    ago, evidence: 1 SEGV with co-tenant <user>" rather than just a
    bare node name.
    """
    doc = _read_doc(blacklist_path(experiment_dir, cluster))
    return _filter_expired(doc["entries"], now or _now())


def record_segv(
    experiment_dir: Path,
    cluster: str,
    *,
    node: str,
    run_id: str,
    job_id: str,
    task_id: int,
    exit_code: int | None = None,
    signal: int | None = None,
    host_allocmem_pct: float | None = None,
    cpu_load_frac: float | None = None,
    concurrent_jobs: list[dict[str, Any]] | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one SEGV (or equivalent fault) on *node*.

    Returns the updated entry. Idempotency: writing twice for the same
    ``(run_id, task_id)`` does not double-count — same evidence record
    is kept once. The TTL is always refreshed to ``now + ttl_days`` so a
    second SEGV resets the clock.

    Multiple-writer safety: the on-disk swap is flock-guarded and uses a
    rename, so concurrent ``/hpc-monitor`` invocations from different
    sessions cannot tear the file.
    """
    if not node:
        raise ValueError("node must be non-empty")
    if not cluster:
        raise ValueError("cluster must be non-empty")
    ts_now = now or _now()
    expires_at = ts_now + timedelta(days=ttl_days)
    path = blacklist_path(experiment_dir, cluster)
    doc = _read_doc(path)

    # Drop expired entries on every write — keeps the file bounded.
    doc["entries"] = _filter_expired(doc["entries"], ts_now)

    # Find existing entry for this node (case-sensitive match).
    target: dict[str, Any] | None = None
    for e in doc["entries"]:
        if e.get("node") == node and e.get("cluster", cluster) == cluster:
            target = e
            break
    if target is None:
        target = {
            "node": node,
            "cluster": cluster,
            "added_at": _iso(ts_now),
            "expires_at": _iso(expires_at),
            "evidence": [],
        }
        doc["entries"].append(target)
    else:
        # Existing entry: refresh expiry. Don't backdate added_at.
        target["expires_at"] = _iso(expires_at)

    # Idempotent evidence append.
    new_ev = {
        "run_id": run_id,
        "job_id": str(job_id),
        "task_id": int(task_id),
        "exit_code": exit_code,
        "signal": signal,
        "ts": _iso(ts_now),
        "host_allocmem_pct": host_allocmem_pct,
        "cpu_load_frac": cpu_load_frac,
        "concurrent_jobs": list(concurrent_jobs or []),
    }
    if not any(
        ev.get("run_id") == new_ev["run_id"] and ev.get("task_id") == new_ev["task_id"]
        for ev in target["evidence"]
    ):
        target["evidence"].append(new_ev)
    # Cap the list to the most-recent N entries.
    target["evidence"] = target["evidence"][-MAX_EVIDENCE_PER_NODE:]

    _atomic_write_locked(path, doc)
    return target
