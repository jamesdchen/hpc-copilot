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
from hpc_agent.infra.ssh_options import _ssh_binary

if TYPE_CHECKING:
    from pathlib import Path


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_msys_local_translates_drive_colon_on_win32(monkeypatch) -> None:
    # MSYS/cygwin rsync parses a drive colon as a remote host spec, so the
    # local operand must become the /c/... form (#10, #11). Push src shape
    # (trailing backslash), pull dst shape (forward slashes), and a temp-dir
    # deploy-staging shape are all translated.
    monkeypatch.setattr(transport.sys, "platform", "win32")
    assert transport._msys_local("C:\\Users\\me\\exp\\") == "/c/Users/me/exp/"
    assert transport._msys_local("C:/Users/me/out/") == "/c/Users/me/out/"
    assert transport._msys_local("D:\\Temp\\tmpABC/") == "/d/Temp/tmpABC/"


def test_msys_local_noop_off_win32(monkeypatch) -> None:
    # POSIX rsync needs the native path untouched; the translation is win32-only.
    monkeypatch.setattr(transport.sys, "platform", "linux")
    assert transport._msys_local("C:\\Users\\me\\") == "C:\\Users\\me\\"
    assert transport._msys_local("/home/me/exp/") == "/home/me/exp/"


def test_msys_local_noop_for_colonless_path_on_win32(monkeypatch) -> None:
    # A relative / colon-less path has no drive letter to remap.
    monkeypatch.setattr(transport.sys, "platform", "win32")
    assert transport._msys_local("relative/path/") == "relative/path/"


def test_rsync_push_translates_win32_local_src(monkeypatch) -> None:
    # rsync_push must pass its local src through the /c/... translation on
    # win32, or MSYS rsync dies "source and destination cannot both be remote"
    # (#10). _disclose_payload is stubbed so the synthetic C:\ path is never
    # walked; rsync is "present" so the plain-rsync path builds the argv.
    monkeypatch.setattr(transport.sys, "platform", "win32")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport._disclose_payload", return_value=0),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
    ):
        transport.rsync_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path="C:\\Users\\me\\proj",
            exclude=[],
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"
    # argv = [*flags, *exclude_flags, src, dst] → src is second-to-last.
    assert cmd[-2] == "/c/Users/me/proj/"
    assert cmd[-1] == "u@h:/r/"


