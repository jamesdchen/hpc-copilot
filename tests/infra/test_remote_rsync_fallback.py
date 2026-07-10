"""Tests for the rsync-absent fallback in hpc_agent.infra.transport.

The transport layer detects rsync via ``shutil.which("rsync")``; when
absent (typically Windows without WSL/MSYS), :func:`rsync_push` routes
to a ``tar c | ssh tar x`` pipeline and :func:`rsync_pull` routes to
``scp -r``. These tests mock ``shutil.which`` and the subprocess
helpers to verify routing without requiring a real cluster.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
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
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
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
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
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
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
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
        # No remote hash manifest -> the full-copy tar shape this helper pins
        # (queue item 6b routes to full copy when the remote can't hash). The
        # delta shape has its own coverage below.
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
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
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
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


def test_rsync_push_fallback_delete_true_stages_then_swaps(tmp_path: Path) -> None:
    """delete=True on the tar fallback is STAGE-THEN-SWAP (run-#10 F-G): the
    extract lands in a sibling staging dir first; the live tree is touched
    only after a COMPLETE transfer — bounded clean, then a merge-copy swap
    (cp -a merges into the non-empty dirs the pre-clean preserves; mv could
    not). Four bounded ssh legs, in order: stage drop, extract-into-stage,
    pre-clean of the live tree, merge+cleanup."""
    calls = _tar_fallback_run_calls(tmp_path, exclude=[], delete=True)
    assert len(calls) == 4
    drop_cmd = str(calls[0][0][0][-1])
    extract_cmd = str(calls[1][0][0][-1])
    clean_cmd = str(calls[2][0][0][-1])
    move_cmd = str(calls[3][0][0][-1])
    assert "rm -rf /r.hpc_stage" in drop_cmd
    assert "tar x -C /r.hpc_stage" in extract_cmd  # NEVER extracts into /r
    assert "rm -rf" not in extract_cmd
    assert "find /r -mindepth 1" in clean_cmd
    assert "xargs -0 -r rm -f --" in clean_cmd
    assert "tar x" not in clean_cmd
    assert "cp -a /r.hpc_stage/. /r/" in move_cmd
    assert "rm -rf /r.hpc_stage" in move_cmd
    # The destructive legs carry their OWN bounded timeouts, strictly shorter
    # than the transfer's — and they run AFTER the transfer, so a transfer
    # timeout can no longer leave a gutted live tree.
    assert calls[0][1]["timeout_sec"] == transport.PRECLEAN_TIMEOUT_SEC
    assert calls[2][1]["timeout_sec"] == transport.PRECLEAN_TIMEOUT_SEC
    assert calls[2][1]["timeout_sec"] < calls[1][1]["timeout_sec"]


def test_tar_fallback_transfer_death_leaves_live_tree_untouched(tmp_path: Path) -> None:
    """F-G fires-test: a transfer that dies mid-flight (the run-#10 30-min
    timeout) must issue ZERO destructive commands against the live tree —
    only the stage drop and the failed extract ever ran."""
    (tmp_path / "f.txt").write_text("hi")
    ok = _ok()
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch(
            "hpc_agent.infra.transport.run_capture_bounded",
            side_effect=[ok, subprocess.TimeoutExpired(cmd="ssh", timeout=300)],
        ) as run_mock,
        # Force the full-copy path so the two-leg side_effect maps to
        # stage-drop then a dying extract (item 6b's delta leg is covered
        # separately); without this the manifest fetch would consume a leg.
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
        tar_proc.stderr = MagicMock()
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        with pytest.raises(TimeoutError):
            transport.rsync_push(
                ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
            )
    cmds = [str(c[0][0][-1]) for c in run_mock.call_args_list if not _is_ssh_version_probe(c)]
    # No pre-clean of /r, no merge-swap — the live tree was never touched.
    assert not any("find /r -mindepth 1" in c for c in cmds)
    assert not any("cp -a /r.hpc_stage/. /r/" in c for c in cmds)


def test_tar_fallback_preclean_prunes_output_dirs_even_if_caller_omits_them(
    tmp_path: Path,
) -> None:
    """#173 core regression: even when the caller's exclude omits results/, the
    post-extract clean prunes the protected output dirs, so `find` never
    descends into the quarter-million-inode crash-loop debris under results/."""
    calls = _tar_fallback_run_calls(tmp_path, exclude=["custom/"], delete=True)
    clean_cmd = str(calls[2][0][0][-1])
    # results/ and _combiner/ are bare names -> pruned at any depth.
    assert "-name results" in clean_cmd
    assert "-name _combiner" in clean_cmd
    assert "-prune -o" in clean_cmd


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
    """A pre-transfer leg that times out fails loud with the leg NAMED
    (stage-dir drop, under F-G's ordering), not a silent wedge that consumes
    the whole transfer timeout — and the transfer never starts."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch(
            "hpc_agent.infra.transport.run_capture_bounded",
            side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=300),
        ),
        # Full-copy path: the first bounded leg is then the stage-dir drop
        # (item 6b's manifest fetch would otherwise be the first timeout).
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
        # Absorb the lazy `ssh -V` probe (see _is_ssh_version_probe): the
        # Popen patch below is GLOBAL (transport.subprocess IS the stdlib
        # module), so a cold-cache probe would otherwise run the REAL
        # subprocess.run against the mocked Popen and explode in stdlib
        # unpacking (the f8585e9c windows-CI failure). Assertions read the
        # run_capture_bounded mock, so this absorber stays anonymous.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        with pytest.raises(TimeoutError, match="stage-dir drop"):
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
        # Full-copy path so the single non-probe leg is the pre-clean itself
        # (item 6b's manifest fetch would otherwise add a leg).
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
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


