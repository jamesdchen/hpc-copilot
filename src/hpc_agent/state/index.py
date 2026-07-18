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

from hpc_agent import errors
from hpc_agent.state.run_record import (
    RunRecord,
    _atomic_write_json,
    _lock_path,
    _locked,
    _read_json,
    journal_dir,
    journal_root_if_exists,
    runs_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "find_in_flight_runs",
    "find_submitting_runs",
    "find_runs_by_campaign",
    "find_held_runs",
    "find_stalled_runs",
    "find_parked_runs",
    "prune_terminal_runs",
]


def _all_run_files(experiment_dir: Path) -> list[Path]:
    rdir = runs_dir(experiment_dir)
    if not rdir.exists():
        return []
    # Exclude ``*.last_status.json`` cache snapshots written by
    # ``hpc_agent.ops.monitor.status.record_status`` — they share the runs/
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
    # _safe_mtime, not exists()+stat(): a concurrent rebuild/prune can
    # remove the index between the two calls, and an unguarded stat()
    # would crash a routine find_in_flight_runs. A missing index
    # (mtime 0.0) counts as stale so the caller rebuilds.
    idx_mtime = _safe_mtime(idx_path)
    if not idx_mtime:
        return True
    return any(_safe_mtime(p) > idx_mtime for p in _all_run_files(experiment_dir))


def _rebuild_index(experiment_dir: Path) -> dict:
    from hpc_agent._kernel.extension.version import is_compatible

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
                from hpc_agent.infra.time import utcnow_iso as _utcnow_iso

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
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import _current_homedir, _run_path

    # F46: probe the namespace with the NON-CREATING accessor. The old
    # ``journal_dir(experiment_dir).exists()`` guard could never fire — the call
    # itself mkdir'd the namespace + wrote repo.json — so every read from a
    # journal-less cwd scaffolded a ghost namespace. journal_root_if_exists()
    # only computes the path, so a read of a non-existent journal returns [] and
    # leaves no trace.
    if not _current_homedir().exists() or not journal_root_if_exists(experiment_dir).exists():
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
        # F42: trust the record we just loaded, not the index tag. A crash
        # between a terminal run-write and its index refresh leaves index.json
        # claiming ``in_flight`` for a run that is terminal on disk, and once a
        # sibling write bumps the index mtime past the run file the staleness
        # rebuild never fires again. Without this filter find_in_flight_runs
        # returns the finished run forever (doctor re-arms it, the campaign
        # loop counts a phantom live run). The status is already in hand.
        if record.status != "in_flight":
            continue
        records.append((_safe_mtime(path), record))
    records.sort(key=lambda item: item[0], reverse=True)
    return [r for _, r in records]


def find_submitting_runs(experiment_dir: Path) -> list[RunRecord]:
    """Return every run with ``status == "submitting"``, newest first.

    The pre-dispatch state (submit-once design §3.3): a submit mints the
    record ``submitting`` BEFORE the remote actuation and promotes it to
    ``in_flight`` only once the job id is in hand, so an orphaned submit
    (drop in the dispatch→id window) is a durable ``submitting`` record
    with empty ``job_ids`` that reconcile-recovery can own — never lost.

    This is the scan reconcile / ``doctor`` use to find those records to
    recover. It follows the same index-first shape as
    :func:`find_in_flight_runs` (the F42 trust-the-record-not-the-index
    filter, F46 non-creating namespace probe) but keys on ``submitting`` —
    a submitting run has no ``job_ids``, so it is deliberately NOT surfaced
    by ``find_in_flight_runs`` (the monitor/campaign live set); it is a
    pre-monitor state. These two scans are independent implementations:
    each keys on its own status literal, with no shared-drift contract.
    """
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import _current_homedir, _run_path

    # F46: non-creating namespace probe (see find_in_flight_runs).
    if not _current_homedir().exists() or not journal_root_if_exists(experiment_dir).exists():
        return []
    if _index_is_stale(experiment_dir):
        _rebuild_index(experiment_dir)
    idx = _read_index(experiment_dir)
    submitting_ids = [
        rid
        for rid, meta in idx.items()
        if isinstance(meta, dict) and meta.get("status") == "submitting"
    ]
    records: list[tuple[float, RunRecord]] = []
    for rid in submitting_ids:
        path = _run_path(experiment_dir, rid)
        if not path.exists():
            continue
        record = load_run(experiment_dir, rid)
        if record is None:
            continue
        # F42: trust the record we just loaded, not the (possibly stale) index
        # tag — a promote submitting→in_flight or a reconcile transition may
        # have landed on disk before its index refresh.
        if record.status != "submitting":
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
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.run_record import _current_homedir

    if not campaign_id:
        return []
    # F46: non-creating namespace probe (see find_in_flight_runs).
    if not _current_homedir().exists() or not journal_root_if_exists(experiment_dir).exists():
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