def test_rsync_pull_translates_win32_local_dst(monkeypatch) -> None:
    # rsync_pull's local dst is the operand MSYS rsync mis-parses as remote
    # host "C" (#11); on win32 it must be the /c/... form. Path.mkdir is
    # stubbed so the synthetic C:\ dir is not materialized on the test host.
    monkeypatch.setattr(transport.sys, "platform", "win32")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value="/usr/bin/rsync"),
        patch("hpc_agent.infra.transport.Path.mkdir"),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
    ):
        transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir="C:\\Users\\me\\exp\\_combiner",
        )
    cmd = run_mock.call_args[0][0]
    assert cmd[0] == "rsync"
    # argv = ["rsync", "-az", *filter_flags, src, dst] → dst is last.
    assert cmd[-1] == "/c/Users/me/exp/_combiner/"


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
    """#173: cluster run-output dirs + stack-minted local pull mirrors are unioned
    into every push's exclude set so a caller's incomplete list can't expose them
    to --delete / the tar pre-clean, nor re-ship them as "code". De-duplicated
    when already present. F10 added ``_aggregated/`` (aggregate-flow output +
    cluster final reduce); run-13 finding 4 added ``_per_task_results/`` /
    ``_per_task_traces/`` (the no-combiner reduce fallbacks' local pull mirrors —
    run 12's 2,700-file mirror rode a code deploy back as 1.18 GB)."""
    assert transport.PROTECTED_OUTPUT_DIRS == [
        "results/",
        "_combiner/",
        "logs/",
        "_aggregated/",
        "_per_task_results/",
        "_per_task_traces/",
        "_dossier/",
    ]
    # Absent from the caller list -> appended.
    eff = transport._effective_excludes(["only_this/"])
    assert "results/" in eff
    assert "_combiner/" in eff
    assert "logs/" in eff  # scheduler log dir — never --delete'd (else it becomes a file)
    assert "_aggregated/" in eff  # F10: aggregate output — never --delete'd / re-pushed
    assert "_per_task_results/" in eff  # finding 4: pull mirror — never re-shipped as code
    assert "_per_task_traces/" in eff
    assert "_dossier/" in eff  # finding 4 sibling: dossier export store — never re-shipped
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
        ".hpc/.deploy_state.json",
        ".hpc/.push_manifest.json",
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
    not). This pins the rsync-ABSENT fallback shape (the mocked run returns an
    empty stdout, so the U4 stage-drop probe finds no ``__HPC_REMOTE_RSYNC__``
    token and the swap tail takes the cp -a path): four bounded ssh legs, in
    order — stage drop (carrying the rsync probe), extract-into-stage, pre-clean
    of the live tree, merge+cleanup."""
    calls = _tar_fallback_run_calls(tmp_path, exclude=[], delete=True)
    assert len(calls) == 4
    drop_cmd = str(calls[0][0][0][-1])
    extract_cmd = str(calls[1][0][0][-1])
    clean_cmd = str(calls[2][0][0][-1])
    move_cmd = str(calls[3][0][0][-1])
    assert "rm -rf /r.hpc_stage" in drop_cmd
    # U4: the rsync probe rides the stage-drop leg (no extra round-trip).
    assert "command -v rsync" in drop_cmd
    assert transport._RSYNC_PROBE_TOKEN in drop_cmd
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
    # The stage-drop leg carried the U4 rsync probe, but the transfer died
    # before ANY swap decision was reached.
    assert any("command -v rsync" in c for c in cmds)
    # No pre-clean of /r, no cp -a merge, no rsync swap — the live tree was never
    # touched under EITHER swap shape.
    assert not any("find /r -mindepth 1" in c for c in cmds)
    assert not any("cp -a /r.hpc_stage/. /r/" in c for c in cmds)
    assert not any("rsync -a --delete" in c for c in cmds)


# ── U4 (2026-07-17): remote-side atomic-per-file swap (primary (a′)) ──────────
#
# The stage-swap torn-FILE window (AUDIT rank-3): the ``cp -a`` merge writes each
# file in place (open/truncate/write), so a concurrent array task could import a
# half-written file. U4 replaces leg-4 with a remote
# ``rsync -a --delete --exclude=<protected>`` swap when the login node has rsync —
# temp+atomic-rename per file closes the window and FOLDS the separate pre-clean
# into rsync's ``--delete`` (one fewer ssh leg). A ``cp -a`` fallback stays for
# rsync-absent login nodes. The atomic path is selected by a zero-cost probe that
# rides the stage-drop leg.


def _tar_fallback_run_calls_seq(tmp_path: Path, *, exclude: list[str], side_effect):
    """Run rsync_push in tar-fallback delete=True mode with a per-call
    ``run_capture_bounded`` side-effect list, returning the (probe-filtered)
    call list. *side_effect* controls each leg's CompletedProcess — crucially
    the stage-drop leg's ``stdout``, which carries the U4 rsync probe token."""
    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=side_effect) as run_mock,
        # Force the full-copy path (no remote hash manifest) so the leg sequence
        # is the stage/extract/swap shape, not the delta batch loop.
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
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
            delete=True,
        )
    return [c for c in run_mock.call_args_list if not _is_ssh_version_probe(c)]


def test_stage_swap_rsync_cmd_is_atomic_per_file_delete() -> None:
    """The U4 primary swap builder: ``rsync -a --delete`` from the staged tree
    into the live root, with the protected set shielded via ``--exclude`` and
    anchored exactly like the ``find`` pre-clean — internal-slash patterns
    root-anchored, bare names match-any-depth. No cp -a, no separate find."""
    cmd = transport._stage_swap_rsync_cmd("/r.hpc_stage", "/r", transport._effective_excludes(None))
    assert cmd.startswith("rsync -a --delete ")
    assert " /r.hpc_stage/ /r/ " in cmd  # trailing-slash source => copy CONTENTS
    assert cmd.endswith("&& rm -rf /r.hpc_stage")
    # Protected content shielded from --delete (the merge contract).
    assert "--exclude=results/" in cmd  # bare name — match any depth
    assert "--exclude=_combiner/" in cmd
    assert "--exclude=logs/" in cmd
    assert "--exclude=hpc_agent/" in cmd
    assert "--exclude=clusters.yaml" in cmd
    # Internal-slash patterns are ROOT-ANCHORED with a leading / (mirroring the
    # pre-clean's `find -path <root>/<pattern>`).
    assert "--exclude=/.hpc/templates/" in cmd
    assert "--exclude=/.hpc/_hpc_dispatch.py" in cmd
    # The torn-file window is gone: no in-place merge, no find pre-clean leg.
    assert "cp -a" not in cmd
    assert "find " not in cmd


