"""Per-run journal — data layer.

Owns the RunRecord dataclass + the layout / locking / atomic-write
primitives that every higher-level helper in
``hpc_agent.state`` builds on.

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
lives in the per-subject runners under :mod:`hpc_agent.ops`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from hpc_agent._kernel.lifecycle.lifecycle import TERMINAL_STATUSES as _LIFECYCLE_TERMINAL
from hpc_agent.infra.io import advisory_flock

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


def _current_homedir() -> Path:
    """Re-resolve the journal home on every call.

    Lookup order:
    1. ``HPC_JOURNAL_DIR`` env var — wins always, so
       ``monkeypatch.setenv("HPC_JOURNAL_DIR", tmp_path)`` alone is
       enough to redirect state writes (v3 BUG-8V3-1 root cause was a
       cached import-time snapshot that ignored the env override).
    2. The module-level ``HPC_HOMEDIR`` attribute — back-compat with the
       pre-v3 ``monkeypatch.setattr(session, "HPC_HOMEDIR", tmp_path)``
       pattern used in ~20 test files. Patching the attribute still
       redirects as long as the env var is unset.
    3. ``~/.claude/hpc`` — the default.
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    # ``if env_val`` previously fell through to the default branch when
    # the env var was set to the empty string; v3 precedence says env
    # wins (including the explicit empty-string case → still treat
    # empty as unset, but distinguish None from "" elsewhere).
    if env_val is not None and env_val != "":
        return Path(env_val)
    attr = globals().get("HPC_HOMEDIR")
    if isinstance(attr, Path):
        return attr
    return Path.home() / ".claude" / "hpc"


# Import-time snapshot kept as a public attribute for back-compat —
# read-mostly callers (capabilities envelope, doc-gen) that just want
# the configured location are fine with the snapshot. State-touching
# call sites (``journal_dir``, ``find_in_flight_runs``,
# ``find_runs_by_campaign``) go through ``_current_homedir()`` so
# per-test env redirection actually applies.
_env_journal_dir = os.environ.get("HPC_JOURNAL_DIR")
HPC_HOMEDIR = (
    Path(_env_journal_dir)
    if _env_journal_dir is not None and _env_journal_dir != ""
    else Path.home() / ".claude" / "hpc"
)

# Re-exported for back-compat. Derived from the canonical
# hpc_agent._kernel.lifecycle.lifecycle.JournalStatus StrEnum so the literal can
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
        "recent_resubmit_request_ids",
        "pending_resubmit",
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
    # Bounded list of recent resubmit request_ids so an A→B→A replay
    # sequence is correctly recognised as a duplicate of A (the
    # ``last_resubmit_request_id`` alone only catches back-to-back
    # replays and double-increments retry counters otherwise). Newest
    # last; capped via ``_MAX_RECENT_RESUBMIT_IDS`` at write time.
    recent_resubmit_request_ids: list[str] = dataclasses.field(default_factory=list)
    # Closed-loop campaign tag. Empty string for open-loop submits.
    # Populated when /submit was invoked with --campaign-id (or with
    # campaign_id set on the submit spec). The asyncio campaign loop
    # uses ``find_runs_by_campaign`` to discover its in-flight set on
    # resume.
    campaign_id: str = ""
    # Resume marker for a multi-batch resubmit that failed partway. When
    # non-empty: ``{"request_id": <rid>, "job_ids": [<ids landed so
    # far>]}``. resubmit_flow uses it to continue from the next
    # un-submitted batch instead of re-running the whole plan (double
    # submit) or skipping the remainder. Cleared once the resubmit
    # completes fully.
    pending_resubmit: dict = dataclasses.field(default_factory=dict)
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
    from hpc_agent.infra.time import utcnow_iso

    d = _current_homedir() / repo_hash(experiment_dir)
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
    :attr:`hpc_agent._kernel.contract.layout.RepoLayout.runs`. The
    pre-B1 collision between these two ``runs_*`` names was a P0 bug
    source; the ``RepoLayout`` / ``JournalLayout`` type split makes
    it a type
    error.
    """
    from hpc_agent._kernel.contract.layout import JournalLayout

    return JournalLayout(experiment_dir).runs


def _run_path(experiment_dir: Path, run_id: str) -> Path:
    """Deprecated alias for ``JournalLayout(experiment_dir).run_record(run_id)``."""
    from hpc_agent._kernel.contract.layout import JournalLayout

    return JournalLayout(experiment_dir).run_record(run_id)


def _lock_path(target: Path) -> Path:
    return target.with_suffix(target.suffix + ".lock")


@contextlib.contextmanager
def _locked(target: Path) -> Iterator[None]:
    """Acquire an exclusive flock on a sibling ``.lock`` file for *target*.

    Thin wrapper around :func:`hpc_agent.infra.io.advisory_flock`
    that derives the lock path via :func:`_lock_path`. No-op on platforms
    without ``fcntl`` (e.g. Windows). The lock file is created on demand
    and never deleted — flock semantics handle reuse.
    """
    with advisory_flock(_lock_path(target)):
        yield


def _atomic_write_json(path: Path, payload: dict, *, fsync: bool = True) -> None:
    """Deprecated forwarder for :func:`hpc_agent.infra.io.atomic_write_json`.

    Kept so callers that import this module-level name don't break;
    new code should import ``atomic_write_json`` from
    ``hpc_agent.infra.io`` directly. This forwarder will be
    removed in a future release.

    The optional ``fsync`` kwarg is forwarded so the hot-path monitor
    tick can write a non-authoritative cache without a redundant fsync
    — see the canonical helper's docstring for the durability tradeoff.
    """
    from hpc_agent.infra.io import atomic_write_json

    atomic_write_json(path, payload, fsync=fsync)


def _read_json(path: Path) -> dict | None:
    try:
        result: dict = json.loads(path.read_text(encoding="utf-8"))
        return result
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _log.warning("session: skipping unreadable %s (%s)", path, exc)
        return None
