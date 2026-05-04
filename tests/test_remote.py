"""Tests for claude_hpc.infra.remote (ssh/rsync/combiner helpers).

Mocks subprocess.run via unittest.mock.patch.  Covers argv composition
(rsync flags, include/exclude order, trailing slashes) and the
run_combiner / run_combiner_checked return-shape contract.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from claude_hpc.infra import remote


def _cp(stdout="", stderr="", returncode=0):
    """Mimic subprocess.CompletedProcess enough for the remote module."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# rsync_push
# ---------------------------------------------------------------------------


class TestRsyncPush:
    def test_flag_composition_with_defaults(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="cluster.example",
                user="alice",
                remote_path="/u/home/alice/proj",
                local_path="/tmp/local_src",
            )

        argv = mock_run.call_args[0][0]
        assert argv[0] == "rsync"
        assert "-az" in argv
        # --delete is on by default
        assert "--delete" in argv
        # excludes from DEFAULT_RSYNC_EXCLUDES, preserving order
        exclude_patterns = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--exclude"]
        assert exclude_patterns == remote.DEFAULT_RSYNC_EXCLUDES
        # Source has trailing slash
        src = argv[-2]
        assert src.endswith("/")
        assert src.rstrip("/") == "/tmp/local_src"
        # Destination has trailing slash, user@host:path/
        dst = argv[-1]
        assert dst == "alice@cluster.example:/u/home/alice/proj/"

    def test_delete_toggle_off(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="c",
                user="u",
                remote_path="/p",
                local_path="/tmp/x",
                delete=False,
            )
        argv = mock_run.call_args[0][0]
        assert "--delete" not in argv

    def test_custom_excludes_passed_in_order(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="c",
                user="u",
                remote_path="/p",
                local_path="/tmp/x",
                exclude=["a/", "b/", "c/"],
            )
        argv = mock_run.call_args[0][0]
        patterns = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--exclude"]
        assert patterns == ["a/", "b/", "c/"]


# ---------------------------------------------------------------------------
# rsync_pull
# ---------------------------------------------------------------------------