# --- re-push regression (audit 2026-07-09): execute the REAL remote command
# strings against a local tree, so the preclean/swap semantics are pinned by
# observed filesystem state, not by string shape alone. ---------------------

_needs_posix_shell = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="executes the remote-side POSIX commands locally",
)


def _first_deploy_remote_tree(remote: Path) -> None:
    """A live tree as it stands after a first deploy: pushed experiment files
    plus the deploy_runtime-placed protected framework files and run output."""
    for rel, content in {
        ".hpc/tasks.py": "old tasks",
        ".hpc/runs/r1.json": "{}",
        ".hpc/_hpc_dispatch.py": "dispatch",
        ".hpc/_hpc_combiner.py": "combiner",
        ".hpc/templates/common/hpc_preamble.sh": "preamble",
        "src/mod.py": "code v1",
        "src/old_pkg/gone.py": "stale module",
        "results/out.txt": "run output",
        "logs/job.o1.1": "task log",
        "hpc_agent/execution/mapreduce/metrics_io.py": "runtime stub",
    }.items():
        f = remote / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)


def _sh(cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=30)


@_needs_posix_shell
def test_preclean_preserves_protected_subtrees_on_disk(tmp_path: Path) -> None:
    """The pre-clean must delete stale unprotected files WITHOUT rm -rf-ing
    through the parent dirs of protected paths. The old single-pass
    ``rm -rf`` deleted ``.hpc/`` wholesale on every re-push — templates,
    dispatcher and all — because ``find`` prints the (unpruned) parent dir
    before descending to the pruned child."""
    remote = tmp_path / "remote"
    _first_deploy_remote_tree(remote)
    clean = _sh(transport._remote_clean_cmd(str(remote), transport._effective_excludes(None)))
    assert clean.returncode == 0, clean.stderr
    # Protected framework files and run output survive.
    assert (remote / ".hpc" / "_hpc_dispatch.py").is_file()
    assert (remote / ".hpc" / "_hpc_combiner.py").is_file()
    assert (remote / ".hpc" / "templates" / "common" / "hpc_preamble.sh").is_file()
    assert (remote / "results" / "out.txt").is_file()
    assert (remote / "logs" / "job.o1.1").is_file()
    assert (remote / "hpc_agent" / "execution" / "mapreduce" / "metrics_io.py").is_file()
    # Unprotected pushed content is cleaned (the fresh extract re-lands it),
    # including now-empty stale dirs (an empty leftover package dir would be
    # importable as a namespace package and shadow a real module).
    assert not (remote / ".hpc" / "tasks.py").exists()
    assert not (remote / "src").exists()


