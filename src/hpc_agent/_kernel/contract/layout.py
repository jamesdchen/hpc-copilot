"""Path resolution for HPC experiment + journal trees.

Two frozen dataclasses, distinguished by *type* so static checkers and
human readers cannot confuse one for the other:

* :class:`RepoLayout` — the experiment-relative ``.hpc/`` tree
  (``tasks.py``, run sidecars, runtime priors). Lives at
  ``<experiment_dir>/.hpc/``.
* :class:`JournalLayout` — the cross-experiment journal tree under
  ``~/.claude/hpc/<repo_hash>/``. Holds ``RunRecord`` JSON, last-status
  snapshots, monitor tick logs, and the journal index.

The pre-B1 codebase used several scattered path helpers
(``framework_subdir``, ``runs_subdir``, ``tasks_path``,
``run_sidecar_path``, ``runtime_path``, ``journal_dir``, ``runs_dir``)
plus a ``runs_dir`` (journal) / ``runs_subdir`` (cluster sidecar) name
collision that caused a P0 ``build_wave_map`` bug.
``RepoLayout``/``JournalLayout`` give those helpers a single, type-safe
home.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hpc_agent import errors

__all__ = ["RepoLayout", "JournalLayout"]


@dataclass(frozen=True)
class RepoLayout:
    """Per-experiment paths under ``<experiment_dir>/.hpc/``.

    All derived paths are computed off ``experiment_dir.resolve()`` so
    writers and readers from different cwds see the same files.
    Properties that materialize a directory (``hpc``, ``runs``) create
    it lazily and idempotently. Methods that return a *file* path do
    not create the file.
    """

    experiment_dir: Path

    @property
    def root(self) -> Path:
        """Resolved absolute experiment directory."""
        return Path(self.experiment_dir).resolve()

    @property
    def hpc(self) -> Path:
        """``.hpc/`` subdirectory; created on first access.

        Also writes ``.hpc/.gitignore`` (ignoring ``runs/``) on first
        call so per-run sidecars don't pollute the user's git history
        while ``tasks.py`` stays tracked.
        """
        sub = self.root / ".hpc"
        sub.mkdir(parents=True, exist_ok=True)
        gitignore = sub / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("runs/\n", encoding="utf-8")
        return sub

    @property
    def runs(self) -> Path:
        """``.hpc/runs/`` — per-run sidecar directory; created lazily."""
        d = self.hpc / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def runtimes(self) -> Path:
        """``.hpc/runtimes/`` — runtime-prior directory.

        Note: callers (``runtime_prior``) decide when to ``mkdir``;
        this property does NOT create the directory so read-only paths
        don't have side effects.
        """
        return self.hpc / "runtimes"

    @property
    def tasks(self) -> Path:
        """``.hpc/tasks.py`` — does not create the file."""
        return self.hpc / "tasks.py"

    def run_sidecar(self, run_id: str) -> Path:
        """``.hpc/runs/<run_id>.json`` — per-run sidecar JSON."""
        return self.runs / f"{run_id}.json"

    def runtime_prior(self, profile: str, cluster: str) -> Path:
        """``.hpc/runtimes/<profile>.<cluster>.json``.

        ``profile`` / ``cluster`` may contain ``/`` (e.g. ``foo/bar``); we
        substitute ``_`` so the resulting filename is portable and a path-like
        token cannot escape the ``runtimes`` dir — the same defensive
        substitution ``cluster_history`` / ``preflight_marker`` apply to
        ``cluster``.
        """
        if not profile:
            raise errors.SpecInvalid("profile must be non-empty")
        if not cluster:
            raise errors.SpecInvalid("cluster must be non-empty")
        safe_profile = profile.replace("/", "_")
        safe_cluster = cluster.replace("/", "_")
        return self.runtimes / f"{safe_profile}.{safe_cluster}.json"

    def cluster_history(self, cluster: str) -> Path:
        """``.hpc/cluster_history/<cluster>/`` — created on first access.

        Persisted ``ClusterSnapshot`` JSON files live here (one file per
        snapshot, named ``<unix_ts>.json``). The directory is created
        eagerly on first read so callers can probe ``list(...)`` without
        guarding on ``exists()`` — same lazy-mkdir pattern as
        :attr:`runs`.
        """
        if not cluster:
            raise errors.SpecInvalid("cluster must be non-empty")
        # Sanitize separators in case a caller passes a path-like cluster
        # name; the historical naming has only used flat tokens but this
        # keeps us tolerant if that changes.
        safe_cluster = cluster.replace("/", "_")
        d = self.hpc / "cluster_history" / safe_cluster
        d.mkdir(parents=True, exist_ok=True)
        return d


@dataclass(frozen=True)
class JournalLayout:
    """Cross-experiment journal under ``~/.claude/hpc/<repo_hash>/``.

    Holds ``RunRecord`` JSON, last-status snapshots, monitor jsonl tick
    logs, and the journal index. Distinct from :class:`RepoLayout` —
    the type system enforces the separation, eliminating the
    pre-B1 ``runs_dir`` (journal) vs ``runs_subdir`` (cluster sidecar)
    name collision.
    """

    experiment_dir: Path

    @property
    def repo_hash(self) -> str:
        """12-char sha256 digest of the resolved experiment dir.

        Delegates to :func:`hpc_agent.state.run_record.repo_hash` so the
        canonical implementation stays in one place during the B1
        migration.
        """
        from hpc_agent.state.run_record import repo_hash as _rh

        return _rh(self.experiment_dir)

    @property
    def root(self) -> Path:
        """``~/.claude/hpc/<repo_hash>/`` (or ``$HPC_JOURNAL_DIR``)."""
        from hpc_agent.state.run_record import journal_dir

        return journal_dir(self.experiment_dir)

    @property
    def runs(self) -> Path:
        """``<journal_root>/runs/`` — created lazily."""
        d = self.root / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def run_record(self, run_id: str) -> Path:
        """``<journal_root>/runs/<run_id>.json`` — the RunRecord JSON."""
        return self.runs / f"{run_id}.json"

    def last_status(self, run_id: str) -> Path:
        """``<journal_root>/runs/<run_id>.last_status.json`` cache snapshot."""
        return self.runs / f"{run_id}.last_status.json"

    def monitor_jsonl(self, run_id: str) -> Path:
        """``<journal_root>/runs/<run_id>.monitor.jsonl`` tick log."""
        return self.runs / f"{run_id}.monitor.jsonl"

    def index(self) -> Path:
        """``<journal_root>/index.json`` — run_id -> {status, updated_at}."""
        return self.root / "index.json"

    def preflight_marker(self, cluster: str) -> Path:
        """``<journal_root>/preflight-<cluster>.json`` — per-cluster cache marker.

        The single source of truth for the preflight cache-marker path.
        ``hpc-preflight`` writes ``{checked_at, all_ok, cluster}`` here on a
        green run; ``hpc-submit``'s pre-flight gate reads it and skips the
        re-check while the marker is under its TTL. Both skills' markdown
        document the same ``~/.claude/hpc/<repo_hash>/preflight-<cluster>.json``
        literal — keep them in step with this method.
        """
        if not cluster:
            raise errors.SpecInvalid("cluster must be non-empty")
        # Sanitize separators so a path-like cluster token can't escape the
        # journal root — same defensive substitution as ``cluster_history``.
        safe_cluster = cluster.replace("/", "_")
        return self.root / f"preflight-{safe_cluster}.json"
