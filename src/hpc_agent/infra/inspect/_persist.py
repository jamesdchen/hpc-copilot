"""ClusterSnapshot persistence + history reads.

Snapshots are written under ``<exp>/.hpc/cluster_history/<cluster>/<unix_ts>.json``
with bounded growth (oldest-first eviction). The reader yields snapshots
in reverse-chronological order.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import TYPE_CHECKING

from hpc_agent._internal.time import parse_iso_utc_or_none, utcnow

from ._common import ClusterSnapshot, _snapshot_from_dict

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = [
    "MAX_HISTORY_SNAPSHOTS",
    "persist_snapshot",
    "read_cluster_history",
]


# Per-cluster snapshot cap. Same bounded-growth pattern as
# `runtime_prior.MAX_SAMPLES`: the history is advisory not audit, so
# trimming oldest-first is fine. Override via HPC_MAX_CLUSTER_HISTORY.
MAX_HISTORY_SNAPSHOTS: int = int(os.environ.get("HPC_MAX_CLUSTER_HISTORY", "10000"))


def _history_dir(experiment_dir: Path, cluster: str) -> Path:
    from hpc_agent._internal.layout import RepoLayout

    return RepoLayout(experiment_dir).cluster_history(cluster)


def persist_snapshot(experiment_dir: Path, snap: ClusterSnapshot) -> Path:
    """Persist *snap* under ``<exp>/.hpc/cluster_history/<cluster>/<unix_ts>.json``.

    Atomic write (``tempfile`` + :func:`os.replace`) so a reader that
    arrives mid-write either sees the previous snapshot list or the new
    one — never a partial JSON document. Returns the file path written.

    Bounded growth: after writing, the directory is trimmed to the
    most-recent :data:`MAX_HISTORY_SNAPSHOTS` files (oldest-first
    eviction). Same pattern as ``runtime_prior``'s sample list cap.

    Filename uses Unix timestamp seconds (sortable, no path-separator
    concerns). When two snapshots arrive in the same second we suffix
    ``-N`` to break ties — this is best-effort and the planner does not
    need second-resolution precision.
    """
    d = _history_dir(experiment_dir, snap.cluster)
    ts = parse_iso_utc_or_none(snap.now_iso)
    unix_ts = int(ts.timestamp()) if ts is not None else int(utcnow().timestamp())
    base = d / f"{unix_ts}.json"
    target = base
    counter = 1
    while target.exists():
        target = d / f"{unix_ts}-{counter}.json"
        counter += 1
    payload = json.dumps(snap.to_dict(), indent=2, sort_keys=True)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - manual cleanup in try/finally below
        "w",
        delete=False,
        dir=str(d),
        prefix=target.name + ".",
        suffix=".tmp",
        encoding="utf-8",
    )
    try:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise
    finally:
        if not tmp.closed:
            tmp.close()
    # Read the cap through the package so tests that monkeypatch
    # ``infra.inspect.MAX_HISTORY_SNAPSHOTS`` (the public re-export)
    # still take effect — direct module-local lookup would freeze the
    # value at import time and break the test hook.
    from hpc_agent.infra import inspect as _pkg

    _prune_history(d, _pkg.MAX_HISTORY_SNAPSHOTS)
    return target


def _prune_history(d: Path, limit: int) -> None:
    """Delete oldest snapshot files until at most *limit* remain.

    Sorts by filename so the embedded unix-ts orders chronologically.
    Best-effort: an unlink that races with another writer is ignored.
    """
    if limit <= 0:
        return
    try:
        files = sorted(p for p in d.iterdir() if p.suffix == ".json" and p.is_file())
    except OSError:
        return
    excess = len(files) - limit
    if excess <= 0:
        return
    for p in files[:excess]:
        try:
            p.unlink()
        except OSError:
            continue


def read_cluster_history(
    experiment_dir: Path,
    cluster: str,
    *,
    since_iso: str | None = None,
    limit: int | None = None,
) -> Iterator[ClusterSnapshot]:
    """Yield persisted snapshots in reverse-chronological order.

    *since_iso* (optional): filter out snapshots whose ``now_iso`` is
    strictly older than *since_iso*. Unparseable timestamps on either
    side fall through (returned).

    *limit* (optional): yield at most this many. Applied after the
    ``since_iso`` filter so callers asking for "the most recent N" get
    the most recent N matching snapshots.

    Files that fail to parse as JSON or lack the expected shape are
    silently skipped — same permissive-read posture as the rest of this
    module.
    """
    d = _history_dir(experiment_dir, cluster)
    try:
        files = sorted(
            (p for p in d.iterdir() if p.suffix == ".json" and p.is_file()),
            reverse=True,
        )
    except OSError:
        return
    since_dt = parse_iso_utc_or_none(since_iso) if since_iso else None
    yielded = 0
    for p in files:
        try:
            text = p.read_text()
        except OSError:
            continue
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        if since_dt is not None:
            ts = parse_iso_utc_or_none(doc.get("now_iso"))
            if ts is None or ts < since_dt:
                continue
        try:
            snap = _snapshot_from_dict(doc)
        except (KeyError, TypeError):
            continue
        yield snap
        yielded += 1
        if limit is not None and yielded >= limit:
            return