def test_rsync_push_fallback_delete_true_atomic_swap_when_remote_has_rsync(
    tmp_path: Path,
) -> None:
    """When the stage-drop probe finds a login-node rsync (its token rides the
    leg's stdout), the swap tail takes the PRIMARY atomic-per-file path: THREE
    legs — stage drop, extract-into-stage, one rsync ``--delete`` swap that folds
    in the pre-clean. No separate find pre-clean, no cp -a."""
    token = transport._RSYNC_PROBE_TOKEN
    side_effect = [
        _ok(stdout=f"{token}\n"),  # leg 1: stage drop — probe finds rsync
        _ok(),  # leg 2: extract into stage
        _ok(),  # leg 3: the atomic rsync swap
    ]
    calls = _tar_fallback_run_calls_seq(tmp_path, exclude=[], side_effect=side_effect)
    assert len(calls) == 3  # one fewer leg than the cp -a fallback's four
    drop_cmd = str(calls[0][0][0][-1])
    extract_cmd = str(calls[1][0][0][-1])
    swap_cmd = str(calls[2][0][0][-1])
    assert "command -v rsync" in drop_cmd
    assert "tar x -C /r.hpc_stage" in extract_cmd
    # The swap is the single atomic rsync leg; the pre-clean is FOLDED in.
    assert "rsync -a --delete" in swap_cmd
    assert "/r.hpc_stage/ /r/" in swap_cmd
    assert "rm -rf /r.hpc_stage" in swap_cmd
    assert "--exclude=results/" in swap_cmd
    assert "--exclude=/.hpc/templates/" in swap_cmd
    # No cp -a merge and no standalone find pre-clean anywhere in the sequence.
    assert not any("cp -a /r.hpc_stage/. /r/" in str(c[0][0][-1]) for c in calls)
    assert not any("find /r -mindepth 1" in str(c[0][0][-1]) for c in calls)
    # The swap carries its own bounded (short) timeout, like every stage leg.
    assert calls[2][1]["timeout_sec"] == transport.PRECLEAN_TIMEOUT_SEC


def test_rsync_push_fallback_delete_true_cp_a_when_remote_lacks_rsync(
    tmp_path: Path,
) -> None:
    """When the probe finds NO login-node rsync (empty stdout on the stage-drop
    leg), the swap tail falls back to today's behavior: FOUR legs — stage drop,
    extract, find pre-clean, cp -a merge. This is the rsync-absent fallback the
    seam map requires U4 to preserve unchanged."""
    side_effect = [_ok(), _ok(), _ok(), _ok()]  # no probe token on leg 1
    calls = _tar_fallback_run_calls_seq(tmp_path, exclude=[], side_effect=side_effect)
    assert len(calls) == 4
    clean_cmd = str(calls[2][0][0][-1])
    move_cmd = str(calls[3][0][0][-1])
    assert "find /r -mindepth 1" in clean_cmd  # pre-clean stays a distinct leg
    assert "cp -a /r.hpc_stage/. /r/" in move_cmd  # unchanged fallback merge
    assert not any("rsync -a --delete" in str(c[0][0][-1]) for c in calls)


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


_needs_rsync = pytest.mark.skipif(
    shutil.which("rsync") is None, reason="exercises a real rsync swap on disk"
)