@_needs_posix_shell
def test_stage_swap_merges_into_preserved_live_tree(tmp_path: Path) -> None:
    """Re-push swap: after the pre-clean, ``.hpc/`` is ALWAYS non-empty (the
    protected templates/dispatcher live there) and the staged tree always
    carries ``.hpc/`` — so the swap must MERGE directories. The old
    ``mv -f -t`` swap failed here with ``Directory not empty`` after the
    pre-clean had already deleted ``.hpc/tasks.py`` — a destructive no-op
    re-push."""
    remote = tmp_path / "remote"
    stage = tmp_path / "remote.hpc_stage"
    _first_deploy_remote_tree(remote)
    for rel, content in {
        ".hpc/tasks.py": "new tasks",
        ".hpc/runs/r1.json": "{}",
        ".hpc/runs/r2.json": "{}",
        "src/mod.py": "code v2",
    }.items():
        f = stage / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    clean = _sh(transport._remote_clean_cmd(str(remote), transport._effective_excludes(None)))
    assert clean.returncode == 0, clean.stderr
    # The merge precondition the old mv swap could not handle: the live
    # ``.hpc/`` is still present and non-empty after the clean.
    assert any((remote / ".hpc").iterdir())

    swap = _sh(transport._stage_swap_cmd(str(stage), str(remote)))
    assert swap.returncode == 0, swap.stderr  # mv died 'Directory not empty' here
    # Fresh content merged into the preserved dirs; staging dir consumed.
    assert (remote / ".hpc" / "tasks.py").read_text() == "new tasks"
    assert (remote / ".hpc" / "runs" / "r2.json").is_file()
    assert (remote / "src" / "mod.py").read_text() == "code v2"
    assert (remote / ".hpc" / "templates" / "common" / "hpc_preamble.sh").is_file()
    assert (remote / ".hpc" / "_hpc_dispatch.py").is_file()
    assert (remote / "results" / "out.txt").is_file()
    assert not stage.exists()


def test_remote_clean_cmd_anchors_excludes() -> None:
    """A bare name prunes at any depth (-name); an internal-slash pattern
    is anchored to the sync root (-path) — mirroring rsync's exclude rule."""
    cmd = transport._remote_clean_cmd("/r", [".git/", "*.pyc", ".hpc/_hpc_dispatch.py"])
    # shlex.quote leaves metachar-free tokens bare and quotes only what
    # needs it (the glob), so the remote shell cannot expand ``*.pyc``.
    assert "-name .git" in cmd
    assert "-name '*.pyc'" in cmd
    assert "-path /r/.hpc/_hpc_dispatch.py" in cmd
    assert "-prune -o ! -type d -print0" in cmd
    assert "-prune -o -type d -print0" in cmd


def test_remote_clean_cmd_protects_framework_files() -> None:
    """The deploy_runtime framework files (PROTECTED_RUNTIME_FILES, always
    unioned into the effective exclude set) land in prune clauses, so the
    pre-clean preserves them; a non-excluded stale file is not pruned and
    therefore gets deleted by the files pass. Files are removed with rm -f
    (never rm -rf on a directory — that is what used to delete .hpc/
    wholesale THROUGH the prunes) and stale dirs children-first with a
    non-empty-tolerant rmdir, so a dir holding protected content survives."""
    cmd = transport._remote_clean_cmd("/r", transport._effective_excludes(None))
    assert "-path /r/.hpc/_hpc_dispatch.py" in cmd
    assert "-path /r/.hpc/_hpc_combiner.py" in cmd
    assert "-path /r/.hpc/templates" in cmd  # the preamble's home
    assert "-name hpc_agent" in cmd  # deployed runtime stubs
    assert "! -type d -print0 | xargs -0 -r rm -f --" in cmd
    assert "rm -rf" not in cmd  # no recursive delete can bypass a prune
    assert cmd.endswith("sort -rz | xargs -0 -r rmdir --ignore-fail-on-non-empty --")