def find_held_runs(experiment_dir: Path, campaign_id: str | None = None) -> list[RunRecord]:
    """Return every run parked on a pending verdict (#231/#234), newest-first.

    A held run carries a non-empty ``pending_verdict`` escalation block — the
    deterministic resolver could not resolve its failure and it is waiting on a
    decision. The campaign loop uses this to know what is parked so it can keep
    progressing on unaffected work while treating held runs as *not done* (a
    pending verdict is neither a live run to monitor nor a terminal run to
    aggregate).

    Scans on-disk run files rather than the index, because the holding state is
    a record field, not the indexed ``status``. Optional *campaign_id* narrows
    to one campaign's parked set.
    """
    from hpc_agent.state.journal import is_held, load_run
    from hpc_agent.state.run_record import _current_homedir

    # F46: non-creating namespace probe (see find_in_flight_runs).
    if not _current_homedir().exists() or not journal_root_if_exists(experiment_dir).exists():
        return []
    held: list[tuple[float, RunRecord]] = []
    for path in _all_run_files(experiment_dir):
        record = load_run(experiment_dir, path.stem)
        if record is None or not is_held(record):
            continue
        if campaign_id is not None and record.campaign_id != campaign_id:
            continue
        held.append((_safe_mtime(path), record))
    held.sort(key=lambda item: item[0], reverse=True)
    return [r for _, r in held]


def find_stalled_runs(now_iso: str, experiment_dir: Path | None = None) -> list[dict]:
    """Return live runs whose driver missed its tick deadline (§5 watchdog).

    A run is *stalled* when it is still ``in_flight`` (or ``submitting``) AND
    carries a ``next_tick_due`` that is now in the past — the driver stamped a
    deadline via :func:`hpc_agent.state.journal.stamp_tick` and the next tick
    never landed by it. This is detection only; the ``doctor`` verb surfaces each
    hit as a drafted recovery proposal. Runs with no ``next_tick_due`` stamped
    yet (never ticked) are not stalled — absence of a deadline is not a missed
    one.

    A ``submitting`` run is also scanned (submit-once design §3.3): a submit that
    died in its dispatch window leaves a ``submitting`` record whose initial
    watchdog stamp lapses, so it surfaces here as a ``doctor`` recovery proposal
    that routes to reconcile-recovery (the ``status`` field on each hit carries
    ``submitting`` so the proposal drafts the right action — re-derive from the
    cluster, never a blind re-arm/re-submit).

    **Parked ≠ stalled** (block-drive.md §5): a run carrying a
    ``pending_decision`` marker is legitimately *awaiting a human decision* at a
    block's y/nudge boundary — not ticking, but not dead. It is excluded here
    (surfaced by :func:`find_parked_runs` instead) so ``doctor`` does not
    false-alarm a parked driver as "stalled — re-arm?".

    Each entry carries the fields a recovery proposal needs::

        {"run_id", "status", "last_tick_at", "next_tick_due", "cluster", "ssh_target"}

    *experiment_dir* defaults to the current working directory to mirror the
    pinned cross-unit signature ``find_stalled_runs(now_iso)`` (the ``doctor``
    verb passes its own dir). *now_iso* must be an ISO-8601 UTC string; a
    malformed value raises :class:`ValueError` (fail loud, not a silent empty).
    """
    from pathlib import Path

    from hpc_agent.infra.time import parse_iso_utc_or_none
    from hpc_agent.state.journal import is_awaiting_decision

    now_dt = parse_iso_utc_or_none(now_iso)
    if now_dt is None:
        raise ValueError(f"find_stalled_runs: now_iso {now_iso!r} is not ISO-8601")

    ed = Path(experiment_dir) if experiment_dir is not None else Path.cwd()
    stalled: list[dict] = []
    # Live in_flight runs PLUS pre-dispatch submitting runs (submit-once §3.3):
    # a submit that died in its dispatch window is a submitting record whose
    # initial watchdog stamp lapsed. Both share the lapsed-deadline check below.
    for record in [*find_in_flight_runs(ed), *find_submitting_runs(ed)]:
        # Parked-on-decision runs are awaiting the human, not stalled (§5).
        if is_awaiting_decision(record.run_id, experiment_dir=ed):
            continue
        due_dt = parse_iso_utc_or_none(record.next_tick_due)
        if due_dt is None:
            continue  # never ticked / no deadline stamped — not a miss
        if due_dt < now_dt:
            stalled.append(
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "last_tick_at": record.last_tick_at,
                    "next_tick_due": record.next_tick_due,
                    "cluster": record.cluster,
                    "ssh_target": record.ssh_target,
                }
            )
    return stalled


