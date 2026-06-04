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

import pytest

from hpc_agent.infra import transport
from hpc_agent.infra.ssh_options import _scp_binary, _ssh_binary

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
    assert ssh_cmd[0] == _ssh_binary()
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
    assert ssh_cmd[0] == _ssh_binary()
    return str(ssh_cmd[-1])


def test_defaults_exclude_venvs_and_credentials() -> None:
    """The default exclude set drops virtualenvs (perf) and never ships
    the credential file clusters.yaml (security) — see issue #149."""
    assert ".venv/" in transport.DEFAULT_RSYNC_EXCLUDES
    assert "venv/" in transport.DEFAULT_RSYNC_EXCLUDES
    assert "node_modules/" in transport.DEFAULT_RSYNC_EXCLUDES
    assert "clusters.yaml" in transport.MANDATORY_RSYNC_EXCLUDES


def test_mandatory_excludes_cannot_be_dropped_by_caller() -> None:
    """A caller-supplied exclude list cannot re-expose clusters.yaml, and always
    carries the protected output dirs (#173)."""
    eff = transport._effective_excludes(["only_this/"])
    assert eff == ["only_this/", "clusters.yaml", "results/", "_combiner/"]
    # None selects the defaults, still with the credential exclude appended.
    assert "clusters.yaml" in transport._effective_excludes(None)


def test_effective_excludes_always_protects_output_dirs() -> None:
    """#173: cluster run-output dirs (results/, _combiner/) are unioned into
    every push's exclude set so a caller's incomplete list can't expose them to
    --delete / the tar pre-clean. De-duplicated when already present."""
    assert transport.PROTECTED_OUTPUT_DIRS == ["results/", "_combiner/"]
    # Absent from the caller list -> appended.
    eff = transport._effective_excludes(["only_this/"])
    assert "results/" in eff
    assert "_combiner/" in eff
    # Already present -> not duplicated.
    eff2 = transport._effective_excludes(["results/", "x/"])
    assert eff2.count("results/") == 1
    # The packaged defaults path also carries them.
    assert "results/" in transport._effective_excludes(None)


