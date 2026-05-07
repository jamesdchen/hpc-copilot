"""Per-run journal package.

Was a single ~450 LOC module; split into three submodules along the
audit's cleanest seam:

* :mod:`.run_record` — :class:`RunRecord` dataclass + path / lock /
  atomic-I/O primitives.
* :mod:`.journal` — :func:`load_run`, :func:`upsert_run`,
  :func:`update_run_status`, :func:`mark_run`. Each writer pairs with
  ``_refresh_index_entry``; that pairing lives here too because the
  invariant is "one lock, one write, one index bump."
* :mod:`.index` — index scan / rebuild / pruning + the cross-run
  queries (:func:`find_in_flight_runs`, :func:`find_runs_by_campaign`,
  :func:`prune_terminal_runs`).

This package re-exports the entire previous ``session.py`` public
surface so all 60+ existing import sites keep working unchanged. The
underscore-prefixed helpers (``_atomic_write_json``, ``_locked``,
``_run_path``, ``_lock_path``, ``_read_json``, ``_UPDATABLE_FIELDS``)
are also re-exported because tests / inner code reach into them.
"""

from __future__ import annotations

from claude_hpc._internal.session.index import (
    _all_run_files,
    _index_is_stale,
    _read_index,
    _rebuild_index,
    find_in_flight_runs,
    find_runs_by_campaign,
    prune_terminal_runs,
)
from claude_hpc._internal.session.journal import _refresh_index_entry
from claude_hpc._internal.session.journal import (
    load_run,
    mark_run,
    update_run_status,
    upsert_run,
)
from claude_hpc._internal.session.run_record import (
    HPC_HOMEDIR,
    SCHEMA_VERSION,
    TERMINAL_STATUSES,
    RunRecord,
    _atomic_write_json,
    _lock_path,
    _locked,
    _read_json,
    _run_path,
    _UPDATABLE_FIELDS,
    journal_dir,
    repo_hash,
    runs_dir,
)

__all__ = [
    "SCHEMA_VERSION",
    "HPC_HOMEDIR",
    "TERMINAL_STATUSES",
    "RunRecord",
    "find_in_flight_runs",
    "find_runs_by_campaign",
    "journal_dir",
    "load_run",
    "mark_run",
    "prune_terminal_runs",
    "repo_hash",
    "runs_dir",
    "update_run_status",
    "upsert_run",
]