@_needs_posix_shell
@_needs_rsync
def test_stage_swap_rsync_merges_and_deletes_on_disk(tmp_path: Path) -> None:
    """Behavioral proof of the U4 merge contract under the REAL rsync swap: fresh
    code merges into the preserved live tree, protected content survives the
    ``--delete``, and stale unprotected code is removed — the same contract the
    cp -a swap + pre-clean gave, now atomic per file (temp+rename)."""
    remote = tmp_path / "remote"
    stage = tmp_path / "remote.hpc_stage"
    _first_deploy_remote_tree(remote)
    for rel, content in {".hpc/tasks.py": "new tasks", "src/mod.py": "code v2"}.items():
        f = stage / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    cmd = transport._stage_swap_rsync_cmd(
        str(stage), str(remote), transport._effective_excludes(None)
    )
    res = _sh(cmd)
    assert res.returncode == 0, res.stderr
    # Fresh code merged into the preserved dirs.
    assert (remote / ".hpc" / "tasks.py").read_text() == "new tasks"
    assert (remote / "src" / "mod.py").read_text() == "code v2"
    # Protected content preserved (never --delete'd).
    assert (remote / ".hpc" / "templates" / "common" / "hpc_preamble.sh").is_file()
    assert (remote / ".hpc" / "_hpc_dispatch.py").is_file()
    assert (remote / "results" / "out.txt").is_file()
    assert (remote / "logs" / "job.o1.1").is_file()
    assert (remote / "hpc_agent" / "execution" / "mapreduce" / "metrics_io.py").is_file()
    # Stale unprotected code deleted by --delete (was live, not staged).
    assert not (remote / "src" / "old_pkg" / "gone.py").exists()
    # Staging dir consumed by the trailing rm -rf.
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


def test_delete_push_preserves_deploy_bookkeeping_files() -> None:
    """A delete=True push must NOT wipe the deploy_runtime-placed bookkeeping
    files (#66): the deploy-cache manifest (.hpc/.deploy_state.json) and the
    push manifest (.hpc/.push_manifest.json) never live in the local push tree,
    so without protection every standard push-then-deploy cycle deletes them —
    the #242 content-hash deploy cache would always miss. Both must ride the
    always-protected exclude set and land in the tar-fallback pre-clean prunes."""
    eff = transport._effective_excludes(None)
    assert ".hpc/.deploy_state.json" in eff
    assert ".hpc/.push_manifest.json" in eff
    # A caller-supplied exclude that names neither still carries both.
    eff_custom = transport._effective_excludes(["only_this/"])
    assert ".hpc/.deploy_state.json" in eff_custom
    assert ".hpc/.push_manifest.json" in eff_custom
    # And the tar-fallback --delete pre-clean anchors them as prunes, so the
    # destructive pass preserves them.
    cmd = transport._remote_clean_cmd("/r", eff)
    assert "-path /r/.hpc/.deploy_state.json" in cmd
    assert "-path /r/.hpc/.push_manifest.json" in cmd


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


def test_rsync_pull_falls_back_to_tar_ssh_pull_when_rsync_missing(tmp_path: Path) -> None:
    """The rsync-less pull now routes to the content-hash PULL engine
    (``tar_ssh_pull``) instead of the old monolithic ``scp -r`` (latency ranks
    2 + 7): it joins ``remote_subdir`` onto ``remote_path``, passes ``include``
    through as ``include_globs``, and adapts the :class:`PullResult` back to the
    ``CompletedProcess`` contract callers read. Full engine coverage lives in
    tests/infra/test_transport_pull.py."""
    captured: dict[str, object] = {}

    def fake_engine(*, ssh_target, remote_path, local_path, include_globs, timeout):
        captured.update(remote_path=remote_path, include_globs=include_globs)
        return transport.PullResult(
            ok=True, files_pulled=2, bytes_pulled=42, skipped_unchanged=0, stderr_tail=""
        )

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.tar_ssh_pull", side_effect=fake_engine),
    ):
        proc = transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
            include=["wave_*.json"],
        )
    assert proc.returncode == 0
    assert captured["remote_path"] == "/r/_combiner"  # subdir joined onto the root
    assert captured["include_globs"] == ["wave_*.json"]  # server-side include filter
    assert (tmp_path / "out").exists()


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


