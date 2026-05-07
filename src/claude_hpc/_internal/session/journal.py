"""Per-run journal — read-modify-write operations on individual run records.

Composes the layout / locking / atomic-write primitives in
:mod:`.run_record` into the four operations the rest of the framework
calls: :func:`load_run`, :func:`upsert_run`, :func:`update_run_status`,
:func:`mark_run`. Index-side maintenance (refreshing the
``index.json`` cache after each write) lives here too because the
write paths and the index update are paired — splitting them across
modules invited skew when a writer landed but the index update lost
its lock race.

Pure scan / rebuild / query helpers live in :mod:`.index`.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from claude_hpc._internal.session.run_record import (
    _UPDATABLE_FIELDS,
    RunRecord,
    _atomic_write_json,
    _locked,
    _read_json,
    _run_path,
    journal_dir,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "load_run",
    "upsert_run",
    "update_run_status",
    "mark_run",
]


def load_run(experiment_dir: Path, run_id: str) -> RunRecord | None:
    """Read one run record. Returns ``None`` if missing or schema mismatch."""
    path = _run_path(experiment_dir, run_id)
    payload = _read_json(path)
    if payload is None:
        return None
    # B8: route reader-side check through the cross-domain manifest in
    # claude_hpc._internal.version. Writer still emits SCHEMA_VERSION;
    # the manifest declares the *supported* range so back-compat is one
    # one-line edit if/when v2 ships.
    from claude_hpc._internal.version import is_compatible

    found = payload.get("schema_version")
    if not isinstance(found, int) or not is_compatible("session", found):
        warnings.warn(
            f"session: schema_version={payload.get('schema_version')!r} "
            f"unsupported; skipping {path.name}",
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
    # Validate against the canonical JournalStatus StrEnum (B2).
    from claude_hpc._internal.lifecycle import JournalStatus

    if status not in set(JournalStatus):
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


def _refresh_index_entry(experiment_dir: Path, run_id: str, status: str) -> None:
    """Bump a single ``index.json`` entry; called after every successful write."""
    from claude_hpc._internal.time import utcnow_iso

    idx_path = journal_dir(experiment_dir) / "index.json"
    with _locked(idx_path):
        idx = _read_json(idx_path) or {}
        if not isinstance(idx, dict):
            idx = {}
        idx[run_id] = {"status": status, "updated_at": utcnow_iso()}
        _atomic_write_json(idx_path, idx)
