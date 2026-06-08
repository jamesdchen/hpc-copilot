"""Tests for hpc_agent.infra.remote + hpc_agent.infra.transport.

Mocks subprocess.run via unittest.mock.patch.  Covers argv composition
(rsync flags, include/exclude order, trailing slashes) and the
run_combiner / run_combiner_checked return-shape contract.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.infra import remote, transport
from hpc_agent.infra.ssh_options import _ssh_binary


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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        )
        # Source has trailing slash
        src = argv[-2]
        assert src.endswith("/")
        assert src.rstrip("/") == "/tmp/local_src"
        # Destination has trailing slash, user@host:path/
        dst = argv[-1]
        assert dst == "alice@cluster.example:/u/home/alice/proj/"

    def test_delete_toggle_off(self):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        # (clusters.yaml) and protected output dirs (results/, _combiner/ —
        # #173) are always unioned in and cannot be dropped.
        assert patterns == ["a/", "b/", "c/", "clusters.yaml", "results/", "_combiner/"]


# ---------------------------------------------------------------------------
# rsync_pull
# ---------------------------------------------------------------------------


class TestRsyncPull:
    def test_with_include_list_filters_in_correct_order(self, tmp_path):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        # cache-manifest write. Ten files (no scheduler arg → sge + slurm
        # templates) + manifest = 11 dst_rels.
        assert captured["ssh_target"] == "u@c"
        assert captured["remote_path"] == "/p"
        rels = set(captured["dst_rels"])
        # Importable stubs into hpc_agent/ (so cluster-side user imports resolve).
        assert "hpc_agent/execution/mapreduce/metrics_io.py" in rels
        assert "hpc_agent/executor_cli.py" in rels
        # Framework executor + combiner into .hpc/
        assert ".hpc/_hpc_dispatch.py" in rels
        assert ".hpc/_hpc_combiner.py" in rels
        # Four templates into .hpc/templates/
        assert ".hpc/templates/cpu_array.sh" in rels
        assert ".hpc/templates/gpu_array.sh" in rels
        assert ".hpc/templates/cpu_array.slurm" in rels
        assert ".hpc/templates/gpu_array.slurm" in rels
        # Two shared preambles into .hpc/templates/common/
        assert ".hpc/templates/common/hpc_preamble.sh" in rels
        assert ".hpc/templates/common/gpu_preamble.sh" in rels
        # Cache manifest write (#242), riding the same transfer.
        assert ".hpc/.deploy_state.json" in rels
        assert len(rels) == 11, sorted(rels)


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
        # argv (remote command last) is the seam's first positional argument.
        assert seam.call_args[0][0][-1] == "ls"

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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_timeout_overrides_default(self):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=42,
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == 42

    def test_explicit_none_disables_enforcement(self):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_push(
                ssh_target="u@c",
                remote_path="/p",
                local_path="/tmp/x",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None

    def test_timeout_expired_reraised_as_timeout_error(self):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
            )
        kwargs = mock_run.call_args.kwargs
        assert kwargs.get("timeout") == remote.RSYNC_TIMEOUT_SEC

    def test_explicit_none_disables_enforcement(self, tmp_path):
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.return_value = _cp()
            transport.rsync_pull(
                ssh_target="u@c",
                remote_path="/p",
                remote_subdir="results",
                local_dir=tmp_path / "out",
                timeout=None,
            )
        kwargs = mock_run.call_args.kwargs
        assert "timeout" in kwargs
        assert kwargs["timeout"] is None


class TestDeployRuntimeTimeout:
    """deploy_runtime emits one ssh prelude + one batched transfer (#252), each
    of which must carry the SSH timeout so a stuck cluster cannot block submit.
    """

    def test_each_subprocess_call_has_ssh_timeout(self):
        with (
            patch("hpc_agent.infra.remote._capture_via_select") as mock_ssh,
            patch("hpc_agent.infra.transport._have_rsync", return_value=True),
            patch("hpc_agent.infra.remote.subprocess.run") as mock_run,
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
            assert call.kwargs.get("timeout") == remote.SSH_TIMEOUT_SEC


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

    def test_ssh_run_retries_then_gives_up_after_schedule(self):
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
        with patch("hpc_agent.infra.remote.subprocess.run") as mock_run:
            mock_run.side_effect = [throttle_cp, ok_cp]
            result = transport.rsync_push(ssh_target="u@c", remote_path="/p", local_path="/tmp/x")
        assert result.returncode == 0
        assert mock_run.call_count == 2

    def test_timeout_error_retries_then_raises(self):
        with patch("hpc_agent.infra.remote._capture_via_select") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="ssh ...", timeout=1.0)
            with pytest.raises(TimeoutError):
                remote.ssh_run("ls", ssh_target="u@c")
        assert mock_run.call_count == 5  # 1 initial + 4 retries


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