def test_tar_push_ssh_exits_early_does_not_deadlock(tmp_path: Path) -> None:
    """#9 regression: when ssh exits (rc!=0) BEFORE draining tar's stream and
    tar's output exceeds the OS pipe buffer, the parent must close its pump
    read-end and unwind promptly — never block forever in an unbounded
    pump_thread.join() past every transport deadline. Here ssh (mocked)
    returns rc=1 without reading pump_r while a >1 MB tar stdout is still
    pumping; the pre-fix code deadlocked in join() (finally's pump_r close
    ran only AFTER the join)."""
    import io
    import os
    import threading

    payload = os.urandom(1_000_000)  # far exceeds the ~64 KB pipe buffer

    def _ssh_rc1(argv, *_a, **_kw):
        # ssh exits non-zero WITHOUT reading its stdin (pump_r), leaving the
        # pump blocked on a full pipe — the deadlock trigger.
        return subprocess.CompletedProcess(
            args=list(argv), returncode=1, stdout="", stderr="remote tar x failed"
        )

    with (
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=_ssh_rc1),
        # Absorb the lazy `ssh -V` probe: the Popen patch is GLOBAL.
        patch("hpc_agent.infra.transport.subprocess.run", return_value=_ok()),
        patch("hpc_agent.infra.transport.subprocess.Popen") as popen_mock,
    ):
        tar_proc = popen_mock.return_value
        tar_proc.stdout = io.BytesIO(payload)
        tar_proc.returncode = 0

        result_box: dict[str, subprocess.CompletedProcess[str]] = {}

        def _call() -> None:
            result_box["r"] = transport._tar_ssh_push(
                ssh_target="u@h",
                remote_path="/r",
                local_path=tmp_path,
                exclude=[],
                delete=False,
                timeout=10,
                total_bytes=len(payload),
            )

        worker = threading.Thread(target=_call)
        worker.start()
        worker.join(timeout=30)
        assert not worker.is_alive(), "_tar_ssh_push deadlocked (unbounded pump join)"

    assert "r" in result_box
    # The truncated transfer surfaces as a non-zero result, not a hang.
    assert result_box["r"].returncode != 0


# --- queue item 6b: content-hash DELTA on rsync-less hosts ---------------------


def _remote_manifest_like(local_root: Path, *, drop: set[str], flip: set[str]):
    """Build a REMOTE :class:`Manifest` from the local tree, then simulate the
    remote diverging: *drop* paths are absent remotely (-> local `missing`),
    *flip* paths get a different sha (-> `mismatched`). Everything else is
    byte-identical (-> never shipped)."""
    from dataclasses import replace

    from hpc_agent.infra.manifest import Manifest, build_manifest

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

    # The delta rides a -T names FILE (run-#12 finding 17: per-path argv
    # overflows Windows' ~32k limit) that is unlinked after the push — capture
    # its content at Popen time, the only moment it exists.
    names_seen: list[str] = []

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

        def _capture_names(cmd, **_kwargs):
            if "-T" in cmd:
                # open(), not Path: the module's Path import is TYPE_CHECKING-only.
                with open(cmd[cmd.index("-T") + 1], encoding="utf-8") as fh:
                    names_seen.append(fh.read())
            return popen_mock.return_value

        popen_mock.side_effect = _capture_names
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
    # tar c -C <src> -T <names-file>  — exactly the sorted changed set, no ".".
    assert tar_cmd[:3] == ["tar", "c", "-C"]
    assert tar_cmd[4] == "-T"
    assert names_seen == ["./changed.txt\n./new.txt\n"]
    assert "." not in tar_cmd[5:]
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


def test_path_excluded_anchors_internal_slash_patterns() -> None:
    """#F57: an internal-slash exclude is ROOT-ANCHORED — it drops that path and
    everything under it, but not a same-named component elsewhere in the tree.
    Before the fix such patterns were inert here (silently shipped in delta
    mode while the rsync and full-copy tar modes honored them)."""
    pats = [p.rstrip("/") for p in ["data/interim/"]]
    # The anchored subtree and its descendants are excluded ...
    assert transport._path_excluded(("data", "interim"), pats)
    assert transport._path_excluded(("data", "interim", "big.bin"), pats)
    assert transport._path_excluded(("data", "interim", "sub", "deep.bin"), pats)
    # ... but a sibling, a same-named subtree NOT at the root, and a
    # prefix-collision are all kept (root-anchoring + component boundary).
    assert not transport._path_excluded(("data", "final", "keep.bin"), pats)
    assert not transport._path_excluded(("src", "data", "interim"), pats)
    assert not transport._path_excluded(("data", "interim_v2", "x"), pats)
    # A bare name still matches at ANY depth (unchanged).
    assert transport._path_excluded(("src", "data", "cache"), ["cache"])