def test_remote_clean_cmd_empty_exclude_deletes_whole_subtree() -> None:
    """With no excludes the pre-clean removes everything under remote_path
    (but never remote_path itself — guarded by -mindepth 1)."""
    cmd = transport._remote_clean_cmd("/r", [])
    assert cmd == (
        "find /r -mindepth 1 ! -type d -print0 | xargs -0 -r rm -f -- && "
        "find /r -mindepth 1 -type d -print0 | sort -rz | "
        "xargs -0 -r rmdir --ignore-fail-on-non-empty --"
    )


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
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
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


def test_payload_disclosure_warns_on_bare_exclude_collision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """F-H fires-test: a bare exclude matching two distinct subtrees (the
    run-#10 'data' vs 'src/data' drop) is named before the bytes move."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.parquet").write_text("x")
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "loading.py").write_text("x")
    transport._disclose_payload(tmp_path, ["data"])
    err = capsys.readouterr().err
    assert "bare exclude 'data' matches 2 distinct subtrees" in err
    assert "src/data" in err
    assert "anchor it" in err


def test_payload_disclosure_no_warning_for_single_subtree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.parquet").write_text("x")
    (tmp_path / "code.py").write_text("x")
    transport._disclose_payload(tmp_path, ["data"])
    err = capsys.readouterr().err
    assert "deploy payload" in err
    assert "distinct subtrees" not in err


def test_anchored_exclude_emits_both_tar_dialects(tmp_path: Path) -> None:
    """F-I fires-test: an anchored ./name exclude ships BOTH the GNU (./name)
    and bsdtar (^name) spellings — each dialect ignores the other's form, so
    top-level-only exclusion is exact on both (the run-#10 src/data drop)."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            exclude=["./data", "logs/"],
            delete=False,
        )
    tar_cmd = popen_mock.call_args[0][0]
    assert "--exclude=./data" in tar_cmd
    assert "--exclude=^data" in tar_cmd  # the bsdtar anchor (native Windows)
    assert "--exclude=logs" in tar_cmd  # bare stays match-any-depth, one form
    assert "--exclude=^logs" not in tar_cmd


# --- queue item 6a: cause disclosure for the no-rsync tar full-copy fallback ---


