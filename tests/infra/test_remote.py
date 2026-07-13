"""Tests for hpc_agent.infra.remote + hpc_agent.infra.transport.

Mocks subprocess.run via unittest.mock.patch.  Covers argv composition
(rsync flags, include/exclude order, trailing slashes) and the
run_combiner / run_combiner_checked return-shape contract.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.errors import SshCircuitOpen
from hpc_agent.infra import remote, transport
from hpc_agent.infra.ssh_options import _ssh_binary


def _inner_remote_cmd(wrapped: str) -> str:
    """Recover the pre-wrap remote command from a ``build_remote_command`` string.

    Every remote command ``ssh_run`` ships is now wrapped in
    ``HPC_AGENT_OP=... timeout ... bash -c '<inner>' HPC_AGENT_OP=...`` (run-12
    finding 20). Tests that assert on the ORIGINAL command shell-quoting look
    inside the ``bash -c`` payload — the round-trip is exact by construction
    (``shlex.quote`` in the builder, ``shlex.split`` here).
    """
    toks = shlex.split(wrapped)
    return toks[toks.index("-c") + 1]


@pytest.fixture(autouse=True)
def _force_rsync_present():
    """Pin _have_rsync to True so existing tests exercise the rsync branch.

    Tests for the scp/tar fallback live in test_remote_rsync_fallback.py
    and explicitly patch ``shutil.which`` themselves; this fixture only
    affects the rsync-branch tests in this file.
    """
    # After PR-3 the transport helpers (rsync_push / rsync_pull) live in
    # ``hpc_agent.infra.transport`` and look up ``_have_rsync`` against
    # that module. Patching the re-exported alias on ``remote`` alone
    # would no longer reach the live call site, so we patch the source
    # of truth in ``transport`` (the alias on ``remote`` is patched too
    # so any future callers that reach in via the legacy attribute path
    # also see the True).
    with patch("hpc_agent.infra.transport._have_rsync", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _disable_ssh_backoff(monkeypatch):
    """Skip backoff retries + sleep entirely so single-call argv tests stay fast.

    The backoff helper's retry loop would otherwise repeat each subprocess
    mock 5 times for genuine throttle markers, breaking ``call_args``
    assertions. Tests that *want* to exercise the backoff path live in
    :class:`TestSshBackoff` below and clear this env var locally.
    """
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")


def _cp(stdout="", stderr="", returncode=0):
    """Mimic subprocess.CompletedProcess enough for the remote module."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# rsync_push
# ---------------------------------------------------------------------------


class TestRsyncPush:
    def test_flag_composition_with_defaults(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="alice@cluster.example",
                remote_path="/u/home/alice/proj",
                local_path="/tmp/local_src",
            )

        argv = mock_run.call_args[0][0]
        assert argv[0] == "rsync"
        assert "-az" in argv
        # --delete is on by default
        assert "--delete" in argv
        # excludes from DEFAULT_RSYNC_EXCLUDES, preserving order, with the
        # mandatory credential-protecting excludes (clusters.yaml) and the
        # protected output dirs (results/, _combiner/ — #173) appended.
        exclude_patterns = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--exclude"]
        assert exclude_patterns == (
            transport.DEFAULT_RSYNC_EXCLUDES
            + transport.MANDATORY_RSYNC_EXCLUDES
            + transport.PROTECTED_OUTPUT_DIRS
            + transport.PROTECTED_RUNTIME_FILES
        )
        # Source has trailing slash
        src = argv[-2]
        assert src.endswith("/")
        assert src.rstrip("/") == "/tmp/local_src"
        # Destination has trailing slash, user@host:path/
        dst = argv[-1]
        assert dst == "alice@cluster.example:/u/home/alice/proj/"

    def test_delete_toggle_off(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                delete=False,
            )
        argv = mock_run.call_args[0][0]
        assert "--delete" not in argv

    def test_custom_excludes_passed_in_order(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                exclude=["a/", "b/", "c/"],
            )
        argv = mock_run.call_args[0][0]
        patterns = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--exclude"]
        # Caller excludes preserved in order; mandatory credential excludes
        # (clusters.yaml), protected output dirs (results/, _combiner/ — #173),
        # and the deploy_runtime framework files (PROTECTED_RUNTIME_FILES) are
        # always unioned in and cannot be dropped by a custom exclude.
        assert patterns == (
            ["a/", "b/", "c/"]
            + transport.MANDATORY_RSYNC_EXCLUDES
            + transport.PROTECTED_OUTPUT_DIRS
            + transport.PROTECTED_RUNTIME_FILES
        )


# ---------------------------------------------------------------------------
# rsync_pull
# ---------------------------------------------------------------------------


class TestRsyncPull:
    def test_with_include_list_filters_in_correct_order(self, tmp_path):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
                include=["*.json", "*.csv"],
            )

        argv = mock_run.call_args[0][0]
        # The filter flags should appear in this exact order:
        #   --include=*/  (prepended, so subdirs are traversed)
        #   --include=<user>  (each user pattern)
        #   --exclude=*   (appended last)
        include_all_dirs_idx = argv.index("--include=*/")
        exclude_all_idx = argv.index("--exclude=*")
        user_indices = [argv.index(f"--include={p}") for p in ("*.json", "*.csv")]

        # All the user includes sit between the directory include and the final exclude.
        assert include_all_dirs_idx < min(user_indices)
        assert max(user_indices) < exclude_all_idx
        # And user patterns preserve input order.
        assert user_indices == sorted(user_indices)

    def test_without_include_no_filter_flags(self, tmp_path):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
            )
        argv = mock_run.call_args[0][0]
        # No --include / --exclude flags when include is None.
        assert not any(a.startswith("--include") for a in argv)
        assert not any(a.startswith("--exclude") for a in argv)


