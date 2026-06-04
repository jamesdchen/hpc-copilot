"""Tests for ``hpc_agent.ops.preflight.check.check_preflight``.

Focus on the file-transfer capability check: a missing ``rsync`` must
NOT fail preflight when the ``scp``+``tar`` fallback transport is
available — the path Windows hosts without WSL/MSYS rsync take, where
``infra.remote`` uses a ``tar c | ssh tar x`` push / ``scp -r`` pull
pipeline instead.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from hpc_agent.infra import ssh_agent
from hpc_agent.ops.preflight import check as preflight


def _which_for(present: set[str]) -> Callable[[str], str | None]:
    """Return a ``shutil.which`` stub that resolves only *present* binaries."""

    def _which(binary: str) -> str | None:
        return f"/usr/bin/{binary}" if binary in present else None

    return _which


def _checks_by_name(present: set[str]) -> dict[str, dict]:
    """Run check_preflight with only *present* binaries on PATH; index by name."""
    with mock.patch.object(preflight.shutil, "which", _which_for(present)):
        result = preflight.check_preflight()
    return {c["name"]: c for c in result["checks"]}


def test_rsync_present_satisfies_file_transfer() -> None:
    checks = _checks_by_name({"ssh", "rsync", "scp", "tar"})
    assert checks["file_transfer_on_path"]["ok"] is True
    assert "rsync" in checks["file_transfer_on_path"]["detail"]


def test_rsync_absent_scp_tar_fallback_satisfies_file_transfer() -> None:
    """The Windows path: no rsync, but scp+tar cover the transport."""
    checks = _checks_by_name({"ssh", "scp", "tar"})
    assert checks["file_transfer_on_path"]["ok"] is True
    assert "fallback" in checks["file_transfer_on_path"]["detail"]


def test_partial_fallback_fails_file_transfer() -> None:
    """scp without tar (or vice versa) is not a usable fallback."""
    checks = _checks_by_name({"ssh", "scp"})
    ft = checks["file_transfer_on_path"]
    assert ft["ok"] is False
    assert "tar" in ft["detail"]


def test_no_transport_fails_file_transfer() -> None:
    checks = _checks_by_name({"ssh"})
    ft = checks["file_transfer_on_path"]
    assert ft["ok"] is False
    assert "rsync" in ft["detail"]
    assert "scp" in ft["detail"]
    assert "tar" in ft["detail"]


def test_ssh_check_named_ssh_on_path_and_no_legacy_rsync_check() -> None:
    """ssh keeps its own check; the old rsync_on_path check is gone."""
    checks = _checks_by_name({"ssh", "rsync"})
    assert checks["ssh_on_path"]["ok"] is True
    assert "rsync_on_path" not in checks


def test_ssh_auth_sock_windows_named_pipe_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows path: env var unset but named-pipe agent reachable → ok=True.

    Check name stays ``ssh_auth_sock`` for backwards compat; the detail
    surfaces the named-pipe state.
    """
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def _run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout="2048 SHA256:xyz key (RSA)\n", stderr="")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _run)
    with mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})):
        result = preflight.check_preflight()
    checks = {c["name"]: c for c in result["checks"]}
    assert "ssh_auth_sock" in checks
    assert checks["ssh_auth_sock"]["ok"] is True
    assert "named-pipe" in checks["ssh_auth_sock"]["detail"]


def test_ssh_auth_sock_windows_no_agent_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows path: env var unset and named-pipe unreachable → ok=False."""
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def _run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=2, stdout="", stderr="")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _run)
    with mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})):
        result = preflight.check_preflight()
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["ssh_auth_sock"]["ok"] is False


def test_ssh_auth_sock_unix_unset_fails_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unix path: env var unset → ok=False with the original message kept."""
    monkeypatch.setattr(ssh_agent.sys, "platform", "linux")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    with mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})):
        result = preflight.check_preflight()
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["ssh_auth_sock"]["ok"] is False
    assert "SSH_AUTH_SOCK is not set" in checks["ssh_auth_sock"]["detail"]
    assert "eval $(ssh-agent -s)" in checks["ssh_auth_sock"]["detail"]


# ─── un-customized clusters.yaml placeholders (issue #135 item 2) ────────────


def test_placeholder_fields_flags_your_tokens() -> None:
    entry = {
        "host": "hoffman2.idre.ucla.edu",
        "user": "<your_user>",
        "scratch": "/real/scratch",
        "conda_envs": ["<your_env>"],
    }
    assert preflight._placeholder_fields(entry) == ["conda_envs", "user"]