def test_tar_fallback_always_excludes_credentials(tmp_path: Path) -> None:
    """Even on the rsync-absent tar path, clusters.yaml is never tarred,
    regardless of the caller's exclude list — issue #149."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
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
            exclude=["custom/"],
            delete=False,
        )
    tar_cmd = popen_mock.call_args[0][0]
    assert "--exclude=clusters.yaml" in tar_cmd


def _is_ssh_version_probe(call_args) -> bool:
    """``True`` when *call_args* is the ``ssh -V`` lazy version probe from
    :func:`hpc_agent.infra.ssh_options._local_openssh_major`.

    The probe fires the first time ``_ssh_crypto_opts`` evaluates
    ``_local_openssh_supports_gcm`` after that function's
    ``functools.cache`` is empty. Other test files (e.g.
    ``test_remote_windows_compat.py``) have fixtures that ``cache_clear()``
    the named-pipe + GCM probes between tests; if they ran before this
    fallback test in the same xdist worker, the cache is cold here and the
    probe fires inside the ``transport.subprocess.run`` mock scope (the
    patch is on the global ``subprocess`` module, so ``ssh_options``'s
    calls land in it too). The probe is unrelated to the tar/ssh push
    semantics this file pins, so filter it at the helper boundary rather
    than mock it or pre-warm the cache (those would couple every test to
    a global-cache invariant).
    """
    args = call_args[0]
    if not args:
        return False
    cmd = args[0]
    return (
        isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] in ("ssh", "ssh.exe") and cmd[1] == "-V"
    )


def _tar_fallback_run_calls(tmp_path: Path, *, exclude: list[str], delete: bool):
    """Run rsync_push in tar-fallback mode; return the mocked subprocess.run
    call list (call 0 = pre-clean when delete=True, last = tar extract).

    The lazy ``ssh -V`` version probe (see :func:`_is_ssh_version_probe`)
    is filtered out — it can appear at unpredictable positions depending
    on the xdist worker's per-process cache state, but is unrelated to
    the tar/ssh push semantics pinned here.
    """
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
    return [c for c in run_mock.call_args_list if not _is_ssh_version_probe(c)]


def test_rsync_push_fallback_delete_true_runs_remote_preclean(tmp_path: Path) -> None:
    """delete=True on the tar fallback emulates rsync --delete: a remote
    pre-clean (find ... | xargs rm -rf) runs as its OWN ssh call BEFORE the tar
    extract, so stale remote files cannot survive a re-push AND the clean can't
    eat the transfer budget (#173)."""
    calls = _tar_fallback_run_calls(tmp_path, exclude=[], delete=True)
    # Two ssh invocations now: the pre-clean (first), then the extract (last).
    assert len(calls) == 2
    preclean_cmd = str(calls[0][0][0][-1])
    extract_cmd = str(calls[-1][0][0][-1])
    assert "mkdir -p /r" in preclean_cmd
    assert "find /r -mindepth 1" in preclean_cmd
    assert "xargs -0 -r rm -rf --" in preclean_cmd
    assert "tar x" not in preclean_cmd  # pre-clean does not extract
    assert "tar x -C /r" in extract_cmd
    assert "rm -rf" not in extract_cmd  # extract does not delete
    # The pre-clean got its OWN bounded timeout, strictly shorter than the
    # transfer's, so a pathological clean fails fast instead of wedging.
    assert calls[0][1]["timeout"] == transport.PRECLEAN_TIMEOUT_SEC
    assert calls[0][1]["timeout"] < calls[-1][1]["timeout"]


def test_tar_fallback_preclean_prunes_output_dirs_even_if_caller_omits_them(
    tmp_path: Path,
) -> None:
    """#173 core regression: even when the caller's exclude omits results/, the
    tar pre-clean prunes the protected output dirs, so `find` never descends
    into the quarter-million-inode crash-loop debris under results/."""
    calls = _tar_fallback_run_calls(tmp_path, exclude=["custom/"], delete=True)
    preclean_cmd = str(calls[0][0][0][-1])
    # results/ and _combiner/ are bare names -> pruned at any depth.
    assert "-name results" in preclean_cmd
    assert "-name _combiner" in preclean_cmd
    assert "-prune -o" in preclean_cmd


def test_rsync_path_delete_protects_output_dirs(tmp_path: Path) -> None:
    """#173: on the rsync path too, --delete must not wipe cluster output —
    results/ and _combiner/ are excluded even when the caller omits them."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()) as run_mock,
    ):
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=["custom/"]
        )
    cmd = run_mock.call_args[0][0]
    assert "--delete" in cmd
    assert "results/" in cmd
    assert "_combiner/" in cmd


def test_tar_fallback_preclean_timeout_is_actionable(tmp_path: Path) -> None:
    """#173: a pre-clean that times out fails loud with an actionable message
    (mentions results/ debris + the delete=False escape hatch), not a silent
    wedge that consumes the whole transfer timeout."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch(
            "hpc_agent.infra.transport.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=300),
        ),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        with pytest.raises(TimeoutError, match="pre-clean"):
            transport.rsync_push(
                ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
            )
        # Never reached the transfer leg.
        popen_mock.assert_not_called()


def test_tar_fallback_preclean_failure_aborts_before_extract(tmp_path: Path) -> None:
    """#173: a pre-clean that fails (non-zero) surfaces as the push failure and
    the extract never runs onto a half-cleaned tree."""
    (tmp_path / "f.txt").write_text("hi")
    fail = subprocess.CompletedProcess(args=[], returncode=5, stdout="", stderr="clean blew up")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=fail) as run_mock,
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert result.returncode == 5
    assert "clean blew up" in result.stderr
    assert run_mock.call_count == 1  # only the pre-clean; no extract
    popen_mock.assert_not_called()


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
    assert cmd[0] == _scp_binary()
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
