"""Tests for ``hpc_agent._kernel.contract.layout``.

The B1 refactor introduced ``RepoLayout`` and ``JournalLayout`` to
replace eight scattered path helpers and to make the ``runs_dir``
(journal) vs ``runs_subdir`` (cluster sidecar) name collision a type
error rather than a P0 bug waiting to happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent._kernel.contract.layout import JournalLayout, RepoLayout


def test_repo_layout_root_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rel = Path(".")
    layout = RepoLayout(rel)
    assert layout.root.is_absolute()
    assert layout.root == tmp_path.resolve()


def test_repo_layout_hpc_creates_dir_and_gitignore(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    assert not (tmp_path / ".hpc").exists()
    hpc = layout.hpc
    assert hpc.is_dir()
    assert (hpc / ".gitignore").read_text() == "runs/\n"


def test_repo_layout_runs_creates_dir(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    runs = layout.runs
    assert runs.is_dir()
    assert runs == tmp_path.resolve() / ".hpc" / "runs"


def test_repo_layout_runtimes_does_not_create(tmp_path: Path) -> None:
    """``runtimes`` must NOT mkdir — read-only paths have no side effects."""
    layout = RepoLayout(tmp_path)
    runtimes = layout.runtimes
    assert not runtimes.exists()


def test_repo_layout_tasks_path(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    assert layout.tasks == tmp_path.resolve() / ".hpc" / "tasks.py"
    assert not layout.tasks.exists()


def test_repo_layout_run_sidecar(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    p = layout.run_sidecar("abc123")
    assert p == tmp_path.resolve() / ".hpc" / "runs" / "abc123.json"


def test_repo_layout_runtime_prior(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    p = layout.runtime_prior("profileA", "cluster1")
    assert p == tmp_path.resolve() / ".hpc" / "runtimes" / "profileA.cluster1.json"


def test_repo_layout_runtime_prior_sanitizes_slash(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    p = layout.runtime_prior("foo/bar", "cluster1")
    assert p.name == "foo_bar.cluster1.json"


def test_repo_layout_runtime_prior_rejects_empty(tmp_path: Path) -> None:
    layout = RepoLayout(tmp_path)
    with pytest.raises(ValueError):
        layout.runtime_prior("", "c")
    with pytest.raises(ValueError):
        layout.runtime_prior("p", "")


def test_journal_layout_runs_distinct_from_repo_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of B1: these two paths must be distinct.

    Pre-B1 they collided in agent-cli code that did ``runs_dir(...)``
    expecting one and getting the other.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    repo = RepoLayout(tmp_path)
    journal = JournalLayout(tmp_path)
    assert repo.runs != journal.runs


def test_journal_layout_root_honors_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ``journal_dir`` re-resolves ``HPC_JOURNAL_DIR`` from os.environ on
    # every call (v3 fix for the test-leak bug class), so the prior
    # ``importlib.reload`` dance is no longer needed — and was itself
    # buggy: the finally-block reload ran while monkeypatch's env value
    # was still live, leaving ``session.HPC_HOMEDIR`` /
    # ``run_record.HPC_HOMEDIR`` permanently bound to tmp_path across
    # the rest of the session (v3 BUG-8V3-2/6).
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    journal = JournalLayout(tmp_path)
    assert str(journal.root).startswith(str(tmp_path / "journal"))


def test_journal_layout_run_record_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    journal = JournalLayout(tmp_path)
    rec = journal.run_record("run-1")
    assert rec.name == "run-1.json"
    assert rec.parent == journal.runs
    last = journal.last_status("run-1")
    assert last.name == "run-1.last_status.json"
    mon = journal.monitor_jsonl("run-1")
    assert mon.name == "run-1.monitor.jsonl"
    idx = journal.index()
    assert idx.name == "index.json"
    assert idx.parent == journal.root


def test_journal_layout_preflight_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    journal = JournalLayout(tmp_path)
    marker = journal.preflight_marker("perlmutter")
    assert marker.name == "preflight-perlmutter.json"
    assert marker.parent == journal.root


def test_journal_layout_preflight_marker_sanitizes_slash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    journal = JournalLayout(tmp_path)
    marker = journal.preflight_marker("site/cluster")
    assert marker.name == "preflight-site_cluster.json"
    assert marker.parent == journal.root


def test_journal_layout_preflight_marker_rejects_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    journal = JournalLayout(tmp_path)
    with pytest.raises(ValueError, match="cluster must be non-empty"):
        journal.preflight_marker("")