# ---------------------------------------------------------------------------
# deploy_runtime
# ---------------------------------------------------------------------------


class TestDeployRuntime:
    """Verify call order: 1 ssh (mkdir prelude), then 1 batched transfer (#252).

    Since #252 the surviving files ship in ONE rsync delta (or tar fallback)
    rather than per-file scp; we patch the ``_deploy_transfer`` seam to assert
    the full SET of dst_rels handed to that single transfer.
    """

    def test_ssh_mkdir_then_transfers_all_files(self):
        # The mkdir runs through ssh_run (capture=True) → the close-pipes
        # capture seam (#209); the files then ship in one batched transfer.
        # The capture seam returns empty stdout, so the deploy cache (#242)
        # sees no remote manifest → a full deploy.
        captured: dict[str, object] = {}

        def _capture(*, ssh_target, remote_path, items):
            captured["ssh_target"] = ssh_target
            captured["remote_path"] = remote_path
            captured["dst_rels"] = [it.dst_rel for it in items]

        with (
            patch("hpc_agent.infra.remote._capture_via_select") as mock_ssh,
            patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture),
        ):
            mock_ssh.return_value = _cp()
            transport.deploy_runtime(ssh_target="u@c", remote_path="/p")

        # ssh mkdir: exactly one capture-seam call, and it is the mkdir. The
        # command resolves through _ssh_binary() (bare "ssh" on Linux/macOS;
        # the native OpenSSH abs path on Windows, per #145), so assert against
        # the resolver rather than a hardcoded name to stay platform-agnostic.
        assert mock_ssh.call_count == 1
        mkdir_argv = mock_ssh.call_args[0][0]
        assert mkdir_argv[0] == _ssh_binary()
        assert "mkdir -p" in mkdir_argv[-1]
        assert ".hpc/templates" in mkdir_argv[-1]
        assert ".hpc/templates/common" in mkdir_argv[-1]
        # The deploy cache folds its manifest read into the same prelude ssh
        # (no extra round-trip): a trailing ``cat`` of the manifest path.
        assert ".hpc/.deploy_state.json" in mkdir_argv[-1]
        # Deployed hpc_agent/ must be a PEP 420 namespace package so it never
        # shadows a pip-installed hpc_agent on the cluster: no __init__.py is
        # created, and stale ones from old deploys are removed.
        assert "touch" not in mkdir_argv[-1]
        assert "rm -f" in mkdir_argv[-1]
        assert "/p/hpc_agent/__init__.py" in mkdir_argv[-1]

        # One batched transfer to the project root carrying every file plus the
        # cache-manifest write. No scheduler arg → sge + slurm cpu/gpu/mpi
        # templates. Twelve base files + the 7-module status-reporter eager
        # closure (#349) + manifest = 20 dst_rels.
        assert captured["ssh_target"] == "u@c"
        assert captured["remote_path"] == "/p"
        rels = set(captured["dst_rels"])
        # Importable stubs into hpc_agent/ (so cluster-side user imports resolve).
        assert "hpc_agent/execution/mapreduce/metrics_io.py" in rels
        assert "hpc_agent/executor_cli.py" in rels
        # Framework executor + combiner into .hpc/
        assert ".hpc/_hpc_dispatch.py" in rels
        assert ".hpc/_hpc_combiner.py" in rels
        # Six templates into .hpc/templates/ (cpu/gpu/mpi × sge/slurm).
        assert ".hpc/templates/cpu_array.sh" in rels
        assert ".hpc/templates/gpu_array.sh" in rels
        assert ".hpc/templates/mpi.sh" in rels
        assert ".hpc/templates/cpu_array.slurm" in rels
        assert ".hpc/templates/gpu_array.slurm" in rels
        assert ".hpc/templates/mpi.slurm" in rels
        # Two shared preambles into .hpc/templates/common/
        assert ".hpc/templates/common/hpc_preamble.sh" in rels
        assert ".hpc/templates/common/gpu_preamble.sh" in rels
        # Status-reporter eager (import-time) closure into the namespace tree
        # (#349) — self-contained from the deployed copy under any python.
        assert "hpc_agent/execution/mapreduce/reduce/status.py" in rels
        assert "hpc_agent/execution/mapreduce/reduce/rollup.py" in rels
        assert "hpc_agent/_kernel/contract/task_id.py" in rels
        assert "hpc_agent/_kernel/contract/vocabulary.py" in rels
        assert "hpc_agent/errors.py" in rels
        assert "hpc_agent/infra/time.py" in rels
        assert "hpc_agent/execution/mapreduce/_guard.py" in rels
        # Cache manifest write (#242), riding the same transfer.
        assert ".hpc/.deploy_state.json" in rels
        assert len(rels) == 20, sorted(rels)


# ---------------------------------------------------------------------------
# ssh_run capture toggle
# ---------------------------------------------------------------------------


class TestSshRunCapture:
    def test_capture_true_routes_through_select_seam(self):
        # capture=True (the default) funnels through the close-pipes-on-exit
        # capture seam, not the blocking streaming subprocess.run path (#209).
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
            seam.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c")
        assert seam.call_count == 1
        # argv (remote command last) is the seam's first positional argument. It
        # is now the finding-20 wrapper; the inner command round-trips to "ls",
        # and the LAYER-2 marker rides the argv (visible to ps/pgrep).
        wrapped = seam.call_args[0][0][-1]
        assert _inner_remote_cmd(wrapped) == "ls"
        assert wrapped.startswith(f"{remote.OP_MARKER_PREFIX}=")
        assert "timeout -k" in wrapped

    def test_capture_false_toggles_capture_output(self):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is False