def test_no_rsync_disclosure_warns_full_reship(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """6a fires-test: when rsync is absent the push names the tar fallback's
    NO-DELTA cost (the run-#11 8.4 GB silent full re-ship to CARC) at transfer
    start, in the same ``[transport]`` style as the payload WARN."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""  # EOF: the byte-pump reads to end
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=False
        )
    err = capsys.readouterr().err
    assert "[transport] WARN no rsync on PATH" in err
    assert "NO DELTA" in err
    assert "re-ships even if the remote is identical" in err


def test_rsync_present_emits_no_fallback_disclosure_or_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rsync-present path (real delta) emits NEITHER the no-rsync WARN nor a
    tar-pump progress line — those belong to the fallback only."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
    ):
        transport.rsync_push(ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[])
    err = capsys.readouterr().err
    assert "no rsync on PATH" not in err
    assert "[transport] progress:" not in err


# --- queue item 10: byte-counting progress pump on the tar|ssh pipe -----------


def test_pump_forwards_bytes_exactly_through_subprocess(tmp_path: Path) -> None:
    """The pump is transfer-transparent: a known binary payload fed through
    :func:`transport._pump_with_progress` into a subprocess cat-equivalent
    (reads stdin, writes a file) round-trips byte-for-byte, and the returned
    count matches."""
    import io
    import os

    payload = os.urandom(300_000)  # spans many 4 KiB chunks
    out_file = tmp_path / "out.bin"
    code = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
    r_fd, w_fd = os.pipe()
    proc = subprocess.Popen([sys.executable, "-c", code, str(out_file)], stdin=r_fd)
    os.close(r_fd)  # child holds its own dup; parent drops the read end so EOF fires
    sent = transport._pump_with_progress(
        io.BytesIO(payload), w_fd, total_bytes=len(payload), chunk_size=4096
    )
    proc.wait(timeout=30)
    assert proc.returncode == 0
    assert sent == len(payload)
    assert out_file.read_bytes() == payload  # binary-exact, no truncation


def test_pump_emits_progress_on_forced_short_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With a forced-short interval the pump emits the item-10 heartbeat line for
    each chunk while still forwarding every byte. A concurrent drain reads the
    pipe so the pump's blocking writes never stall."""
    import io
    import os
    import threading

    payload = b"x" * (5 * 4096)
    r_fd, w_fd = os.pipe()
    collected = bytearray()

    def _drain() -> None:
        while True:
            block = os.read(r_fd, 4096)
            if not block:
                break
            collected.extend(block)
        os.close(r_fd)

    drainer = threading.Thread(target=_drain)
    drainer.start()
    sent = transport._pump_with_progress(
        io.BytesIO(payload),
        w_fd,
        total_bytes=len(payload),
        interval_sec=0.0,  # emit after every chunk
        chunk_size=4096,
    )
    drainer.join(timeout=10)
    err = capsys.readouterr().err
    assert sent == len(payload)
    assert bytes(collected) == payload
    assert "[transport] progress:" in err
    assert "MB / ~" in err and "elapsed" in err
    assert err.count("[transport] progress:") >= 2  # multiple chunks -> multiple beats


# --- queue item 6b: content-hash DELTA on rsync-less hosts ---------------------


def _remote_manifest_like(local_root: Path, *, drop: set[str], flip: set[str]):
    """Build a REMOTE :class:`Manifest` from the local tree, then simulate the
    remote diverging: *drop* paths are absent remotely (-> local `missing`),
    *flip* paths get a different sha (-> `mismatched`). Everything else is
    byte-identical (-> never shipped)."""
    from dataclasses import replace

    from hpc_agent.ops.transfer.manifest import Manifest, build_manifest

    entries = []
    for e in build_manifest(local_root).entries:
        if e.path in drop:
            continue
        entries.append(replace(e, sha256="0" * 64) if e.path in flip else e)
    return Manifest(entries=tuple(entries))


def test_delta_tars_exactly_the_changed_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """6b fires-test: with a remote hash manifest available, the rsync-less push
    tars EXACTLY the missing+mismatched files (not the whole tree), extracts them
    ADDITIVELY (no pre-clean / stage-swap), and discloses the delta. If the delta
    were not applied, tar would archive ``.`` and this assertion would fail."""
    (tmp_path / "same.txt").write_text("identical")
    (tmp_path / "changed.txt").write_text("v1")
    (tmp_path / "new.txt").write_text("brand new")
    remote_manifest = _remote_manifest_like(tmp_path, drop={"new.txt"}, flip={"changed.txt"})

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=remote_manifest),
        # Isolate the delta-tar mechanics: the post-ship prune + push-manifest
        # write (ruling 6) are their own legs, covered by tests/infra/
        # test_transport_prune.py — patch them out so run_mock's last call is
        # the tar extract this test pins.
        patch("hpc_agent.infra.transport._prune_manifest_known_extras"),
        patch("hpc_agent.infra.transport._write_push_manifest"),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert result.returncode == 0
    tar_cmd = popen_mock.call_args[0][0]
    # tar c -C <src> changed.txt new.txt  — exactly the sorted changed set, no ".".
    assert tar_cmd[:3] == ["tar", "c", "-C"]
    assert tar_cmd[4:] == ["changed.txt", "new.txt"]
    assert "." not in tar_cmd[4:]
    assert "same.txt" not in tar_cmd  # identical file is never shipped
    # ADDITIVE extract: no pre-clean, no stage-swap (delta never prunes remote).
    remote_cmd = str(run_mock.call_args[0][0][-1])
    assert "tar x -C /r" in remote_cmd
    assert "find /r -mindepth 1" not in remote_cmd
    assert ".hpc_stage" not in remote_cmd
    err = capsys.readouterr().err
    assert "content-hash DELTA" in err
    assert "shipping 2 changed/new" in err
    assert "Additive only" in err