def test_delta_manifest_honors_internal_slash_exclude(tmp_path: Path) -> None:
    """#F57 fire-path: the rsync-less DELTA push must drop an anchored
    internal-slash pattern from the local manifest — exactly as the full-copy
    tar mode (``--exclude=data/interim``) does — so the SAME push command ships
    the SAME file set whether or not the remote-manifest round-trip succeeded.
    Before the fix ``data/interim/**`` shipped ONLY in delta mode."""
    (tmp_path / "keep.py").write_text("code")
    interim = tmp_path / "data" / "interim"
    interim.mkdir(parents=True)
    (interim / "big.bin").write_text("huge intermediate")
    (interim / "sub").mkdir()
    (interim / "sub" / "deep.bin").write_text("deeper")
    (tmp_path / "data" / "final.txt").write_text("keep this")

    rels = transport._pushable_relpaths(tmp_path, [p.rstrip("/") for p in ["data/interim/"]])
    assert "keep.py" in rels
    assert "data/final.txt" in rels  # sibling of the excluded subtree survives
    assert not any(r.startswith("data/interim/") for r in rels)


def test_remote_snippet_honors_internal_slash_exclude(tmp_path: Path) -> None:
    """#F57 lockstep: the DEPLOYED-runtime snippet must anchor an internal-slash
    exclude the SAME way the local manifest now does, or the delta would ship a
    phantom diff for the excluded subtree. Execute the real snippet and compare
    file sets against the local manifest."""
    import json as _json
    import os as _os
    import subprocess as _sp

    from hpc_agent.infra.manifest import Manifest

    tree = tmp_path / "tree"
    for rel, content in {
        "keep.py": "code",
        "data/interim/big.bin": "intermediate",
        "data/interim/sub/deep.bin": "deeper",
        "data/final.txt": "keep",
    }.items():
        f = tree / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)

    exclude = ["data/interim/"]
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
    # Same file set on both sides -> zero phantom delta for the excluded subtree.
    assert set(remote.paths) == set(local.paths)
    assert not any(p.startswith("data/interim/") for p in remote.paths)
    assert "data/final.txt" in remote.paths


def test_remote_snippet_agrees_with_local_manifest(tmp_path: Path) -> None:
    """The DEPLOYED-runtime snippet and the LOCAL manifest must describe the same
    tree with the same content-hash atoms, or the delta would ship phantom
    diffs. Execute the real snippet (as the cluster would, cwd = the tree, same
    excludes) under this interpreter and assert byte-for-byte manifest equality
    with :func:`build_manifest`."""
    import json as _json
    import os as _os
    import subprocess as _sp

    from hpc_agent.infra.manifest import Manifest, build_manifest

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
    from hpc_agent.infra.manifest import manifest_delta

    assert manifest_delta(local, remote).nothing_to_ship


# ─── child-failure disclosure (run-#13 finding 2) ──────────────────────────
# A VPN-severed scp/ssh child dies non-zero with its "lost connection" story in
# stderr; the transport runner must flush that story to the worker log at death,
# not let it die on the tail-able surface with a stale progress line.


def _fail(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_tar_ssh_pull_disclosure_flows_through_rsync_pull(tmp_path: Path) -> None:
    """A failed rsync-less pull surfaces the engine's stderr tail through the
    ``CompletedProcess`` the reroute adapts (the child-stderr disclosure itself
    is the engine's concern — see tests/infra/test_transport_pull.py — this pins
    the failure mapping at the ``rsync_pull`` boundary)."""

    def fake_engine(**kwargs):
        return transport.PullResult(
            ok=False,
            files_pulled=0,
            bytes_pulled=0,
            skipped_unchanged=0,
            stderr_tail="ssh: Connection reset by peer\nlost connection",
        )

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.tar_ssh_pull", side_effect=fake_engine),
    ):
        proc = transport.rsync_pull(
            ssh_target="u@h",
            remote_path="/r",
            remote_subdir="_combiner",
            local_dir=tmp_path / "out",
        )
    assert proc.returncode == 1
    assert "lost connection" in proc.stderr  # the story is carried to the caller


