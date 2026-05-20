"""Per-run journal — index scan / rebuild / pruning + cross-run queries.

The journal's ``index.json`` caches ``run_id -> {status, updated_at}``
so ``find_in_flight_runs`` doesn't need to re-parse every per-run
sidecar on each call. This module owns that cache and the queries
that read it.

Per-write index updates live in :mod:`.journal` alongside the writers
(they're paired and must succeed or fail together); this module
handles the scan / rebuild / prune paths that only need the cache,
plus the lookup helpers (``find_in_flight_runs``,
``find_runs_by_campaign``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from hpc_agent._internal.session.run_record import (
    RunRecord,
    _atomic_write_json,
    _lock_path,
    _locked,
    _read_json,
    journal_dir,
    runs_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "find_in_flight_runs",
    "find_runs_by_campaign",
    "prune_terminal_runs",
]


def _all_run_files(experiment_dir: Path) -> list[Path]:
    rdir = runs_dir(experiment_dir)
    if not rdir.exists():
        return []
    # Exclude ``*.last_status.json`` cache snapshots written by
    # ``hpc_agent.runner.record_status`` — they share the runs/
    # directory but are not journal records.  Including them here
    # made every status poll touch the directory's mtime and force
    # a full index rebuild on the next ``find_in_flight_runs``.
    return [
        p
        for p in rdir.glob("*.json")
        if not p.name.endswith(".tmp") and not p.name.endswith(".last_status.json")
    ]


def _safe_mtime(p: Path) -> float:
    """File mtime, or 0.0 if the file vanished.

    A concurrent ``prune_terminal_runs`` (or another session's prune)
    can ``unlink`` a run file between the directory glob and the
    ``stat()`` here. An unguarded ``stat()`` would raise
    ``FileNotFoundError`` and crash a routine ``find_in_flight_runs``
    call, so a vanished file is treated as "oldest" instead.
    """
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _read_index(experiment_dir: Path) -> dict:
    idx_path = journal_dir(experiment_dir) / "index.json"
    payload = _read_json(idx_path) or {}
    return payload if isinstance(payload, dict) else {}


def _index_is_stale(experiment_dir: Path) -> bool:
    idx_path = journal_dir(experiment_dir) / "index.json"
    if not idx_path.exists():
        return True
    idx_mtime = idx_path.stat().st_mtime
    return any(_safe_mtime(p) > idx_mtime for p in _all_run_files(experiment_dir))


def _rebuild_index(experiment_dir: Path) -> dict:
    from hpc_agent._internal.version import is_compatible

    idx_path = journal_dir(experiment_dir) / "index.json"
    # Hold the index lock for the entire scan+write so concurrent
    # ``_refresh_index_entry`` writes from other processes can't slip in
    # between the directory scan and the index rewrite (which would
    # otherwise clobber the freshly-installed terminal-transition
    # entry). Use each run file's mtime as ``updated_at`` so a routine
    # rebuild doesn't clobber the real timestamps with "time of last
    # rebuild".
    with _locked(idx_path):
        entries: dict[str, dict] = {}
        for path in _all_run_files(experiment_dir):
            payload = _read_json(path)
            if payload is None:
                continue
            sv = payload.get("schema_version")
            if not isinstance(sv, int) or not is_compatible("session", sv):
                continue
            run_id = payload.get("run_id") or path.stem
            try:
                mtime_iso = (
                    __import__("datetime")
                    .datetime.fromtimestamp(
                        path.stat().st_mtime,
                        tz=__import__("datetime").timezone.utc,
                    )
                    .isoformat(timespec="seconds")
                )
            except OSError:
                from hpc_agent._internal.time import utcnow_iso as _utcnow_iso

                mtime_iso = _utcnow_iso()
            entries[run_id] = {
                "status": payload.get("status", "in_flight"),
                "updated_at": mtime_iso,
            }
        _atomic_write_json(idx_path, entries)
    return entries


def find_in_flight_runs(experiment_dir: Path) -> list[RunRecord]:
    """Return every run with ``status == "in_flight"``, newest first.

    Cross-checks the index against on-disk run files; rebuilds the index
    if it's missing or stale.
    """
    from hpc_agent._internal.session.journal import load_run
    from hpc_agent._internal.session.run_record import _current_homedir, _run_path

    if not _current_homedir().exists() or not journal_dir(experiment_dir).exists():
        return []
    if _index_is_stale(experiment_dir):
        _rebuild_index(experiment_dir)
    idx = _read_index(experiment_dir)
    in_flight_ids = [
        rid
        for rid, meta in idx.items()
        if isinstance(meta, dict) and meta.get("status") == "in_flight"
    ]
    records: list[tuple[float, RunRecord]] = []
    for rid in in_flight_ids:
        path = _run_path(experiment_dir, rid)
        if not path.exists():
            continue
        record = load_run(experiment_dir, rid)
        if record is None:
            continue
        records.append((_safe_mtime(path), record))
    records.sort(key=lambda item: item[0], reverse=True)
    return [r for _, r in records]


def find_runs_by_campaign(experiment_dir: Path, campaign_id: str) -> list[RunRecord]:
    """Return every run whose ``campaign_id`` matches, oldest-first.

    Used by the asyncio campaign loop on resume to discover its in-flight
    set without re-asking the user. Empty *campaign_id* returns ``[]`` —
    open-loop submits never match a campaign.
    """
    from hpc_agent._internal.session.journal import load_run
    from hpc_agent._internal.session.run_record import _current_homedir

    if not campaign_id:
        return []
    if not _current_homedir().exists() or not journal_dir(experiment_dir).exists():
        return []
    files = _all_run_files(experiment_dir)
    matched: list[tuple[float, RunRecord]] = []
    for path in files:
        record = load_run(experiment_dir, path.stem)
        if record is None or record.campaign_id != campaign_id:
            continue
        matched.append((_safe_mtime(path), record))
    matched.sort(key=lambda item: item[0])  # oldest-first
    return [r for _, r in matched]


def prune_terminal_runs(experiment_dir: Path, keep: int = 20) -> int:
    """Evict oldest non-in-flight runs past *keep*. Returns count removed."""
    if keep < 0:
        raise ValueError("keep must be non-negative")
    files = _all_run_files(experiment_dir)
    terminal: list[tuple[float, Path, str]] = []
    for path in files:
        payload = _read_json(path)
        if payload is None:
            continue
        if payload.get("status", "in_flight") == "in_flight":
            continue
        terminal.append((_safe_mtime(path), path, payload.get("run_id", path.stem)))
    if len(terminal) <= keep:
        return 0
    terminal.sort(key=lambda item: item[0], reverse=True)

    # Collect deletions first; update the index once at the end so we
    # do one atomic write + one flock per prune call instead of N.
    # Without batching, a process that dies mid-loop leaves run files
    # ``unlink``'d but still listed in the index — a journal pointing
    # at ghosts until the next staleness rebuild.
    removed_ids: list[str] = []
    for _, path, run_id in terminal[keep:]:
        try:
            path.unlink()
        except OSError:
            continue
        with contextlib.suppress(OSError):
            _lock_path(path).unlink()
        # Also unlink the per-run ``.last_status.json`` cache file
        # written by ``runner.record_status``; otherwise it
        # accumulates indefinitely.
        with contextlib.suppress(OSError):
            (path.parent / f"{path.stem}.last_status.json").unlink()
        removed_ids.append(run_id)

    if removed_ids:
        idx_path = journal_dir(experiment_dir) / "index.json"
        with _locked(idx_path):
            idx = _read_json(idx_path) or {}
            if isinstance(idx, dict):
                changed = False
                for rid in removed_ids:
                    if rid in idx:
                        del idx[rid]
                        changed = True
                if changed:
                    _atomic_write_json(idx_path, idx)
    return len(removed_ids)
