"""Session/journal back-compat barrel.

The previous ``hpc_agent.state.session`` package was moved into
``hpc_agent.state.{journal,run_record,index}`` in the Wave 4 reorg.
This module mirrors the previous ``__init__`` re-export surface so the
60+ import sites that did ``from hpc_agent.state import session`` /
``session._read_json`` keep working unchanged via
``from hpc_agent.state import session``.

Prefer direct submodule imports for new code:

* :mod:`hpc_agent.state.run_record` — :class:`RunRecord`, locking,
  atomic-I/O primitives.
* :mod:`hpc_agent.state.journal` — read-modify-write operations.
* :mod:`hpc_agent.state.index` — index scan / rebuild / cross-run queries.
"""

from __future__ import annotations

# Submodules re-exported by name so legacy call sites that did
# ``from hpc_agent._internal.session import run_record`` keep working
# via the rewritten ``from hpc_agent.state.session import run_record``
# path. Tests in particular monkeypatch attributes on the module
# object (``monkeypatch.setattr(run_record, "HPC_HOMEDIR", ...)``).
from hpc_agent.state import index, journal, run_record  # noqa: F401 — re-export
from hpc_agent.state.index import (
    _all_run_files,
    _index_is_stale,
    _read_index,
    _rebuild_index,
    find_in_flight_runs,
    find_runs_by_campaign,
    prune_terminal_runs,
)
from hpc_agent.state.journal import (
    _refresh_index_entry,
    load_run,
    mark_run,
    update_run_record,
    update_run_status,
    upsert_run,
)
from hpc_agent.state.run_record import (
    _UPDATABLE_FIELDS,
    HPC_HOMEDIR,
    SCHEMA_VERSION,
    TERMINAL_STATUSES,
    RunRecord,
    _atomic_write_json,
    _lock_path,
    _locked,
    _read_json,
    _run_path,
    journal_dir,
    repo_hash,
    runs_dir,
)

__all__ = [
    "HPC_HOMEDIR",
    "SCHEMA_VERSION",
    "TERMINAL_STATUSES",
    "RunRecord",
    "_UPDATABLE_FIELDS",
    "_all_run_files",
    "_atomic_write_json",
    "_index_is_stale",
    "_lock_path",
    "_locked",
    "_read_index",
    "_read_json",
    "_rebuild_index",
    "_refresh_index_entry",
    "_run_path",
    "find_in_flight_runs",
    "find_runs_by_campaign",
    "journal_dir",
    "load_run",
    "mark_run",
    "prune_terminal_runs",
    "repo_hash",
    "runs_dir",
    "update_run_record",
    "update_run_status",
    "upsert_run",
]
