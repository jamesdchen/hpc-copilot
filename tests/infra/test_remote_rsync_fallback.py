"""Tests for the rsync-absent fallback in hpc_agent.infra.transport.

The transport layer detects rsync via ``shutil.which("rsync")``; when
absent (typically Windows without WSL/MSYS), :func:`rsync_push` routes
to a ``tar c | ssh tar x`` pipeline and :func:`rsync_pull` routes to
``scp -r``. These tests mock ``shutil.which`` and the subprocess
helpers to verify routing without requiring a real cluster.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from hpc_agent.infra import transport

if TYPE_CHECKING:
    from pathlib import Path


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_have_rsync_reports_truthy_when_present() -> None:
    with patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"):
        assert transport._have_rsync() is True


def test_have_rsync_reports_false_when_absent() -> None:
    with patch("hpc_agent.infra.transport.shutil.which", return_value=None):
        assert transport._have_rsync() is False


def test_rsync_push_uses_rsync_when_available(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()) as run_mock,
    ):
        transport.rsync_push(ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[])
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"
    assert "-az" in cmd
    assert "--delete" in cmd


def test_rsync_push_falls_back_to_tar_when_rsync_missing(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    fake_run = _ok()
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=fake_run) as run_mock,
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0

        # delete=False here keeps this test focused on tar-exclude
        # routing; the delete=True pre-clean path has its own coverage
        # (test_rsync_push_fallback_delete_true_runs_remote_preclean).
        result = transport.rsync_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            exclude=[".git/", "__pycache__/"],
            delete=False,
        )
    assert result.returncode == 0
    # tar got spawned with the right excludes
    tar_cmd = popen_mock.call_args[0][0]
    assert tar_cmd[0] == "tar"
    assert "--exclude=.git" in tar_cmd
    assert "--exclude=__pycache__" in tar_cmd
    # ssh got spawned with the remote tar x command
    ssh_cmd = run_mock.call_args[0][0]
    assert ssh_cmd[0] == "ssh"
    assert "u@h" in ssh_cmd


def _tar_fallback_remote_cmd(tmp_path: Path, *, exclude: list[str], delete: bool) -> str:
    """Run rsync_push in tar-fallback mode; return the remote shell command
    string handed to ssh (the last element of the ssh argv)."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()) as run_mock,
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            exclude=exclude,
            delete=delete,
        )
    ssh_cmd = run_mock.call_args[0][0]
    assert ssh_cmd[0] == "ssh"
    return str(ssh_cmd[-1])


def test_rsync_push_fallback_delete_true_runs_remote_preclean(tmp_path: Path) -> None:
    """delete=True on the tar fallback emulates rsync --delete: a remote
    pre-clean (find ... | xargs rm -rf) runs before the tar extract so
    stale remote files cannot survive a re-push."""
    remote_cmd = _tar_fallback_remote_cmd(tmp_path, exclude=[], delete=True)
    assert "mkdir -p /r" in remote_cmd
    assert "find /r -mindepth 1" in remote_cmd
    assert "xargs -0 -r rm -rf --" in remote_cmd
    assert "tar x -C /r" in remote_cmd
    # the pre-clean must run BEFORE the extract
    assert remote_cmd.index("find /r") < remote_cmd.index("tar x")


def test_rsync_push_fallback_delete_false_skips_preclean(tmp_path: Path) -> None:
    """delete=False keeps the additive behavior — no remote deletion."""
    remote_cmd = _tar_fallback_remote_cmd(tmp_path, exclude=[], delete=False)
    assert "tar x -C /r" in remote_cmd
    assert "find" not in remote_cmd
    assert "rm -rf" not in remote_cmd


def test_remote_clean_cmd_anchors_excludes() -> None:
    """A bare name prunes at any depth (-name); an internal-slash pattern
    is anchored to the sync root (-path) — mirroring rsync's exclude rule."""
    cmd = transport._remote_clean_cmd("/r", [".git/", "*.pyc", ".hpc/_hpc_dispatch.py"])
    # shlex.quote leaves metachar-free tokens bare and quotes only what
    # needs it (the glob), so the remote shell cannot expand ``*.pyc``.
    assert "-name .git" in cmd
    assert "-name '*.pyc'" in cmd
    assert "-path /r/.hpc/_hpc_dispatch.py" in cmd
    assert "-prune -o -print0" in cmd


def test_remote_clean_cmd_protects_framework_files() -> None:
    """The framework files in DEFAULT_RSYNC_EXCLUDES land in prune clauses,
    so the pre-clean preserves them; a non-excluded stale file is not
    pruned and therefore gets deleted by the trailing rm -rf."""
    cmd = transport._remote_clean_cmd("/r", transport.DEFAULT_RSYNC_EXCLUDES)
    assert "-path /r/.hpc/_hpc_dispatch.py" in cmd
    assert "-path /r/.hpc/_hpc_combiner.py" in cmd
    assert "-name hpc_agent" in cmd  # deployed runtime stubs
    assert cmd.endswith("-print0 | xargs -0 -r rm -rf --")


def test_remote_clean_cmd_empty_exclude_deletes_whole_subtree() -> None:
    """With no excludes the pre-clean removes everything under remote_path
    (but never remote_path itself — guarded by -mindepth 1)."""
    cmd = transport._remote_clean_cmd("/r", [])
    assert cmd == "find /r -mindepth 1 -print0 | xargs -0 -r rm -rf --"


def test_rsync_pull_uses_rsync_when_available(tmp_path: Path) -> None:
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()) as run_mock,
    ):
        transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"


def test_rsync_pull_falls_back_to_scp_when_rsync_missing(tmp_path: Path) -> None:
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()) as run_mock,
    ):
        transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "scp"
    assert "-r" in cmd
    assert any("u@h:/r/_combiner/" in arg for arg in cmd)
    assert (tmp_path / "out").exists()


def test_tar_push_propagates_ssh_failure(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    fail = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="ssh: connect refused"
    )
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=fail),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0

        result = transport.rsync_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            exclude=[],
            delete=False,
        )
    assert result.returncode == 2
    assert "connect refused" in result.stderr
