"""Tests for hpc_mapreduce.infra.remote (ssh/rsync/combiner helpers).

Mocks subprocess.run via unittest.mock.patch.  Covers argv composition
(rsync flags, include/exclude order, trailing slashes) and the
run_combiner / run_combiner_checked return-shape contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from hpc_mapreduce.infra import remote


def _cp(stdout="", stderr="", returncode=0):
    """Mimic subprocess.CompletedProcess enough for the remote module."""
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# rsync_push
# ---------------------------------------------------------------------------


class TestRsyncPush:
    def test_flag_composition_with_defaults(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
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

    The current code scp's context.py, metrics_io.py, then combiner.py in
    that order.  If context.py doesn't exist on disk we still test the call
    sequence that the function emits.
    """

    def test_ssh_mkdir_then_scps_in_order(self):
        # subprocess.run is used both inside ssh_run (first call) and for each scp.
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.deploy_runtime(host="c", user="u", remote_path="/p")

        all_calls = mock_run.call_args_list
        # Expect 4 subprocess.run invocations: ssh (mkdir), scp x3
        # (context.py, metrics_io.py, combiner.py).
        assert len(all_calls) >= 4

        first_argv = all_calls[0][0][0]
        second_argv = all_calls[1][0][0]
        third_argv = all_calls[2][0][0]
        fourth_argv = all_calls[3][0][0]

        assert first_argv[0] == "ssh"
        assert "mkdir -p" in first_argv[-1]

        assert second_argv[0] == "scp"
        assert second_argv[1].endswith("context.py")
        assert second_argv[2].endswith(":/p/hpc_mapreduce/map/context.py")

        assert third_argv[0] == "scp"
        assert third_argv[1].endswith("metrics_io.py")
        assert third_argv[2].endswith(":/p/hpc_mapreduce/map/metrics_io.py")

        assert fourth_argv[0] == "scp"
        assert fourth_argv[1].endswith("combiner.py")
        assert fourth_argv[2].endswith(":/p/_hpc_combiner.py")


# ---------------------------------------------------------------------------
# ssh_run capture toggle
# ---------------------------------------------------------------------------


class TestSshRunCapture:
    def test_capture_true_by_default(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u")
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is True

    def test_capture_false_toggles_capture_output(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.ssh_run("ls", host="c", user="u", capture=False)
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("capture_output") is False


# ---------------------------------------------------------------------------
# run_combiner / run_combiner_checked
# ---------------------------------------------------------------------------


class TestRunCombiner:
    def test_run_combiner_default_no_force(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(host="c", user="u", remote_path="/p", wave=3)
        argv = mock_run.call_args[0][0]
        # argv = ["ssh", "u@c", "<command string>"]
        cmd_str = argv[-1]
        assert "--wave 3" in cmd_str
        assert "--manifest _hpc_dispatch.json" in cmd_str
        assert "--force" not in cmd_str

    def test_run_combiner_force_appends_flag(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner(host="c", user="u", remote_path="/p", wave=3, force=True)
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str


class TestRunCombinerChecked:
    def test_returns_true_on_success(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp(stdout="ok\n", stderr="", returncode=0)
            ok, out, err = remote.run_combiner_checked(host="c", user="u", remote_path="/p", wave=0)
        assert ok is True
        assert out == "ok\n"
        assert err == ""

    def test_returns_false_on_failure(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp(stdout="", stderr="boom", returncode=1)
            ok, out, err = remote.run_combiner_checked(host="c", user="u", remote_path="/p", wave=0)
        assert ok is False
        assert out == ""
        assert err == "boom"

    def test_force_threaded_through(self):
        with patch("hpc_mapreduce.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            remote.run_combiner_checked(host="c", user="u", remote_path="/p", wave=0, force=True)
        cmd_str = mock_run.call_args[0][0][-1]
        assert "--force" in cmd_str