class TestRsyncPull:
    def test_with_include_list_filters_in_correct_order(self, tmp_path):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_pull(
                host="c",
                user="u",
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
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_pull(
                host="c",
                user="u",
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
    """Verify call order: 1 ssh (mkdir), then 1 scp per source file.

    The current code scp's metrics_io.py, then combiner.py in that order.
    (The earlier orphan ``context.py`` push was removed in regfix because
    the source module never existed on disk; deploy_runtime would have
    raised FileNotFoundError on the scp call.)
    """

    def test_ssh_mkdir_then_scps_in_order(self):
        # subprocess.run is used both inside ssh_run (mkdir) and for each scp.
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.deploy_runtime(host="c", user="u", remote_path="/p")

        all_calls = mock_run.call_args_list
        # Expect 11 subprocess.run invocations:
        #   1 ssh (mkdir -p claude_hpc/mapreduce, .hpc/templates, .hpc/templates/common),
        #   1 scp into claude_hpc/mapreduce/ (metrics_io.py),
        #   1 scp into claude_hpc/ (executor_cli.py — for tasks.py imports),
        #   1 scp into .hpc/_hpc_dispatch.py,
        #   4 scp into .hpc/templates/ (sge cpu/gpu, slurm cpu/gpu),
        #   2 scp into .hpc/templates/common/ (hpc_preamble.sh, gpu_preamble.sh),
        #   1 scp into .hpc/_hpc_combiner.py.
        assert len(all_calls) == 11, [c[0][0][:3] for c in all_calls]

        argvs = [c[0][0] for c in all_calls]

        # ssh mkdir is first
        assert argvs[0][0] == "ssh"
        assert "mkdir -p" in argvs[0][-1]
        assert ".hpc/templates" in argvs[0][-1]
        assert ".hpc/templates/common" in argvs[0][-1]

        # Importable stub into claude_hpc/mapreduce/
        assert argvs[1][0] == "scp"
        assert argvs[1][1].endswith("metrics_io.py")
        assert argvs[1][2].endswith(":/p/claude_hpc/mapreduce/metrics_io.py")

        # executor_cli stub into claude_hpc/ (so tasks.py top-level
        # ``from claude_hpc.executor_cli import ...`` resolves on cluster).
        assert argvs[2][0] == "scp"
        assert argvs[2][1].endswith("executor_cli.py")
        assert argvs[2][2].endswith(":/p/claude_hpc/executor_cli.py")

        # Framework executor into .hpc/
        assert argvs[3][0] == "scp"
        assert argvs[3][1].endswith("dispatch.py")
        assert argvs[3][2].endswith(":/p/.hpc/_hpc_dispatch.py")

        # Four templates into .hpc/templates/
        template_dsts = {argv[2] for argv in argvs[4:8]}
        assert all(argv[0] == "scp" for argv in argvs[4:8])
        assert any(d.endswith(":/p/.hpc/templates/cpu_array.sh") for d in template_dsts)
        assert any(d.endswith(":/p/.hpc/templates/gpu_array.sh") for d in template_dsts)
        assert any(d.endswith(":/p/.hpc/templates/cpu_array.slurm") for d in template_dsts)
        assert any(d.endswith(":/p/.hpc/templates/gpu_array.slurm") for d in template_dsts)

        # Two shared preambles into .hpc/templates/common/
        common_dsts = {argv[2] for argv in argvs[8:10]}
        assert all(argv[0] == "scp" for argv in argvs[8:10])
        assert any(d.endswith(":/p/.hpc/templates/common/hpc_preamble.sh") for d in common_dsts)
        assert any(d.endswith(":/p/.hpc/templates/common/gpu_preamble.sh") for d in common_dsts)

        # Combiner is last
        assert argvs[10][0] == "scp"
        assert argvs[10][1].endswith("combiner.py")
        assert argvs[10][2].endswith(":/p/.hpc/_hpc_combiner.py")


# ---------------------------------------------------------------------------
# ssh_run capture toggle
# ---------------------------------------------------------------------------


class TestSshRunCapture:
    def test_capture_true_by_default(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is True

    def test_capture_false_toggles_capture_output(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is False


# ---------------------------------------------------------------------------
# run_combiner / run_combiner_checked
# ---------------------------------------------------------------------------


class TestRunCombiner:
    def test_run_combiner_default_no_force(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(host="c", user="u", remote_path="/p", wave=3, run_id="r1")
        argv = mock_run.call_args[0][0]
        cmd_str = argv[-1]
        assert "--wave 3" in cmd_str
        assert "--run-id r1" in cmd_str
        assert ".hpc/_hpc_combiner.py" in cmd_str
        assert "--force" not in cmd_str

    def test_run_combiner_force_appends_flag(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(
                host="c", user="u", remote_path="/p", wave=3, run_id="r1", force=True
            )
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str


class TestRunCombinerChecked:
    def test_returns_true_on_success(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp(stdout="ok\n", stderr="", returncode=0)
            ok, out, err = remote.run_combiner_checked(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1"
            )
        assert ok is True
        assert out == "ok\n"
        assert err == ""

    def test_returns_false_on_failure(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp(stdout="", stderr="boom", returncode=1)
            ok, out, err = remote.run_combiner_checked(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1"
            )
        assert ok is False
        assert out == ""
        assert err == "boom"

    def test_force_threaded_through(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner_checked(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1", force=True
            )
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str


class TestRunCombinerShellQuoting:
    def test_remote_path_with_space_is_quoted(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(
                host="c",
                user="u",
                remote_path="/path with space",
                wave=1,
                run_id="my run id",
            )
        cmd_str = mock_run.call_args[0][0][-1]
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
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u", timeout=7.5)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 7.5

    def test_explicit_none_disables_enforcement(self):
        """Passing ``timeout=None`` is the documented escape hatch and
        must propagate as a literal ``None`` to ``subprocess.run``.
        """
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u", timeout=None)
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None

    def test_timeout_expired_reraised_as_timeout_error(self):
        cmd = "sleep 9999"
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=cmd, timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                remote.ssh_run(cmd, host="cluster.example", user="alice")
        msg = str(exc_info.value)
        # Host (user@host) and a snippet of the command must appear.
        assert "alice@cluster.example" in msg
        assert "sleep 9999" in msg

    def test_timeout_message_truncates_long_command(self):
        long_cmd = "echo " + ("x" * 500)
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd=long_cmd, timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                remote.ssh_run(long_cmd, host="c", user="u")
        msg = str(exc_info.value)
        # The message must not embed the entire 500+ char command verbatim.
        assert long_cmd not in msg
        # But should contain the leading prefix.
        assert "echo " in msg

    def test_timeout_applies_when_capture_false(self):
        """``capture=False`` and ``timeout`` are orthogonal — the timeout
        still applies in streaming mode unless the caller opts out.
        """
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("tail -f log", host="c", user="u", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is False
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC


class TestRsyncPushTimeout:
    def test_default_timeout_applied_when_omitted(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="c",
                user="u",
                remote_path="/p",
                local_path="/tmp/x",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="c",
                user="u",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=42,
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 42

    def test_explicit_none_disables_enforcement(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_push(
                host="c",
                user="u",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None

    def test_timeout_expired_reraised_as_timeout_error(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="rsync ...", timeout=1.0)
            with pytest.raises(TimeoutError) as exc_info:
                remote.rsync_push(
                    host="cluster.example",
                    user="alice",
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
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_pull(
                host="c",
                user="u",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_none_disables_enforcement(self, tmp_path):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.rsync_pull(
                host="c",
                user="u",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None


class TestDeployRuntimeTimeout:
    """deploy_runtime emits one ssh + three scp calls, each of which
    must carry the SSH timeout so a stuck cluster cannot block submit.
    """

    def test_each_subprocess_call_has_ssh_timeout(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.deploy_runtime(host="c", user="u", remote_path="/p")
        for call in mock_run.call_args_list:
            assert call.kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC


class TestRunCombinerTimeout:
    def test_default_timeout_threaded_through_to_ssh_run(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(host="c", user="u", remote_path="/p", wave=0, run_id="r1")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_threaded_through_to_ssh_run(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1", timeout=15
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 15

    def test_explicit_none_threaded_through_to_ssh_run(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1", timeout=None
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None


class TestRunCombinerCheckedTimeout:
    def test_default_timeout_threaded_through(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner_checked(host="c", user="u", remote_path="/p", wave=0, run_id="r1")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC

    def test_explicit_timeout_threaded_through(self):
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner_checked(
                host="c", user="u", remote_path="/p", wave=0, run_id="r1", timeout=21
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 21

    def test_timeout_propagates_as_timeout_error_not_ok_false(self):
        """A genuine cluster hang must surface as TimeoutError so
        callers can distinguish "remote returned non-zero" from "we
        never heard back".
        """
        with patch("claude_hpc.infra.remote.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                remote.run_combiner_checked(
                    host="c", user="u", remote_path="/p", wave=0, run_id="r1"
                )
