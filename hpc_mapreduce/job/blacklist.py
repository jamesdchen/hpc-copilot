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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hpc_mapreduce._io import atomic_locked_update
from hpc_mapreduce._time import parse_iso_utc_or_none, utcnow

SCHEMA_VERSION: int = 1
DEFAULT_TTL_DAYS: int = 7
MAX_EVIDENCE_PER_NODE: int = 5


def blacklist_path(experiment_dir: Path, cluster: str) -> Path:
    """Return the canonical blacklist file path for *cluster*.

    Forwarder for ``RepoLayout(experiment_dir).blacklist(cluster)``.
    The layout class resolves *experiment_dir* (so a writer invoking
    from a child directory and a reader invoking from the project root
    see the same file).
    """
    from hpc_mapreduce.layout import RepoLayout

    return RepoLayout(experiment_dir).blacklist(cluster)


def _now() -> datetime:
    return utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


_parse_iso = parse_iso_utc_or_none


def _empty_doc() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def _normalise(doc: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise a parsed blacklist doc (or ``None``) to a well-shaped
    dict with ``schema_version`` and a list ``entries``.
    """
    if not isinstance(doc, dict):
        return _empty_doc()
    doc.setdefault("schema_version", SCHEMA_VERSION)
    if not isinstance(doc.get("entries"), list):
        doc["entries"] = []
    return doc


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
    return _normalise(doc)


def _atomic_write_locked(path: Path, doc: dict[str, Any]) -> None:
    """Atomically write *doc* to *path* with a flock-guarded swap.

    Backwards-compat shim retained for tests / external callers. Prefer
    :func:`atomic_locked_update` for new code so the read happens inside
    the lock — otherwise two concurrent writers can each read a stale
    doc, mutate independently, and one's update will silently overwrite
    the other's.
    """
    atomic_locked_update(path, lambda _existing: doc)


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
    """Drop expired entries from the file. Returns count removed.

    The whole read-filter-write happens inside the per-file flock so a
    concurrent ``record_segv`` cannot resurrect a just-pruned entry.
    """
    path = blacklist_path(experiment_dir, cluster)
    counts = {"removed": 0}

    def _mutate(raw: dict[str, Any] | None) -> dict[str, Any]:
        doc = _normalise(raw)
        before = len(doc.get("entries", []))
        doc["entries"] = _filter_expired(doc.get("entries", []), _now())
        counts["removed"] = before - len(doc["entries"])
        return doc

    atomic_locked_update(path, _mutate)
    return counts["removed"]


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

    # Capture the target across the lock so we can return it.
    target_box: dict[str, Any] = {}

    def _mutate(raw: dict[str, Any] | None) -> dict[str, Any]:
        doc = _normalise(raw)
        # Drop expired entries on every write — keeps the file bounded.
        doc["entries"] = _filter_expired(doc.get("entries", []), ts_now)

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
        target["evidence"] = target["evidence"][-MAX_EVIDENCE_PER_NODE:]
        target_box["target"] = target
        return doc

    atomic_locked_update(path, _mutate)
    target: dict[str, Any] = target_box["target"]
    return target