def test_delta_identical_remote_ships_zero_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the remote manifest matches every local file, the push ships nothing —
    no tar, no ssh transfer — and says so. The empty-ship guard must fire."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    remote_manifest = _remote_manifest_like(tmp_path, drop=set(), flip=set())
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=remote_manifest),
        # The post-ship prune + push-manifest write (ruling 6) still ride even a
        # zero-byte ship (a drop with nothing new to send can still leave a
        # manifest-known extra to prune) — patch them out so this test pins the
        # empty-SHIP guard: no tar, no transfer.
        patch("hpc_agent.infra.transport._prune_manifest_known_extras"),
        patch("hpc_agent.infra.transport._write_push_manifest"),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert result.returncode == 0
    popen_mock.assert_not_called()  # no tar spawned
    run_mock.assert_not_called()  # no ssh transfer
    err = capsys.readouterr().err
    assert "already identical for all 2 files" in err
    assert "shipping 0 bytes" in err


def test_manifest_unavailable_falls_back_to_full_tar_with_disclosure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """First deploy / pre-delta runtime: no remote manifest -> the whole tree
    ships via the full-copy tar (archives ``.``), and the 6a disclosure names
    the NO-DELTA cost AND the reason (item 6b). The reason guard must fire."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    tar_cmd = popen_mock.call_args[0][0]
    assert tar_cmd[-1] == "."  # whole tree, not a delta file list
    err = capsys.readouterr().err
    assert "NO DELTA" in err
    assert "remote content-hash manifest unavailable" in err


def test_delta_kill_switch_forces_full_tar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """HPC_NO_DEPLOY_DELTA=1 forces the whole-tree copy even when a remote
    manifest WOULD be available — the manifest is never even fetched — and the
    disclosure names the kill-switch as the reason."""
    monkeypatch.setenv("HPC_NO_DEPLOY_DELTA", "1")
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()),
        patch("hpc_agent.infra.transport._remote_push_manifest") as manifest_mock,
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = MagicMock()
        tar_proc.stdout.read.return_value = b""
        tar_proc.stderr = MagicMock()
        tar_proc.stderr.read.return_value = b""
        tar_proc.returncode = 0
        tar_proc.wait.return_value = 0
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    manifest_mock.assert_not_called()  # kill-switch skips the round-trip entirely
    assert popen_mock.call_args[0][0][-1] == "."  # whole tree
    assert "HPC_NO_DEPLOY_DELTA=1" in capsys.readouterr().err


def test_remote_snippet_agrees_with_local_manifest(tmp_path: Path) -> None:
    """The DEPLOYED-runtime snippet and the LOCAL manifest must describe the same
    tree with the same content-hash atoms, or the delta would ship phantom
    diffs. Execute the real snippet (as the cluster would, cwd = the tree, same
    excludes) under this interpreter and assert byte-for-byte manifest equality
    with :func:`build_manifest`."""
    import json as _json
    import os as _os
    import subprocess as _sp

    from hpc_agent.ops.transfer.manifest import Manifest, build_manifest

    tree = tmp_path / "tree"
    for rel, content in {
        "keep.txt": "a",
        "src/mod.py": "code",
        "data/x.bin": "\x00\x01",
        "results/out.txt": "OUTPUT",  # excluded -> must appear in NEITHER manifest
        "sub/nested/deep.txt": "deep",
    }.items():
        f = tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    exclude = ["results/", ".git/"]
    snippet_file = tmp_path / "snippet.py"
    snippet_file.write_text(transport._REMOTE_MANIFEST_SNIPPET, encoding="utf-8")
    env = {
        **_os.environ,
        "HPC_DELTA_EXCLUDES": _json.dumps([p.rstrip("/") for p in exclude]),
        "HPC_DELTA_CAP": str(transport._DELTA_MANIFEST_FILE_CAP),
    }
    proc = _sp.run(
        [sys.executable, str(snippet_file)],
        cwd=str(tree),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    remote = Manifest.from_dict(_json.loads(proc.stdout))
    local = transport._local_push_manifest(tree, exclude)
    # Same content identity -> zero delta either direction.
    assert remote.digest == local.digest
    assert remote.digest == build_manifest(tree, paths=list(local.paths)).digest
    assert "results/out.txt" not in remote.paths  # excluded on both sides
    from hpc_agent.ops.transfer.manifest import manifest_delta

    assert manifest_delta(local, remote).nothing_to_ship
