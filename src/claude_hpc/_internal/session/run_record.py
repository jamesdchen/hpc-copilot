"""Per-run journal — data layer.

Owns the RunRecord dataclass + the layout / locking / atomic-write
primitives that every higher-level helper in
``claude_hpc._internal.session`` builds on.

Storage layout (one tree per experiment cwd)::

    ~/.claude/hpc/<repo_hash>/
    ├── repo.json                   {"experiment_dir": ..., "first_seen": ...}
    ├── index.json                  cache of run_id -> {status, updated_at}
    ├── index.lock
    └── runs/
        ├── <run_id>.json
        ├── <run_id>.lock
        └── ...

``repo_hash`` is ``sha256(experiment_dir.resolve())[:12]``. Pure I/O;
no SSH, no mapreduce imports — composition with cluster-mutating ops
lives in :mod:`claude_hpc.runner`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from claude_hpc._internal.io import advisory_flock
from claude_hpc._internal.lifecycle import TERMINAL_STATUSES as _LIFECYCLE_TERMINAL

__all__ = [
    "SCHEMA_VERSION",
    "HPC_HOMEDIR",
    "TERMINAL_STATUSES",
    "RunRecord",
    "repo_hash",
    "journal_dir",
    "runs_dir",
]

if TYPE_CHECKING:
    from collections.abc import Iterator

SCHEMA_VERSION = 1
# Resolve at import time. MARs (and any caller that wants its own state tree)
# can set HPC_JOURNAL_DIR before importing this module to redirect the journal.
HPC_HOMEDIR = Path(os.environ.get("HPC_JOURNAL_DIR") or (Path.home() / ".claude" / "hpc"))

# Re-exported for back-compat. Derived from the canonical
# claude_hpc._internal.lifecycle.JournalStatus StrEnum so the literal can
# no longer drift from the rest of the codebase.
TERMINAL_STATUSES = _LIFECYCLE_TERMINAL

# Whitelist of fields ``update_run_status`` may overwrite on an existing
# RunRecord. Keeps the read-modify-write helper from accidentally
# clobbering identity / provenance fields (run_id, profile, cluster,
# submitted_at, etc.).
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
    # Closed-loop campaign tag. Empty string for open-loop submits.
    # Populated when /submit was invoked with --campaign-id (or with
    # campaign_id set on the submit spec). The asyncio campaign loop
    # uses ``find_runs_by_campaign`` to discover its in-flight set on
    # resume.
    campaign_id: str = ""
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> RunRecord:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in known})


def repo_hash(experiment_dir: Path) -> str:
    """Stable 12-char hex digest of the resolved experiment directory."""
    return hashlib.sha256(str(Path(experiment_dir).resolve()).encode()).hexdigest()[:12]


def journal_dir(experiment_dir: Path) -> Path:
    """Return ``~/.claude/hpc/<repo_hash>/`` for *experiment_dir* (created)."""
    from claude_hpc._internal.time import utcnow_iso

    d = HPC_HOMEDIR / repo_hash(experiment_dir)
    d.mkdir(parents=True, exist_ok=True)
    repo_meta = d / "repo.json"
    if not repo_meta.exists():
        _atomic_write_json(
            repo_meta,
            {
                "experiment_dir": str(Path(experiment_dir).resolve()),
                "first_seen": utcnow_iso(),
            },
        )
    (d / "runs").mkdir(exist_ok=True)
    return d


def runs_dir(experiment_dir: Path) -> Path:
    """Deprecated forwarder for ``JournalLayout(experiment_dir).runs``.

    NOTE: this is the **journal** runs directory under
    ``~/.claude/hpc/<repo_hash>/runs/``, NOT the cluster sidecar runs
    directory under ``<experiment_dir>/.hpc/runs/`` — that one is
    :attr:`claude_hpc._internal.layout.RepoLayout.runs` (also exported as
    :func:`claude_hpc.runs_subdir`). The pre-B1 collision between
    these two ``runs_*`` names was a P0 bug source; the
    ``RepoLayout`` / ``JournalLayout`` type split makes it a type
    error.
    """
    from claude_hpc._internal.layout import JournalLayout

    return JournalLayout(experiment_dir).runs


def _run_path(experiment_dir: Path, run_id: str) -> Path:
    """Deprecated alias for ``JournalLayout(experiment_dir).run_record(run_id)``."""
    from claude_hpc._internal.layout import JournalLayout

    return JournalLayout(experiment_dir).run_record(run_id)


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _locked(target: Path) -> Iterator[None]:
    """Acquire an exclusive flock on a sibling ``.lock`` file for *target*.

    Thin wrapper around :func:`claude_hpc._internal.io.advisory_flock`
    that derives the lock path via :func:`_lock_path`. No-op on platforms
    without ``fcntl`` (e.g. Windows). The lock file is created on demand
    and never deleted — flock semantics handle reuse.
    """
    with advisory_flock(_lock_path(target)):
        yield


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* atomically (tmp + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
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
        result: dict = json.loads(path.read_text())
        return result
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("session: skipping unreadable %s (%s)", path, exc)
        return None