def test_placeholder_fields_clean_entry() -> None:
    entry = {"user": "jamesdc1", "scratch": "/u/scratch/j/jamesdc1", "conda_envs": ["hpc-pi"]}
    assert preflight._placeholder_fields(entry) == []


def _tcp_ok() -> mock.MagicMock:
    cm = mock.MagicMock()
    cm.__enter__ = mock.MagicMock(return_value=cm)
    cm.__exit__ = mock.MagicMock(return_value=False)
    return cm


def _ssh_echo_ok() -> SimpleNamespace:
    """Canned successful ``ssh_run`` result for the cluster_ssh_echo probe."""
    return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")


def test_preflight_fails_on_uncustomized_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clusters.yaml entry still carrying ``<your_...>`` tokens must fail the
    cluster_config_customized check even when TCP :22 and ssh echo are green."""
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"hoffman2": {"host": "h", "user": "<your_user>", "conda_envs": ["<your_env>"]}},
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", return_value=_tcp_ok()),
        mock.patch("hpc_agent.infra.remote.ssh_run", return_value=_ssh_echo_ok()),
    ):
        result = preflight.check_preflight(cluster="hoffman2")
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_tcp_22"]["ok"] is True
    assert checks["cluster_ssh_echo"]["ok"] is True
    assert checks["cluster_config_customized"]["ok"] is False
    assert "user" in checks["cluster_config_customized"]["detail"]
    assert result["all_ok"] is False


def test_preflight_passes_when_cluster_customized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"hoffman2": {"host": "h", "user": "jamesdc1", "conda_envs": ["hpc-pi"]}},
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", return_value=_tcp_ok()),
        mock.patch("hpc_agent.infra.remote.ssh_run", return_value=_ssh_echo_ok()),
    ):
        result = preflight.check_preflight(cluster="hoffman2")
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_config_customized"]["ok"] is True
    # Pin the new functional ssh probe: green here means the production
    # ssh path (ssh_argv + multiplex + crypto) actually works, not just
    # that port 22 is reachable. Catches the 2026-06-04 demo class —
    # named-pipe ControlMaster bind failure, Git-Bash-vs-native-OpenSSH
    # binary mismatch, ssh-agent unreachable — that TCP alone misses.
    assert checks["cluster_ssh_echo"]["ok"] is True


def test_preflight_fails_when_ssh_echo_fails_despite_tcp_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the 2026-06-04 inert-guard class: port 22 open AND
    ssh round-trip failing is the production-broken state preflight
    previously missed. The new cluster_ssh_echo check must fail here."""
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"hoffman2": {"host": "h", "user": "jamesdc1", "conda_envs": ["hpc-pi"]}},
    )
    # The classic named-pipe bind failure: ssh exits non-zero with the
    # marker in stderr. Production submit would hit the same.
    ssh_bad = SimpleNamespace(
        returncode=255,
        stdout="",
        stderr="getsockname failed: Not a socket\n",
    )
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(preflight.socket, "create_connection", return_value=_tcp_ok()),
        mock.patch("hpc_agent.infra.remote.ssh_run", return_value=ssh_bad),
    ):
        result = preflight.check_preflight(cluster="hoffman2")
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_tcp_22"]["ok"] is True  # the inert guard would have said "green"
    assert checks["cluster_ssh_echo"]["ok"] is False  # the new functional guard catches it
    assert "getsockname" in checks["cluster_ssh_echo"]["detail"]
    assert result["all_ok"] is False


def test_preflight_skips_ssh_echo_when_tcp_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When TCP :22 is unreachable, the ssh_echo probe is skipped (no point
    burning the 5s ssh timeout on a host we already know is offline)."""
    monkeypatch.setattr(
        preflight,
        "load_clusters_config",
        lambda: {"hoffman2": {"host": "h", "user": "jamesdc1", "conda_envs": ["hpc-pi"]}},
    )
    ssh_mock = mock.MagicMock()
    with (
        mock.patch.object(preflight.shutil, "which", _which_for({"ssh", "rsync"})),
        mock.patch.object(
            preflight.socket,
            "create_connection",
            side_effect=OSError("connection refused"),
        ),
        mock.patch("hpc_agent.infra.remote.ssh_run", ssh_mock),
    ):
        result = preflight.check_preflight(cluster="hoffman2")
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["cluster_tcp_22"]["ok"] is False
    assert "cluster_ssh_echo" not in checks  # skipped on tcp fail
    ssh_mock.assert_not_called()
    assert result["all_ok"] is False