# ---------------------------------------------------------------------------
# run_combiner / run_combiner_checked
# ---------------------------------------------------------------------------


class TestRunCombiner:
    def test_run_combiner_default_no_force(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(ssh_target="u@c", remote_path="/p", wave=3, run_id="r1")
        argv = mock_run.call_args[0][0]
        cmd_str = argv[-1]
        assert "--wave 3" in cmd_str
        assert "--run-id r1" in cmd_str
        assert ".hpc/_hpc_combiner.py" in cmd_str
        assert "--force" not in cmd_str

    def test_run_combiner_force_appends_flag(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(
                ssh_target="u@c", remote_path="/p", wave=3, run_id="r1", force=True
            )
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str


class TestRunCombinerChecked:
    def test_returns_true_on_success(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp(stdout="ok\n", stderr="", returncode=0)
            ok, out, err = transport.run_combiner_checked(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1"
            )
        assert ok is True
        assert out == "ok\n"
        assert err == ""

    def test_returns_false_on_failure(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp(stdout="", stderr="boom", returncode=1)
            ok, out, err = transport.run_combiner_checked(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1"
            )
        assert ok is False
        assert out == ""
        assert err == "boom"

    def test_force_threaded_through(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner_checked(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1", force=True
            )
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str


class TestRunCombinerShellQuoting:
    def test_remote_path_with_space_is_quoted(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(
                ssh_target="u@c",
                remote_path="/path with space",
                wave=1,
                run_id="my run id",
            )
        cmd_str = _inner_remote_cmd(mock_run.call_args[0][0][-1])
        assert "cd '/path with space'" in cmd_str
        assert "HPC_RUN_ID='my run id'" in cmd_str
        assert "--run-id 'my run id'" in cmd_str


# ---------------------------------------------------------------------------
# Subprocess timeout enforcement
# ---------------------------------------------------------------------------


class TestModuleTimeoutConstants:
    """The module exposes two named timeout defaults — verify their
    presence and types so downstream consumers (and the boundary
    contract) have something stable to import.
    """

    def test_ssh_timeout_is_positive_int(self):
        assert isinstance(remote.SSH_TIMEOUT_SEC, int)
        assert remote.SSH_TIMEOUT_SEC > 0

    def test_rsync_timeout_is_positive_int(self):
        assert isinstance(remote.RSYNC_TIMEOUT_SEC, int)
        assert remote.RSYNC_TIMEOUT_SEC > 0

    def test_constants_exported_in_all(self):
        assert "SSH_TIMEOUT_SEC" in remote.__all__
        assert "RSYNC_TIMEOUT_SEC" in remote.__all__


class TestSshRunTimeout:
    def test_default_timeout_applied_when_omitted(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c", timeout=7.5)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 7.5

    def test_explicit_none_disables_enforcement(self):
        """Passing ``timeout=None`` is the documented escape hatch and must
        propagate as a literal ``None`` through the capture seam (and on to
        ``subprocess.run`` / ``Popen.wait``).
        """
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c", timeout=None)
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None

    def test_timeout_expired_reraised_as_timeout_error(self):
        cmd = "sleep 9999"
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                remote.ssh_run(cmd, ssh_target="alice@cluster.example")
        msg = str(exc_info.value)
        # Host (user@host) and a snippet of the command must appear.
        assert "alice@cluster.example" in msg
        assert "sleep 9999" in msg

    def test_timeout_message_truncates_long_command(self):
        long_cmd = "echo " + ("x" * 500)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=long_cmd, timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                remote.ssh_run(long_cmd, ssh_target="u@c")
        msg = str(exc_info.value)
        # The message must not embed the entire 500+ char command verbatim.
        assert long_cmd not in msg
        # But should contain the leading prefix.
        assert "echo " in msg

    def test_timeout_applies_when_capture_false(self):
        """``capture=False`` and ``timeout`` are orthogonal — the timeout
        still applies in streaming mode unless the caller opts out.
        """
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("tail -f log", ssh_target="u@c", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is False
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC


class TestRsyncPushTimeout:
    def test_default_timeout_applied_when_omitted(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout_sec") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=42,
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout_sec") == 42

    def test_explicit_none_disables_enforcement(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout_sec" in kwargs
        assert kwargs["timeout_sec"] is None

    def test_timeout_expired_reraised_as_timeout_error(self):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rsync ...", timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                transport.rsync_push(
                    ssh_target="alice@cluster.example",
                    remote_path="/u/home/alice/proj",
                    local_path="/tmp/local_src",
                )
        msg = str(exc_info.value)
        # Host must appear in the message.
        assert "cluster.example" in msg
        # And the src->dst arrow form (truncated) should be visible.
        assert "->" in msg
        assert "/tmp/local_src" in msg


class TestRsyncPullTimeout:
    def test_default_timeout_applied_when_omitted(self, tmp_path):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout_sec") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_none_disables_enforcement(self, tmp_path):
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout_sec" in kwargs
        assert kwargs["timeout_sec"] is None


class TestDeployRuntimeTimeout:
    """deploy_runtime emits one ssh prelude + one batched transfer (#252), each
    of which must carry the SSH timeout so a stuck cluster cannot block submit.
    """

    def test_each_subprocess_call_has_ssh_timeout(self):
        with (
            patch("hpc_agent.infra.remote._capture_via_select") as mock_ssh,
            patch("hpc_agent.infra.transport._have_rsync", return_value=True),
            patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run,
        ):
            mock_ssh.return_value = _cp()
            mock_run.return_value = _cp()
            transport.deploy_runtime(ssh_target="u@c", remote_path="/p")
        # The ssh mkdir (capture seam) and the single rsync transfer
        # (subprocess.run) must both carry the SSH timeout. Filter to the rsync
        # call (argv[0] == "rsync"): on Windows the one-time `ssh -V` version
        # probe (#243) also goes through subprocess.run but carries its own
        # short probe timeout, not the SSH transfer timeout.
        assert mock_ssh.call_args.kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC
        rsync_calls = [c for c in mock_run.call_args_list if c[0][0] and c[0][0][0] == "rsync"]
        assert rsync_calls, "expected one rsync transfer call"
        for call in rsync_calls:
            assert call.kwargs.get("timeout_sec") == remote.SSH_TIMEOUT_SEC


class TestRunCombinerTimeout:
    def test_default_timeout_threaded_through_to_ssh_run(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(ssh_target="u@c", remote_path="/p", wave=0, run_id="r1")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_threaded_through_to_ssh_run(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1", timeout=15
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 15

    def test_explicit_none_threaded_through_to_ssh_run(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1", timeout=None
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None


class TestRunCombinerCheckedTimeout:
    def test_default_timeout_threaded_through(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner_checked(ssh_target="u@c", remote_path="/p", wave=0, run_id="r1")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_threaded_through(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = _cp()
            transport.run_combiner_checked(
                ssh_target="u@c", remote_path="/p", wave=0, run_id="r1", timeout=21
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 21

    def test_timeout_propagates_as_timeout_error_not_ok_false(self):
        """A genuine cluster hang must surface as TimeoutError so
        callers can distinguish "remote returned non-zero" from "we
        never heard back".
        """
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                transport.run_combiner_checked(
                    ssh_target="u@c", remote_path="/p", wave=0, run_id="r1"
                )


# ---------------------------------------------------------------------------
# SSH rate-limit backoff
# ---------------------------------------------------------------------------


class TestSshBackoff:
    @pytest.fixture(autouse=True)
    def _enable_backoff(self, monkeypatch):
        """Local override: enable backoff and pin delays to zero for speed."""
        monkeypatch.delenv("HPC_SSH_NO_BACKOFF", raising=False)
        monkeypatch.setattr("hpc_agent.infra.remote._BACKOFF_DELAYS_SEC", (0.0,) * 4)
        # Ensure no actual sleeping in the very-rare-edge case the schedule
        # is consulted directly.
        monkeypatch.setattr("hpc_agent.infra.remote.time.sleep", lambda _: None)

    def test_ssh_run_retries_on_throttle_marker_then_succeeds(self):
        throttle_cp = _cp(
            stderr="kex_exchange_identification: Connection closed by remote host",
            returncode=255,
        )
        ok_cp = _cp(stdout="hi\n", returncode=0)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = [throttle_cp, throttle_cp, ok_cp]
            result = remote.ssh_run("ls", ssh_target="u@c")
        assert result.returncode == 0
        assert mock_run.call_count == 3  # two throttles + one success

    def test_ssh_run_does_not_retry_on_normal_failure(self):
        """Auth failures, command-not-found etc must surface immediately."""
        bad_cp = _cp(stderr="Permission denied (publickey).", returncode=255)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = bad_cp
            result = remote.ssh_run("ls", ssh_target="u@c")
        assert result.returncode == 255
        assert mock_run.call_count == 1

    def test_ssh_run_ladder_stops_at_circuit_trip(self):
        """Connection-marked failures trip the per-host breaker mid-ladder.

        Three consecutive connection-level failures open the circuit
        (ssh_circuit.CIRCUIT_THRESHOLD), so the 4th ladder rung fails fast
        with SshCircuitOpen instead of opening a 4th connection — the
        ban-hammer guard the 2026-07-04 probe storm showed was missing.
        """
        throttle_cp = _cp(stderr="ssh_exchange_identification: Connection closed", returncode=255)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = throttle_cp
            with pytest.raises(SshCircuitOpen):
                remote.ssh_run("ls", ssh_target="u@c")
        assert mock_run.call_count == 3

    def test_ssh_run_retries_then_gives_up_after_schedule_with_override(self, monkeypatch):
        """With the per-host override set, the historical exhaustion contract
        holds: every scheduled retry runs and the failing cp is RETURNED."""
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        throttle_cp = _cp(stderr="ssh_exchange_identification: Connection closed", returncode=255)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.return_value = throttle_cp
            result = remote.ssh_run("ls", ssh_target="u@c")
        # 1 initial + 4 retries = 5 attempts total when all return throttle.
        assert mock_run.call_count == 5
        assert result.returncode == 255

    def test_rsync_push_retries_on_protocol_marker(self):
        throttle_cp = _cp(
            stderr=(
                "ssh_exchange_identification: Connection closed by remote host\n"
                "rsync error: error in rsync protocol data stream (code 12)"
            ),
            returncode=12,
        )
        ok_cp = _cp(returncode=0)
        with patch("hpc_agent.infra.transport.run_capture_bounded") as mock_run:
            mock_run.side_effect = [throttle_cp, ok_cp]
            result = transport.rsync_push(ssh_target="u@c", remote_path="/p", local_path="/tmp/x")
        assert result.returncode == 0
        assert mock_run.call_count == 2

    def test_timeout_error_trips_circuit_mid_ladder(self):
        """Wrapper timeouts count as connection failures: rung 4 fails fast."""
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(SshCircuitOpen):
                remote.ssh_run("ls", ssh_target="u@c")
        assert mock_run.call_count == 3  # breaker opens at the 3rd failure

    def test_timeout_error_retries_then_raises_with_override(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                remote.ssh_run("ls", ssh_target="u@c")
        assert mock_run.call_count == 5  # 1 initial + 4 retries

    def test_non_idempotent_timeout_is_not_retried(self, monkeypatch):
        """F54 fire-path: a client-side TimeoutError on a NON-idempotent submit
        leg must surface immediately — the remote qsub runs under a server-side
        deadline that outlives the client by REMOTE_DEADLINE_MARGIN_SEC, so it may
        already have submitted the array; a retry would duplicate it. Even with
        the breaker overridden (so retries WOULD otherwise run), exactly one
        attempt is made."""
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                remote.ssh_run("qsub job.sh", ssh_target="u@c", idempotent=False)
        assert mock_run.call_count == 1  # NO retry — the duplicate-submit door is closed

    def test_non_idempotent_still_retries_a_throttle_reject(self, monkeypatch):
        """F54: an sshd rate-limit rejects the connection BEFORE the command
        dispatches, so re-trying it can never double-run — throttle retry is
        preserved even for a non-idempotent command."""
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        throttle_cp = _cp(stderr="ssh_exchange_identification: Connection closed", returncode=255)
        ok_cp = _cp(stdout="JOB1\n", returncode=0)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = [throttle_cp, ok_cp]
            r = remote.ssh_run("qsub job.sh", ssh_target="u@c", idempotent=False)
        assert r.returncode == 0
        assert mock_run.call_count == 2

    def test_non_idempotent_remote_scope_disables_timeout_retry(self, monkeypatch):
        """F54: the ambient non_idempotent_remote() scope reaches ssh_run without
        threading the keyword — the submit leg's seam. A client timeout inside it
        is not retried."""
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with remote.non_idempotent_remote(), pytest.raises(TimeoutError):
                remote.ssh_run("qsub job.sh", ssh_target="u@c")
        assert mock_run.call_count == 1

    def test_explicit_idempotent_kwarg_overrides_scope(self, monkeypatch):
        """An explicit idempotent=True wins over an enclosing non_idempotent_remote
        scope, so a nested idempotent probe still gets its retries."""
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with remote.non_idempotent_remote(), pytest.raises(TimeoutError):
                remote.ssh_run("qstat", ssh_target="u@c", idempotent=True)
        assert mock_run.call_count == 5  # retried despite the ambient non-idempotent scope


class TestEngineSeamIdempotence:
    """F55: the engine's ``except EngineUnavailable: pass`` one-shot fall-through
    is a SECOND re-execution door (besides the backoff timeout-retry). A command
    the engine already DISPATCHED must not be re-run one-shot when it is
    non-idempotent — that duplicates a qsub the remote may still be executing."""

    def _engine_raises(self, monkeypatch, exc):
        from hpc_agent.infra import ssh_engine

        monkeypatch.setattr(ssh_engine, "engine_enabled", lambda: True)

        def _raise(*_a, **_k):
            raise exc

        monkeypatch.setattr(ssh_engine, "engine_ssh_run", _raise)

    def test_non_idempotent_dispatched_failure_is_not_reexecuted(self, monkeypatch):
        """Fire-path: engine on, a POST-dispatch failure for a non-idempotent
        command surfaces (TimeoutError) instead of falling through to the one-shot
        path — the one-shot subprocess is never invoked."""
        from hpc_agent.infra import ssh_engine

        self._engine_raises(
            monkeypatch, ssh_engine.EngineUnavailable("torn mid-run", dispatched=True)
        )
        with (
            patch("hpc_agent.infra.remote._capture_via_select") as one_shot,
            pytest.raises(TimeoutError),
        ):
            remote.ssh_run("qsub job.sh", ssh_target="u@c", idempotent=False)
        one_shot.assert_not_called()  # the one-shot re-execution door is closed

    def test_idempotent_dispatched_failure_still_falls_through(self, monkeypatch):
        """Back-compat: an idempotent read surface keeps the 'engine can never be
        worse than off' contract — a dispatched failure falls through to one-shot
        and degrades normally."""
        from hpc_agent.infra import ssh_engine

        self._engine_raises(
            monkeypatch, ssh_engine.EngineUnavailable("torn mid-run", dispatched=True)
        )
        with patch("hpc_agent.infra.remote._capture_via_select") as one_shot:
            one_shot.return_value = _cp(stdout="ok\n", returncode=0)
            r = remote.ssh_run("qstat", ssh_target="u@c")  # idempotent default
        assert r.returncode == 0
        one_shot.assert_called_once()

    def test_non_idempotent_predispatch_failure_falls_through(self, monkeypatch):
        """A PRE-dispatch engine failure (breaker refused / failed connect) never
        ran the command, so even a non-idempotent command safely falls back."""
        from hpc_agent.infra import ssh_engine

        self._engine_raises(
            monkeypatch, ssh_engine.EngineUnavailable("connect refused")  # dispatched=False
        )
        with patch("hpc_agent.infra.remote._capture_via_select") as one_shot:
            one_shot.return_value = _cp(stdout="JOB1\n", returncode=0)
            r = remote.ssh_run("qsub job.sh", ssh_target="u@c", idempotent=False)
        assert r.returncode == 0
        one_shot.assert_called_once()


# Module-level (NOT under TestSshBackoff, whose autouse fixture zeroes the
# schedule): #308 re-expresses the hand-rolled backoff loop as a RetryPolicy.
# Pin parity against the *real* schedule so a future edit to _BACKOFF_DELAYS_SEC
# that broke the geometric doubling would surface here rather than silently
# changing which delays are slept.
def test_ssh_backoff_policy_reproduces_schedule_exactly():
    policy = remote._ssh_backoff_policy()
    # One initial attempt plus one retry per scheduled delay.
    assert policy.max_attempts == 1 + len(remote._BACKOFF_DELAYS_SEC)
    # delay_for is 1-based over the *retries*; it must match the tuple term for
    # term — i.e. 2s/4s/8s/16s for the production schedule.
    delays = [policy.delay_for(i) for i in range(1, len(remote._BACKOFF_DELAYS_SEC) + 1)]
    assert tuple(delays) == remote._BACKOFF_DELAYS_SEC
    # And the throttle signal + TimeoutError are exactly the retry triggers.
    assert policy.retry_on == (TimeoutError, remote._ThrottleRetry)


# ---------------------------------------------------------------------------
# Close-pipes-on-exit capture reader (#209): real-subprocess behaviour
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="select-loop reader is POSIX-only")
@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX /bin/sh")
class TestCaptureSelectReader:
    """Exercise the real ``_communicate_select`` / ``_capture_via_select`` path
    against a local ``sh`` so the anti-hang behaviour is verified without a
    cluster. POSIX-only (select(2) over pipes).
    """

    def test_captures_stdout_stderr_and_returncode(self):
        proc = subprocess.Popen(
            ["sh", "-c", "printf out; printf err 1>&2; exit 3"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = remote._communicate_select(proc, argv=["sh"], timeout=10)
        assert out == "out"
        assert err == "err"
        assert proc.returncode == 3

    def test_capture_via_select_returns_completedprocess(self):
        cp = remote._capture_via_select(["sh", "-c", "echo hi"], timeout=10)
        assert isinstance(cp, subprocess.CompletedProcess)
        assert cp.returncode == 0
        assert cp.stdout == "hi\n"
        assert cp.stderr == ""

    def test_large_output_is_not_truncated(self):
        # Exceeds a single pipe buffer / one os.read chunk, proving the loop
        # drains across many reads rather than losing data past 64 KiB.
        n = 100_000
        proc = subprocess.Popen(
            ["sh", "-c", f"yes x | head -c {n}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = remote._communicate_select(proc, argv=["sh"], timeout=30)
        assert len(out) == n

    @pytest.mark.slow
    def test_returns_before_backgrounded_child_closes_pipe(self):
        # THE regression (#209): a remote-style command whose foreground exits
        # immediately but which leaves a child holding the stdout/stderr pipe
        # must return at ~foreground speed, NOT wait for the child or the
        # timeout. A blocking read would block until the 3s child exits (pipe
        # EOF); the select reader returns as soon as the shell does.
        proc = subprocess.Popen(
            ["sh", "-c", "printf done; sleep 3 &"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        start = time.monotonic()
        out, _ = remote._communicate_select(proc, argv=["sh"], timeout=30)
        elapsed = time.monotonic() - start
        assert out == "done"
        assert elapsed < 1.5, f"reader waited {elapsed:.2f}s for a backgrounded child"

    @pytest.mark.slow
    def test_runaway_foreground_raises_timeout_and_is_killed(self):
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with pytest.raises(subprocess.TimeoutExpired):
            remote._communicate_select(proc, argv=["sh", "-c", "sleep 30"], timeout=0.5)
        # The child was killed + reaped, not left running.
        assert proc.returncode is not None


# ---------------------------------------------------------------------------
# Windows blocking-capture fallback: the timeout must be a HARD deadline
# ---------------------------------------------------------------------------
#
# The 2026-07-04 submit pre-flight wedge: ``subprocess.run(..., timeout=60)``
# is not a hard deadline — on TimeoutExpired it kills the child then calls an
# UNBOUNDED ``communicate()``, which blocks forever when a grandchild holds
# the inherited stdout/stderr handles (ssh ControlMaster mux / agent relay).
# ``_capture_windows`` replaces it with bounded waits only. The helper is
# plain ``Popen`` code, so it is exercised on every platform even though only
# Windows routes through it in production.


class TestCaptureWindowsBoundedTimeout:
    def test_returns_completedprocess_on_success(self):
        cp = remote._capture_windows(
            [sys.executable, "-c", "import sys; print('hi'); print('err', file=sys.stderr)"],
            timeout=30,
        )
        assert cp.returncode == 0
        assert cp.stdout.strip() == "hi"
        assert "err" in cp.stderr

    def test_timeout_fires_despite_grandchild_holding_pipes(self):
        """The wedge reproduction: the child spawns a grandchild that inherits
        its stdout/stderr handles, then sleeps. Killing the child does NOT
        close the pipes (the grandchild still holds them), so an unbounded
        post-kill drain — subprocess.run's behavior — blocks until the
        grandchild exits (~30s here; hours in the field). The bounded drain
        must surface TimeoutExpired in timeout + _POST_KILL_DRAIN_SEC + slack.
        """
        script = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=sys.stdout, stderr=sys.stderr); "
            "time.sleep(30)"
        )
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            remote._capture_windows([sys.executable, "-c", script], timeout=1.5)
        elapsed = time.monotonic() - start
        # Old behavior: ~30s (grandchild lifetime). Bound: 1.5s timeout +
        # 5s post-kill drain + generous process-spawn slack.
        assert elapsed < 20, f"post-kill drain not bounded: took {elapsed:.1f}s"

    def test_timeout_kills_child_and_reports_within_bound(self):
        start = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            remote._capture_windows(
                [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0
            )
        # No grandchild → kill closes the pipes → drain returns immediately.
        assert time.monotonic() - start < 15


class TestBackoffRetryIsLoud:
    """The ladder used to retry silently — a stalled connection looked like a
    dead driver. Each retry must leave a stderr breadcrumb naming the label,
    the attempt, and the failure."""

    @pytest.fixture(autouse=True)
    def _enable_backoff(self, monkeypatch):
        monkeypatch.delenv("HPC_SSH_NO_BACKOFF", raising=False)
        monkeypatch.setattr("hpc_agent.infra.remote._BACKOFF_DELAYS_SEC", (0.0,) * 4)
        monkeypatch.setattr("hpc_agent.infra.remote.time.sleep", lambda _: None)

    def test_throttle_retry_prints_attempt_line(self, capsys):
        throttle_cp = _cp(
            stderr="kex_exchange_identification: Connection closed by remote host",
            returncode=255,
        )
        ok_cp = _cp(stdout="hi\n", returncode=0)
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = [throttle_cp, ok_cp]
            remote.ssh_run("ls", ssh_target="u@c")
        err = capsys.readouterr().err
        assert "ssh u@c: attempt 1 failed" in err
        assert "kex_exchange_identification" in err
        assert "retrying in" in err

    def test_timeout_retry_prints_attempt_lines(self, capsys, monkeypatch):
        # Override the circuit breaker so all 4 scheduled retries actually run
        # (three consecutive timeouts would otherwise trip it mid-ladder —
        # that path is covered in TestSshBackoff / tests/infra/test_ssh_circuit.py).
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "c")
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                remote.ssh_run("ls", ssh_target="u@c")
        err = capsys.readouterr().err
        # 5 attempts total → 4 retry announcements (final failure raises).
        for n in (1, 2, 3, 4):
            assert f"ssh u@c: attempt {n} failed" in err


# ---------------------------------------------------------------------------
# asyncssh command-channel engine fast path (opt-in, HPC_SSH_ENGINE=asyncssh)
# ---------------------------------------------------------------------------
#
# The engine runs the command over a held asyncssh connection (a library
# channel with typed errors) instead of a cold one-shot handshake. It is the
# first (and, since the phase-1 broker was retired + deleted 2026-07-07, the
# ONLY) fast path in ``ssh_run``: capture-mode only, opt-in, and a hard
# fallback — any ``EngineUnavailable`` falls straight through to the one-shot
# path (engine → one-shot), so an engine can never regress the ban-sensitive
# default.
#
# The sibling module ``hpc_agent.infra.ssh_engine`` may not exist yet on disk,
# so these tests STUB it into ``sys.modules`` (the seam lazy-imports it inside
# the ``if capture:`` branch). They exercise the seam's ordering and fallback
# contract standalone — no asyncssh import, no cluster.


class _FakeEngine:
    """A stand-in for ``hpc_agent.infra.ssh_engine`` installed into sys.modules.

    Records whether ``engine_enabled`` / ``engine_ssh_run`` were consulted so a
    test can assert the seam's ordering and short-circuit behaviour.
    """

    class EngineUnavailable(Exception):
        pass

    def __init__(self, *, enabled, result=None, raise_unavailable=False):
        self._enabled = enabled
        self._result = result
        self._raise_unavailable = raise_unavailable
        self.enabled_calls = 0
        self.run_calls = 0
        self.last_kwargs = None

    def engine_enabled(self):
        self.enabled_calls += 1
        return self._enabled

    def engine_ssh_run(self, cmd, *, ssh_target, timeout):
        self.run_calls += 1
        self.last_kwargs = {"cmd": cmd, "ssh_target": ssh_target, "timeout": timeout}
        if self._raise_unavailable:
            raise self.EngineUnavailable("engine unavailable (test)")
        return self._result


def _install_engine(monkeypatch, engine):
    """Inject *engine* as the lazily-imported ``hpc_agent.infra.ssh_engine``.

    ``ssh_run`` does ``from hpc_agent.infra import ssh_engine`` inside the
    capture branch; we satisfy that both via sys.modules and the package attr
    so the import resolves to our stub whether or not the real file exists.
    """
    import hpc_agent.infra as infra_pkg

    monkeypatch.setitem(sys.modules, "hpc_agent.infra.ssh_engine", engine)
    monkeypatch.setattr(infra_pkg, "ssh_engine", engine, raising=False)


class TestSshRunEngineFastPath:
    def test_engine_enabled_and_succeeds_skips_one_shot(self, monkeypatch):
        """Engine returns → the one-shot capture seam is never invoked."""
        engine = _FakeEngine(enabled=True, result=_cp(stdout="from-engine\n"))
        _install_engine(monkeypatch, engine)
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
            result = remote.ssh_run("ls", ssh_target="u@c")
        assert result.stdout == "from-engine\n"
        assert engine.run_calls == 1
        assert seam.call_count == 0  # one-shot never touched
        # The seam passes the resolved effective timeout, not the sentinel.
        assert engine.last_kwargs["timeout"] == remote.SSH_TIMEOUT_SEC
        assert engine.last_kwargs["ssh_target"] == "u@c"

    def test_engine_unavailable_falls_through_to_one_shot(self, monkeypatch):
        """EngineUnavailable → fall through to the one-shot path."""
        engine = _FakeEngine(enabled=True, raise_unavailable=True)
        _install_engine(monkeypatch, engine)
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
            seam.return_value = _cp(stdout="from-oneshot\n")
            result = remote.ssh_run("ls", ssh_target="u@c")
        assert engine.run_calls == 1  # engine was tried
        assert seam.call_count == 1  # and we fell through to one-shot
        assert result.stdout == "from-oneshot\n"

    def test_engine_disabled_is_not_consulted(self, monkeypatch):
        """Flag OFF → engine_ssh_run is never called; one-shot runs as today."""
        engine = _FakeEngine(enabled=False)
        _install_engine(monkeypatch, engine)
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
            seam.return_value = _cp()
            remote.ssh_run("ls", ssh_target="u@c")
        assert engine.enabled_calls == 1  # gate checked
        assert engine.run_calls == 0  # but the channel never used
        assert seam.call_count == 1

    def test_capture_false_never_consults_the_engine(self, monkeypatch):
        """Streaming mode inherits the parent fds → the engine is skipped
        entirely (its gate isn't even checked)."""
        engine = _FakeEngine(enabled=True, result=_cp())
        _install_engine(monkeypatch, engine)
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("tail -f log", ssh_target="u@c", capture=False)
        assert engine.enabled_calls == 0
        assert engine.run_calls == 0
        assert mock_run.call_count == 1

    def test_engine_short_circuits_before_one_shot(self, monkeypatch):
        """Ordering: a succeeding engine short-circuits BEFORE the one-shot
        seam is reached (engine → one-shot). The phase-1 broker rung that once
        sat between them was retired + deleted 2026-07-07, so one-shot is now
        the only fallback left."""
        engine = _FakeEngine(enabled=True, result=_cp(stdout="engine\n"))
        _install_engine(monkeypatch, engine)
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
            result = remote.ssh_run("ls", ssh_target="u@c")
        assert result.stdout == "engine\n"
        assert engine.run_calls == 1
        assert seam.call_count == 0  # one-shot never reached


# ---------------------------------------------------------------------------
# build_remote_command — server-side self-destruct + self-id (run-12 finding 20)
# ---------------------------------------------------------------------------


class TestBuildRemoteCommand:
    def test_deadline_derives_from_client_budget_plus_margin(self):
        # timeout=60 → remote bound 60 + REMOTE_DEADLINE_MARGIN_SEC (60) = 120s,
        # so the client's own timeout normally fires first.
        wrapped = remote.build_remote_command("echo hi", timeout=60)
        assert f"timeout -k 10 {60 + remote.REMOTE_DEADLINE_MARGIN_SEC}s" in wrapped

    def test_no_client_timeout_gets_generous_default_never_unbounded(self):
        wrapped = remote.build_remote_command("sleep 1", timeout=None)
        assert f"{remote.REMOTE_DEADLINE_DEFAULT_SEC}s" in wrapped
        assert "timeout -k" in wrapped  # always bounded, never a bare command

    def test_marker_rides_both_environ_prefix_and_argv_dollar_zero(self):
        wrapped = remote.build_remote_command("echo hi", timeout=30, op="submit-s2")
        # Leading env-assignment (environ) and trailing bash $0 (argv/ps/pgrep)
        # are the SAME token.
        toks = shlex.split(wrapped)
        assert toks[0].startswith(f"{remote.OP_MARKER_PREFIX}=submit-s2:")
        assert toks[-1] == toks[0]

    def test_op_label_sanitised_to_argv_safe(self):
        wrapped = remote.build_remote_command("echo hi", timeout=30, op="weird op/$(x)")
        marker = shlex.split(wrapped)[0]
        # No shell metacharacters survive into the token — only the marker key,
        # a sanitised label ([A-Za-z0-9._-] with everything else → '_'), and the
        # epoch remain.
        assert re.fullmatch(rf"{remote.OP_MARKER_PREFIX}=[A-Za-z0-9._-]+:\d+", marker)
        for meta in (" ", "/", "$", "(", ")"):
            assert meta not in marker

    def test_quoting_round_trip_preserves_compound_command_byte_for_byte(self):
        original = "cd '/path with space' && python -m x | grep 'a b' && echo $?"
        wrapped = remote.build_remote_command(original, timeout=30)
        assert _inner_remote_cmd(wrapped) == original

    def test_escape_hatch_returns_command_unwrapped(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_NO_REMOTE_DEADLINE", "1")
        original = "cd /p && python reporter.py"
        assert remote.build_remote_command(original, timeout=30) == original

    def test_remote_op_context_sets_ambient_label(self):
        with remote.remote_op("verify-canary"):
            assert remote.current_remote_op() == "verify-canary"
            wrapped = remote.build_remote_command("echo hi", timeout=30)
        assert shlex.split(wrapped)[0].startswith(f"{remote.OP_MARKER_PREFIX}=verify-canary:")
        # Reset after the context.
        assert remote.current_remote_op() is None

    def test_explicit_op_overrides_ambient(self):
        with remote.remote_op("ambient"):
            wrapped = remote.build_remote_command("echo hi", timeout=30, op="explicit")
        assert shlex.split(wrapped)[0].startswith(f"{remote.OP_MARKER_PREFIX}=explicit:")


class TestCaptureSeamStdinIsolation:
    """The capture seams never hand the parent's stdin to a child (run-12
    finding 4): under ``mcp-serve`` (the default IN-PROCESS runner) the
    parent's stdin is the live JSON-RPC pipe, and ``ssh`` reads-and-forwards
    local stdin by default — an inheriting child steals protocol bytes or
    blocks forever. Both capture seams must give the child DEVNULL.

    Fire path: each seam runs in a RE-EXEC'd parent whose stdin carries
    pending bytes (pytest's own fd 0 is already null-like, so an in-process
    call could never catch an inheritance regression). The seam's child must
    read 0 bytes (DEVNULL EOF), never the parent's payload."""

    @staticmethod
    def _assert_seam_isolates(seam_name: str) -> None:
        inner = (
            f"from hpc_agent.infra.remote import {seam_name}; import sys; "
            f"p = {seam_name}([sys.executable, '-c', "
            "'import sys; print(len(sys.stdin.buffer.read()))'], timeout=30); "
            "print('CHILD_READ=' + p.stdout.strip())"
        )
        outer = subprocess.run(
            [sys.executable, "-c", inner],
            input="PROTOCOL-BYTES-THAT-MUST-NOT-LEAK",
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert outer.returncode == 0, outer.stderr
        assert "CHILD_READ=0" in outer.stdout

    def test_capture_via_select_child_stdin_is_devnull(self):
        self._assert_seam_isolates("_capture_via_select")

    def test_capture_windows_child_stdin_is_devnull(self):
        # The Windows-named seam is portable (plain Popen + communicate), so
        # its stdin isolation is pinned on every platform.
        self._assert_seam_isolates("_capture_windows")
