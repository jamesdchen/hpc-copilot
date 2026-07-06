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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=fake_run) as run_mock,
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
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
    assert eff == (
        ["only_this/"]
        + transport.MANDATORY_RSYNC_EXCLUDES
        + transport.PROTECTED_OUTPUT_DIRS
        + transport.PROTECTED_RUNTIME_FILES
    )
    # None selects the defaults, still with the credential exclude appended.
    assert "clusters.yaml" in transport._effective_excludes(None)


def test_effective_excludes_always_protects_output_dirs() -> None:
    """#173: cluster run-output dirs (results/, _combiner/, logs/) are unioned
    into every push's exclude set so a caller's incomplete list can't expose them
    to --delete / the tar pre-clean. De-duplicated when already present."""
    assert transport.PROTECTED_OUTPUT_DIRS == ["results/", "_combiner/", "logs/"]
    # Absent from the caller list -> appended.
    eff = transport._effective_excludes(["only_this/"])
    assert "results/" in eff
    assert "_combiner/" in eff
    assert "logs/" in eff  # scheduler log dir — never --delete'd (else it becomes a file)
    # Already present -> not duplicated.
    eff2 = transport._effective_excludes(["results/", "x/"])
    assert eff2.count("results/") == 1
    # The packaged defaults path also carries them.
    assert "results/" in transport._effective_excludes(None)


def test_effective_excludes_always_protects_runtime_files() -> None:
    """A caller-supplied exclude must NOT drop the deploy_runtime-placed
    framework files (.hpc/templates/ etc.). Regression (2026-06-08): a custom
    rsync_excludes replaced DEFAULT_RSYNC_EXCLUDES, so .hpc/templates/ lost its
    --delete protection and the remote pre-clean wiped the cluster preamble —
    every array task then died with `hpc_preamble.sh: No such file or directory`
    (~26ms exit 1 on SGE) while the canary that ran before the wipe passed."""
    assert transport.PROTECTED_RUNTIME_FILES == [
        "hpc_agent/",
        ".hpc/_hpc_dispatch.py",
        ".hpc/_hpc_combiner.py",
        ".hpc/templates/",
    ]
    # A custom exclude that names none of them still carries them all.
    eff = transport._effective_excludes(["only_this/"])
    for pat in transport.PROTECTED_RUNTIME_FILES:
        assert pat in eff, f"{pat} dropped when a custom exclude is supplied"
    # And the remote pre-clean prunes .hpc/templates/ given that set, so the
    # deployed preamble survives the --delete pass.
    clean_cmd = transport._remote_clean_cmd("/scratch/run", eff)
    assert "/scratch/run/.hpc/templates" in clean_cmd
    # The packaged-defaults path carries them too (de-duplicated, not doubled).
    assert transport._effective_excludes(None).count(".hpc/templates/") == 1


def test_tar_fallback_always_excludes_credentials(tmp_path: Path) -> None:
    """Even on the rsync-absent tar path, clusters.yaml is never tarred,
    regardless of the caller's exclude list — issue #149."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
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
    import os

    args = call_args[0]
    if not args:
        return False
    cmd = args[0]
    if not isinstance(cmd, list) or len(cmd) < 2:
        return False
    # Cross-platform: on Linux ``_ssh_binary()`` returns ``"ssh"``; on
    # Windows it returns ``r"C:\Windows\System32\OpenSSH\ssh.exe"``.
    # ``os.path.basename(...).lower()`` collapses both to the bare name
    # so the predicate fires on either.
    basename = os.path.basename(str(cmd[0])).lower()
    return basename in ("ssh", "ssh.exe") and cmd[1] == "-V"


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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
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
    assert calls[0][1]["timeout_sec"] == transport.PRECLEAN_TIMEOUT_SEC
    assert calls[0][1]["timeout_sec"] < calls[-1][1]["timeout_sec"]


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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
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
            "hpc_agent.infra.transport.run_capture_bounded",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=300),
        ),
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=fail) as run_mock,
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert result.returncode == 5
    assert "clean blew up" in result.stderr
    # Filter the ssh -V version probe (see :func:`_is_ssh_version_probe`)
    # so the assertion is cache-state-agnostic across xdist workers.
    transfer_calls = [c for c in run_mock.call_args_list if not _is_ssh_version_probe(c)]
    assert len(transfer_calls) == 1, f"unexpected transfer calls: {transfer_calls}"
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
    """The deploy_runtime framework files (PROTECTED_RUNTIME_FILES, always
    unioned into the effective exclude set) land in prune clauses, so the
    pre-clean preserves them; a non-excluded stale file is not pruned and
    therefore gets deleted by the trailing rm -rf."""
    cmd = transport._remote_clean_cmd("/r", transport._effective_excludes(None))
    assert "-path /r/.hpc/_hpc_dispatch.py" in cmd
    assert "-path /r/.hpc/_hpc_combiner.py" in cmd
    assert "-path /r/.hpc/templates" in cmd  # the preamble's home
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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
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
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
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
    # scp receives the dir WITHOUT a trailing slash (it copies the dir itself
    # into a staging area, which _scp_pull then flattens into local_dir — see
    # test_scp_fallback_does_not_double_nest).
    assert any(a == "u@h:/r/_combiner" for a in cmd)
    assert (tmp_path / "out").exists()


def test_scp_fallback_does_not_double_nest(tmp_path: Path) -> None:
    """scp -r copies the directory itself (not its contents); _scp_pull must
    flatten the staging copy into local_dir so there is no ``_combiner/_combiner/``
    nesting — the Windows (rsync-absent) aggregate bug that broke
    verify-aggregation-complete."""
    from pathlib import Path as _Path  # module-level import is TYPE_CHECKING-only

    out = tmp_path / "_combiner"

    def fake_scp(cmd, **kwargs):
        # Emulate ``scp -r remote:.../_combiner <staging>`` creating
        # ``<staging>/_combiner/wave_0.json`` (scp copies the dir, not contents).
        staging = _Path(cmd[-1])
        nested = staging / "_combiner"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "wave_0.json").write_text("{}", encoding="utf-8")
        return _ok()

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=fake_scp),
    ):
        transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=str(out),
        )
    assert (out / "wave_0.json").is_file()  # flattened into local_dir
    assert not (out / "_combiner").exists()  # NOT double-nested


def test_tar_push_propagates_ssh_failure(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hi")
    fail = subprocess.CompletedProcess(
        args=[], returncode=2, stdout="", stderr="ssh: connect refused"
    )
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=fail),
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
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