def test_disclose_child_failure_bounds_the_tail() -> None:
    import contextlib as _ctx
    import io as _io

    from hpc_agent.infra.transport import disclose_child_failure
    from hpc_agent.infra.transport._disclose import _CHILD_STDERR_TAIL_CHARS

    buf = _io.StringIO()
    with _ctx.redirect_stderr(buf):
        disclose_child_failure(what="tar|ssh push", returncode=255, stderr="x" * 20_000)
    out = buf.getvalue()
    assert "child tar|ssh push exited 255" in out
    assert "(truncated)" in out
    assert len(out) < _CHILD_STDERR_TAIL_CHARS + 500


def test_disclose_child_failure_names_empty_stderr() -> None:
    import contextlib as _ctx
    import io as _io

    from hpc_agent.infra.transport import disclose_child_failure

    buf = _io.StringIO()
    with _ctx.redirect_stderr(buf):
        disclose_child_failure(what="scp pull", returncode=1, stderr="")
    assert "(no stderr captured)" in buf.getvalue()


# ─── F7 verify-during-build (unit 2.4b): the transfer plane bypasses the ssh
# engine and is preamble-free ──────────────────────────────────────────────────
#
# The verify memo (transport/__init__.py, "F7 verify-during-build memo") aborted
# the transfer-plane-routing leg: every transfer op already reaches the cluster
# through the ONE-SHOT ``run_capture_bounded`` bounded runner (never ``ssh_run``,
# so never the asyncssh engine) with a raw, preamble-free remote command line.
# These pins LOCK that verdict so a future edit that re-routed a transfer through
# ``ssh_run`` (re-arming the engine wrapper) or bolted a ``module load`` /
# ``conda activate`` preamble onto a transfer command would turn RED.

# Tokens that would appear iff a transfer command acquired the control-plane
# activation preamble (``remote_activation_for_sidecar``) or the ``ssh_run``
# self-destruct wrapper (``remote.build_remote_command``). A transfer command line
# is byte-equal to the raw shell it runs and carries NONE of these (E1).
_FORBIDDEN_PREAMBLE_TOKENS = (
    "module load",  # Lmod ceremony (control-plane preamble)
    "module purge",
    "conda activate",  # conda ceremony (control-plane preamble)
    "source ",  # `source .../conda.sh` (control-plane preamble)
    "HPC_AGENT_OP=",  # the LAYER-2 marker (ssh_run wrapper only)
    "timeout -k",  # the LAYER-1 self-destruct deadline (ssh_run wrapper only)
)


def _warm_delta_push_ssh_cmds(tmp_path: Path, *, n_extra: int = 0) -> list[str]:
    """Run a WARM rsync-less delta re-push against a real 3-file tree and return
    the remote command string handed to ssh for every ``run_capture_bounded``
    open, in call order.

    The remote is content-identical except ``changed.txt`` (1 file ships). A smart
    ``run_capture_bounded`` mock answers each open by inspecting the remote command
    so the REAL ``_remote_push_manifest`` / ``_read_prior_push_manifest`` /
    ``_write_push_manifest`` / prune legs all execute and are counted — no leg is
    patched out, so the dial count is the true one. *n_extra* seeds a
    manifest-known remote extra so the prune ``rm`` leg fires.
    """
    from dataclasses import replace

    from hpc_agent.infra.manifest import FileEntry, build_manifest

    (tmp_path / "same.txt").write_text("identical")
    (tmp_path / "changed.txt").write_text("v1")
    (tmp_path / "also_same.txt").write_text("stable")

    entries = [
        replace(e, sha256="0" * 64) if e.path == "changed.txt" else e
        for e in build_manifest(tmp_path).entries
    ]
    if n_extra:
        entries.append(FileEntry(path="gone_extra.txt", size=3, sha256="1" * 64))
    remote_files = {
        "files": [{"path": e.path, "size": e.size, "sha256": e.sha256} for e in entries],
        "hashed": 0,
        "cached": len(entries),
    }
    import json as _json

    prior_manifest = _json.dumps({"paths": ["gone_extra.txt"], "manifest_schema": 2})

    cmds: list[str] = []

    def _smart(cmd, *_a, **_kw):
        remote = str(cmd[-1]) if isinstance(cmd, list) else str(cmd)
        cmds.append(remote)
        if "HPC_DELTA_EXCLUDES" in remote:  # the remote hash-manifest snippet
            return _ok(stdout=_json.dumps(remote_files))
        if "cat " in remote and "push_manifest" in remote:  # read prior manifest
            return _ok(stdout=prior_manifest if n_extra else "")
        return _ok()

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=_smart),
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
    # Filter the lazy ``ssh -V`` version probe — it never rides run_capture_bounded
    # (it is a bare subprocess.run in ssh_options), so nothing to filter here, but
    # guard defensively against a "-V" leaking into the list.
    return [c for c in cmds if c != "-V"]


