"""Tests for the rsync-absent fallback in claude_hpc.infra.remote.

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

from claude_hpc.infra import remote

if TYPE_CHECKING:
    from pathlib import Path


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_have_rsync_reports_truthy_when_present() -> None:
    with patch("claude_hpc.infra.remote.shutil.which", return_value="/usr/bin/rsync"):
        assert remote._have_rsync() is True


def test_have_rsync_reports_false_when_absent() -> None:
    with patch("claude_hpc.infra.remote.shutil.which", return_value=None):
        assert remote._have_rsync() is False


def test_rsync_push_uses_rsync_when_available(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("claude_hpc.infra.remote.shutil.which", return_value="/usr/bin/rsync"),
        patch("claude_hpc.infra.remote.subprocess.run", return_value=_ok()) as run_mock,
    ):
        remote.rsync_push(host="h", user="u", remote_path="/r", local_path=tmp_path, exclude=[])
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"
    assert "-az" in cmd
    assert "--delete" in cmd


def test_rsync_push_falls_back_to_tar_when_rsync_missing(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    fake_run = _ok()
    with (
        patch("claude_hpc.infra.remote.shutil.which", return_value=None),
        patch("claude_hpc.infra.remote.subprocess.run", return_value=fake_run) as run_mock,
        patch("claude_hpc.infra.remote.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0

        result = remote.rsync_push(
            host="h",
            user="u",
            remote_path="/r",
            local_path=tmp_path,
            exclude=[".git/", "__pycache__/"],
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
    assert "tar x" in " ".join(ssh_cmd)
    assert "mkdir -p" in " ".join(ssh_cmd)


def test_rsync_pull_uses_rsync_when_available(tmp_path: Path) -> None:
    with (
        patch("claude_hpc.infra.remote.shutil.which", return_value="/usr/bin/rsync"),
        patch("claude_hpc.infra.remote.subprocess.run", return_value=_ok()) as run_mock,
    ):
        remote.rsync_pull(
            host="h",
            user="u",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"


def test_rsync_pull_falls_back_to_scp_when_rsync_missing(tmp_path: Path) -> None:
    with (
        patch("claude_hpc.infra.remote.shutil.which", return_value=None),
        patch("claude_hpc.infra.remote.subprocess.run", return_value=_ok()) as run_mock,
    ):
        remote.rsync_pull(
            host="h",
            user="u",
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
        patch("claude_hpc.infra.remote.shutil.which", return_value=None),
        patch("claude_hpc.infra.remote.subprocess.run", return_value=fail),
        patch("claude_hpc.infra.remote.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0

        result = remote.rsync_push(
            host="h", user="u", remote_path="/r", local_path=tmp_path, exclude=[]
        )
    assert result.returncode == 2
    assert "connect refused" in result.stderr