def find_parked_runs(now_iso: str, experiment_dir: Path | None = None) -> list[dict]:
    """Return live runs parked on a human decision (§5 "parked ≠ stalled").

    A run is *parked* (not stalled) when it is still ``in_flight`` AND carries a
    non-empty ``pending_decision`` marker — a ``block-drive`` span reached a
    block's y/nudge boundary, wrote ``{block, brief, resume_cursor}``, and is
    waiting for the human's ``y``/nudge (see
    :func:`hpc_agent.state.journal.mark_pending_decision`). Such a driver is not
    ticking but is not dead, so ``doctor`` reads it as "awaiting your decision
    since T" rather than false-alarming a stalled driver.

    Each entry carries what a parked-run read needs::

        {"run_id", "status", "block", "workflow", "awaiting_since"}

    *now_iso* is validated (ISO-8601 UTC, fail loud) for signature parity with
    :func:`find_stalled_runs`; the parked read itself does not depend on a
    deadline. *experiment_dir* defaults to the current working directory.
    """
    from pathlib import Path

    from hpc_agent.infra.time import parse_iso_utc_or_none
    from hpc_agent.state.journal import read_pending_decision

    if parse_iso_utc_or_none(now_iso) is None:
        raise ValueError(f"find_parked_runs: now_iso {now_iso!r} is not ISO-8601")

    ed = Path(experiment_dir) if experiment_dir is not None else Path.cwd()
    parked: list[dict] = []
    for record in find_in_flight_runs(ed):
        marker = read_pending_decision(record.run_id, experiment_dir=ed)
        if not marker:
            continue
        parked.append(
            {
                "run_id": record.run_id,
                "status": record.status,
                "block": marker.get("block"),
                "workflow": marker.get("workflow"),
                "awaiting_since": marker.get("awaiting_since"),
            }
        )
    return parked


def prune_terminal_runs(experiment_dir: Path, keep: int = 20) -> int:
    """Evict oldest TERMINAL runs past *keep*. Returns count removed.

    Only :data:`TERMINAL_STATUSES` (complete / failed / abandoned) are
    prune-eligible. A non-terminal record — ``in_flight`` (live) OR
    ``submitting`` (pre-dispatch / orphaned; submit-once design §3.3) —
    is NEVER pruned: pruning a ``submitting`` orphan would garbage-collect
    the only durable evidence reconcile-recovery needs to adopt the array
    or safely re-submit. The guard keys on membership in TERMINAL_STATUSES,
    not ``!= "in_flight"``, so any future non-terminal status is kept too.
    """
    from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES

    if keep < 0:
        raise errors.SpecInvalid("keep must be non-negative")
    files = _all_run_files(experiment_dir)
    terminal: list[tuple[float, Path, str]] = []
    for path in files:
        payload = _read_json(path)
        if payload is None:
            continue
        if payload.get("status", "in_flight") not in TERMINAL_STATUSES:
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


def discover_journaled_experiments() -> tuple[list[Path], list[dict[str, str]]]:
    """Every experiment this machine has journaled — via a NON-CREATING glob.

    The ONE definition of fleet discovery (moved from ``ops.attention_queue``
    so substrate readers — e.g. the Stop-hook completeness witness — can reach
    it without a ``_kernel``-imports-``ops`` layering inversion; the ops seat
    delegates here). Globs the journal home for ``*/repo.json`` (never
    ``journal_dir``, which mkdirs + writes ``repo.json`` — a read must never
    scaffold a namespace) and recovers each ``experiment_dir``. Returns
    ``(experiment_dirs, skipped)``: a ``repo.json`` that is unreadable / torn,
    or whose ``experiment_dir`` no longer exists on disk, is skipped silently
    and counted (a wiped demo repo must never crash the morning read). A
    missing journal home yields nothing.
    """
    import json
    from pathlib import Path

    from hpc_agent.state.run_record import current_homedir

    experiments: list[Path] = []
    skipped: list[dict[str, str]] = []
    home = current_homedir()
    if not home.exists():
        return experiments, skipped
    for repo_json in sorted(home.glob("*/repo.json")):
        namespace = repo_json.parent.name
        try:
            doc = json.loads(repo_json.read_text(encoding="utf-8"))
            experiment_dir = doc["experiment_dir"]
        except (OSError, ValueError, KeyError, TypeError):
            skipped.append({"ref": namespace, "reason": "unreadable/torn repo.json"})
            continue
        if not isinstance(experiment_dir, str) or not experiment_dir:
            skipped.append({"ref": namespace, "reason": "repo.json has no experiment_dir"})
            continue
        path = Path(experiment_dir)
        if not path.exists():
            skipped.append({"ref": namespace, "reason": "experiment_dir no longer exists"})
            continue
        experiments.append(path)
    return experiments, skipped