def test_warm_delta_push_stays_within_four_opens(tmp_path: Path) -> None:
    """F2 push-fold acceptance (unit 2.4b): a WARM rsync-less re-push (small delta,
    nothing to prune) costs <= 4 cold ssh opens — remote hash manifest, the single
    delta batch, and the final push-manifest seal (3). The many-batch cold case is
    inherently more (one durable checkpoint per landed batch, run-13 finding 3 — a
    correctness feature, not a fold-away regression); this pin fixes the WARM floor
    so a future edit that re-added a per-op cold round-trip to the warm path turns
    RED."""
    cmds = _warm_delta_push_ssh_cmds(tmp_path)
    assert len(cmds) <= 4, f"warm push opened {len(cmds)} ssh connections: {cmds}"
    # The exact warm shape: manifest read, one delta batch, final seal.
    assert any("HPC_DELTA_EXCLUDES" in c for c in cmds)  # remote hash manifest
    assert any("tar x -C /r" in c for c in cmds)  # the single delta batch
    assert any("push_manifest" in c.lower() or "HPC_PM_PAYLOAD" in c for c in cmds)  # final seal


def test_transfer_plane_remote_commands_are_preamble_free(tmp_path: Path) -> None:
    """E1 byte-equality: every transfer-plane remote command line — the delta path
    (remote hash manifest, tar extract, push-manifest write) AND the with-prune
    path (prior-manifest read, prune ``rm``) — is byte-equal to the raw shell it
    runs: no ``module load`` / ``conda activate`` / ``source`` control-plane
    preamble and no ``HPC_AGENT_OP=``/``timeout -k`` ssh_run self-destruct wrapper.
    The transfer plane never routes through ``remote_activation_for_sidecar`` or
    ``build_remote_command``."""
    cmds = _warm_delta_push_ssh_cmds(tmp_path, n_extra=1)
    # The with-prune path exercised every transfer leg: manifest, batch, prior-read,
    # prune rm, seal.
    assert any("rm -f -- gone_extra.txt" in c for c in cmds), f"prune leg absent: {cmds}"
    for cmd in cmds:
        for token in _FORBIDDEN_PREAMBLE_TOKENS:
            assert token not in cmd, f"transfer command acquired {token!r}: {cmd!r}"


def test_full_copy_tar_extract_is_preamble_free(tmp_path: Path) -> None:
    """The manifest-less full-copy fallback's remote extract is preamble-free too —
    a first deploy (no remote manifest) must not carry activation/wrapper text."""
    remote_cmd = _tar_fallback_remote_cmd(tmp_path, exclude=[], delete=False)
    for token in _FORBIDDEN_PREAMBLE_TOKENS:
        assert token not in remote_cmd, f"full-copy extract acquired {token!r}: {remote_cmd!r}"
    assert "tar x -C /r" in remote_cmd  # the raw extract, nothing wrapped around it


def test_transfer_plane_push_never_consults_ssh_engine(tmp_path: Path) -> None:
    """Row 9 (engine-seam laws extend): even with the asyncssh engine ENABLED, an
    rsync-less push drives ``run_capture_bounded`` and never the engine — the
    transfer plane IS the one-shot leg, so the 2026-07-16 engine-default flip
    leaves its dial counts byte-identical. A regression that routed a transfer
    through ``ssh_run`` (which owns the engine gate) would trip this."""
    from hpc_agent.infra import ssh_engine

    (tmp_path / "f.txt").write_text("hi")
    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=_ok()) as run_mock,
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=None),
        patch.object(ssh_engine, "engine_enabled", return_value=True),
        patch.object(ssh_engine, "engine_ssh_run") as engine_mock,
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
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=False
        )
    engine_mock.assert_not_called()  # the engine gate was never reached
    assert run_mock.called  # the one-shot bounded runner carried the transfer
