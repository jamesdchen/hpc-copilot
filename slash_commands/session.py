"""Per-run journal for HPC submissions.

Persists the bootstrap context for an in-flight `/submit` so a fresh Claude
Code session can pick up `/status` without re-deriving the cluster, job
IDs, manifest filename, combined-wave list, retry history, etc.

Storage layout (one tree per experiment cwd):

    ~/.claude/hpc/<repo_hash>/
    ├── repo.json                   # {"experiment_dir": ..., "first_seen": ...}
    ├── index.json                  # cache of run_id -> {status, updated_at}
    ├── index.lock
    └── runs/
        ├── <run_id>.json
        ├── <run_id>.lock
        └── ...

`repo_hash` is `sha256(experiment_dir.resolve())[:12]`. Pure IO; no SSH,
no mapreduce imports. Composition with cluster-mutating ops lives in
``slash_commands.runner``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "SCHEMA_VERSION",
    "HPC_HOMEDIR",
    "RunRecord",
    "repo_hash",
    "journal_dir",
    "runs_dir",
    "load_run",
    "upsert_run",
    "update_run_status",
    "mark_run",
    "find_in_flight_runs",
    "prune_terminal_runs",
]

SCHEMA_VERSION = 1
# Resolve at import time. MARs (and any caller that wants its own state tree)
# can set HPC_JOURNAL_DIR before importing this module to redirect the journal.
HPC_HOMEDIR = Path(
    os.environ.get("HPC_JOURNAL_DIR") or (Path.home() / ".claude" / "hpc")
)
TERMINAL_STATUSES = frozenset({"complete", "failed", "abandoned"})
_UPDATABLE_FIELDS = frozenset(
    {
        "last_status",
        "combined_waves",
        "failed_waves",
        "retries",
        "stage",
        "job_ids",
        "last_resubmit_request_id",
    }
)
_log = logging.getLogger(__name__)


@dataclasses.dataclass
class RunRecord:
    """One submitted run as seen by the agent layer."""

    run_id: str
    profile: str
    cluster: str
    ssh_target: str
    remote_path: str
    job_name: str
    job_ids: list[str]
    total_tasks: int
    submitted_at: str
    experiment_dir: str
    last_status: dict = dataclasses.field(default_factory=dict)
    combined_waves: list[int] = dataclasses.field(default_factory=list)
    failed_waves: list[int] = dataclasses.field(default_factory=list)
    retries: dict[str, dict] = dataclasses.field(default_factory=dict)
    stage: str = "monitor"
    status: str = "in_flight"
    last_resubmit_request_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "RunRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_hash(experiment_dir: Path) -> str:
    """Stable 12-char hex digest of the resolved experiment directory."""
    return hashlib.sha256(str(Path(experiment_dir).resolve()).encode()).hexdigest()[:12]


def journal_dir(experiment_dir: Path) -> Path:
    """Return ``~/.claude/hpc/<repo_hash>/`` for *experiment_dir* (created)."""
    d = HPC_HOMEDIR / repo_hash(experiment_dir)
    d.mkdir(parents=True, exist_ok=True)
    repo_meta = d / "repo.json"
    if not repo_meta.exists():
        _atomic_write_json(
            repo_meta,
            {
                "experiment_dir": str(Path(experiment_dir).resolve()),
                "first_seen": _utcnow_iso(),
            },
        )
    (d / "runs").mkdir(exist_ok=True)
    return d


def runs_dir(experiment_dir: Path) -> Path:
    return journal_dir(experiment_dir) / "runs"


def _run_path(experiment_dir: Path, run_id: str) -> Path:
    return runs_dir(experiment_dir) / f"{run_id}.json"


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _locked(target: Path) -> Iterator[None]:
    """Acquire an exclusive flock on a sibling ``.lock`` file for *target*.

    No-op on platforms without ``fcntl`` (e.g. Windows). The lock file is
    created on demand and never deleted — flock semantics handle reuse.
    """
    if fcntl is None:
        yield
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(target)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("session: skipping unreadable %s (%s)", path, exc)
        return None


def load_run(experiment_dir: Path, run_id: str) -> RunRecord | None:
    """Read one run record. Returns ``None`` if missing or schema mismatch."""
    path = _run_path(experiment_dir, run_id)
    payload = _read_json(path)
    if payload is None:
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        warnings.warn(
            f"session: schema_version={payload.get('schema_version')} "
            f"!= {SCHEMA_VERSION}; skipping {path.name}",
            stacklevel=2,
        )
        return None
    return RunRecord.from_dict(payload)


def upsert_run(experiment_dir: Path, record: RunRecord) -> None:
    """Atomically write the run record and refresh the index entry."""
    path = _run_path(experiment_dir, record.run_id)
    with _locked(path):
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)


def update_run_status(experiment_dir: Path, run_id: str, **fields: Any) -> RunRecord:
    """Read-modify-write a single run record. Whitelisted fields only."""
    bad = set(fields) - _UPDATABLE_FIELDS
    if bad:
        raise ValueError(f"update_run_status: unknown field(s) {sorted(bad)}")
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        existing.update(fields)
        record = RunRecord.from_dict(existing)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record


def mark_run(
    experiment_dir: Path,
    run_id: str,
    *,
    status: str,
    stage: str | None = None,
) -> RunRecord:
    """Terminal transition. Updates status (and optionally stage)."""
    if status not in {"in_flight", *TERMINAL_STATUSES}:
        raise ValueError(f"mark_run: invalid status {status!r}")
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        existing["status"] = status
        if stage is not None:
            existing["stage"] = stage
        record = RunRecord.from_dict(existing)
        _atomic_write_json(path, record.to_dict())
    _refresh_index_entry(experiment_dir, record.run_id, record.status)
    return record


def _all_run_files(experiment_dir: Path) -> list[Path]:
    rdir = runs_dir(experiment_dir)
    if not rdir.exists():
        return []
    # Exclude ``*.last_status.json`` cache snapshots written by
    # ``slash_commands.runner.record_status`` — they share the runs/
    # directory but are not journal records.  Including them here
    # made every status poll touch the directory's mtime and force
    # a full index rebuild on the next ``find_in_flight_runs``.
    return [
        p
        for p in rdir.glob("*.json")
        if not p.name.endswith(".tmp")
        and not p.name.endswith(".last_status.json")
    ]


def _read_index(experiment_dir: Path) -> dict:
    idx_path = journal_dir(experiment_dir) / "index.json"
    payload = _read_json(idx_path) or {}
    return payload if isinstance(payload, dict) else {}


def _index_is_stale(experiment_dir: Path) -> bool:
    idx_path = journal_dir(experiment_dir) / "index.json"
    if not idx_path.exists():
        return True
    idx_mtime = idx_path.stat().st_mtime
    return any(p.stat().st_mtime > idx_mtime for p in _all_run_files(experiment_dir))


def _rebuild_index(experiment_dir: Path) -> dict:
    entries: dict[str, dict] = {}
    for path in _all_run_files(experiment_dir):
        payload = _read_json(path)
        if payload is None:
            continue
        if payload.get("schema_version") != SCHEMA_VERSION:
            continue
        run_id = payload.get("run_id") or path.stem
        entries[run_id] = {
            "status": payload.get("status", "in_flight"),
            "updated_at": _utcnow_iso(),
        }
    idx_path = journal_dir(experiment_dir) / "index.json"
    with _locked(idx_path):
        _atomic_write_json(idx_path, entries)
    return entries


def _refresh_index_entry(experiment_dir: Path, run_id: str, status: str) -> None:
    idx_path = journal_dir(experiment_dir) / "index.json"
    with _locked(idx_path):
        idx = _read_json(idx_path) or {}
        if not isinstance(idx, dict):
            idx = {}
        idx[run_id] = {"status": status, "updated_at": _utcnow_iso()}
        _atomic_write_json(idx_path, idx)


def find_in_flight_runs(experiment_dir: Path) -> list[RunRecord]:
    """Return every run with ``status == "in_flight"``, newest first.

    Cross-checks the index against on-disk run files; rebuilds the index
    if it's missing or stale.
    """
    if not HPC_HOMEDIR.exists() or not journal_dir(experiment_dir).exists():
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
        records.append((path.stat().st_mtime, record))
    records.sort(key=lambda item: item[0], reverse=True)
    return [r for _, r in records]


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
        terminal.append((path.stat().st_mtime, path, payload.get("run_id", path.stem)))
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
