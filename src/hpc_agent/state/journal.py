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

from hpc_agent.state.run_record import (
    _UPDATABLE_FIELDS,
    RunRecord,
    _atomic_write_json,
    _locked,
    _read_json,
    _run_path,
    journal_dir,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = [
    "load_run",
    "upsert_run",
    "update_run_status",
    "update_run_record",
    "mark_run",
]


def load_run(experiment_dir: Path, run_id: str) -> RunRecord | None:
    """Read one run record. Returns ``None`` if missing or schema mismatch."""
    path = _run_path(experiment_dir, run_id)
    payload = _read_json(path)
    if payload is None:
        return None
    # B8: route reader-side check through the cross-domain manifest in
    # hpc_agent._kernel.extension.version. Writer still emits SCHEMA_VERSION;
    # the manifest declares the *supported* range so back-compat is one
    # one-line edit if/when v2 ships.
    from hpc_agent._kernel.extension.version import is_compatible

    found = payload.get("schema_version")
    if not isinstance(found, int) or not is_compatible("session", found):
        warnings.warn(
            f"session: schema_version={payload.get('schema_version')!r} "
            f"unsupported; skipping {path.name}",
            stacklevel=2,
        )
        return None
    try:
        return RunRecord.from_dict(payload)
    except TypeError:
        # A structurally-incomplete v1 record (e.g. an older record
        # written before a now-required field existed, or a truncated
        # file) makes the dataclass constructor raise. ``load_run`` is
        # documented to return None on an unusable record — skip it
        # rather than letting the TypeError escape into callers.
        warnings.warn(
            f"session: run record {path.name} is structurally incomplete; skipping",
            stacklevel=2,
        )
        return None


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


def update_run_record(
    experiment_dir: Path,
    run_id: str,
    mutate: Callable[[RunRecord], None],
) -> RunRecord:
    """Locked read-modify-write of a run record via a mutation callback.

    Unlike :func:`update_run_status` — which overwrites whitelisted
    fields with caller-supplied *values* — this reads the record, hands
    the live :class:`RunRecord` to *mutate*, and writes it back, all
    inside the per-run flock. Use it when the new value depends on the
    current on-disk value (e.g. appending to ``combined_waves``): passing
    a snapshot computed from an earlier unlocked ``load_run`` read would
    silently drop a concurrent writer's update.

    Raises :class:`FileNotFoundError` if no record exists for *run_id*.
    """
    path = _run_path(experiment_dir, run_id)
    with _locked(path):
        existing = _read_json(path)
        if existing is None:
            raise FileNotFoundError(f"no run record for {run_id!r}")
        record = RunRecord.from_dict(existing)
        mutate(record)
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
    from hpc_agent._kernel.lifecycle.lifecycle import JournalStatus

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


def _refresh_index_entry(
    experiment_dir: Path,
    run_id: str,
    status: str,
) -> None:
    """Bump a single ``index.json`` entry; called after every successful write.

    Re-reads the run file under the index lock and uses its freshly-read
    status (falling back to *status* only if the per-run file read fails).
    This closes a lost-update race: two writers A and B that each release
    the per-run lock before grabbing the index lock could otherwise install
    A's stale status over B's terminal-transition write.

    If the index read fails (transient OSError, partial-write torn JSON)
    AND the index file exists, we refuse to overwrite — the index will
    self-heal on the next ``_index_is_stale`` rebuild. Previously the
    helper treated a failed read as "treat the entire index as empty,"
    which clobbered every other entry with a single-key dict.
    """
    from hpc_agent.infra.time import utcnow_iso
    from hpc_agent.state.run_record import _read_json as _read_run
    from hpc_agent.state.run_record import _run_path

    idx_path = journal_dir(experiment_dir) / "index.json"
    with _locked(idx_path):
        # Re-read the current status from disk so a concurrent writer's
        # terminal transition can't get clobbered by our stale snapshot.
        run_path = _run_path(experiment_dir, run_id)
        fresh_status = status
        try:
            payload = _read_run(run_path)
        except Exception:  # noqa: BLE001 — fall back to caller-supplied value
            payload = None
        if isinstance(payload, dict):
            payload_status = payload.get("status")
            if isinstance(payload_status, str) and payload_status:
                fresh_status = payload_status

        idx_existed = idx_path.exists()
        idx = _read_json(idx_path)
        if idx is None:
            if idx_existed:
                # Read failed on a file that exists — likely transient.
                # Refuse to overwrite the whole index with a one-entry
                # dict; the staleness check will rebuild from per-run
                # files on the next find_in_flight_runs call.
                return
            idx = {}
        if not isinstance(idx, dict):
            return  # corrupt index — same self-heal logic applies
        idx[run_id] = {"status": fresh_status, "updated_at": utcnow_iso()}
        _atomic_write_json(idx_path, idx)
