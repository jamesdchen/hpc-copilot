"""Tests for ``hpc_agent.infra.ssh_agent``.

Cross-platform SSH-agent detection: Unix uses ``SSH_AUTH_SOCK``;
Windows OpenSSH uses a named pipe (``\\\\.\\pipe\\openssh-ssh-agent``)
and never sets the env var, so on Windows we probe via ``ssh-add -l``.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent.infra import ssh_agent, ssh_options


def test_agent_available_unix_env_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "linux")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-XXXX/agent.123")
    assert ssh_agent.agent_available() is True


def test_agent_available_unix_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "linux")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh_agent.agent_available() is False


def test_agent_available_windows_env_var_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-var path wins even on Windows — no named-pipe probe needed."""
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-XXXX/agent.123")

    def _no_subprocess(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ssh-add should not be invoked when env var is set")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _no_subprocess)
    assert ssh_agent.agent_available() is True


def _stub_run(returncode: int) -> Any:
    def _run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    return _run


def test_agent_available_windows_named_pipe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_agent.subprocess, "run", _stub_run(0))
    assert ssh_agent.agent_available() is True


def test_agent_available_windows_named_pipe_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc=1 means agent reachable but no keys; still treated as available.

    Preflight surfaces the no-keys detail separately; the gate only
    cares whether an agent process is reachable at all.
    """
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_agent.subprocess, "run", _stub_run(1))
    assert ssh_agent.agent_available() is True


def test_agent_available_windows_named_pipe_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_agent.subprocess, "run", _stub_run(2))
    assert ssh_agent.agent_available() is False


def test_agent_available_windows_ssh_add_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("ssh-add")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _raise)
    assert ssh_agent.agent_available() is False


def test_agent_available_windows_ssh_add_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="ssh-add", timeout=5)

    monkeypatch.setattr(ssh_agent.subprocess, "run", _raise)
    assert ssh_agent.agent_available() is False


def test_agent_detail_unix_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "linux")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.42")
    assert ssh_agent.agent_detail() == "SSH_AUTH_SOCK=/tmp/agent.42"


def test_agent_detail_unix_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "linux")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert ssh_agent.agent_detail() == "SSH_AUTH_SOCK is not set"


def test_agent_detail_windows_pipe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    def _run(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout="2048 SHA256:abc user@host (RSA)\n", stderr="")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _run)
    detail = ssh_agent.agent_detail()
    assert "named-pipe agent" in detail
    assert "RSA" in detail


def test_agent_detail_windows_pipe_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_agent.subprocess, "run", _stub_run(1))
    assert "no keys loaded" in ssh_agent.agent_detail()


def test_agent_detail_windows_pipe_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_agent.subprocess, "run", _stub_run(2))
    assert "unreachable" in ssh_agent.agent_detail()


def test_agent_available_uses_resolved_ssh_add_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The named-pipe probe must invoke the *resolved* ssh-add binary (via the
    ssh_argv seam), not the bare name Git Bash would shadow with its own
    /usr/bin/ssh-add. Patch the resolver ssh_argv consults."""
    monkeypatch.setattr(ssh_agent.sys, "platform", "win32")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(ssh_options, "_ssh_add_binary", lambda: "/native/ssh-add.exe")
    seen: dict[str, Any] = {}

    def _run(argv: Any, *args: Any, **kwargs: Any) -> Any:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ssh_agent.subprocess, "run", _run)
    assert ssh_agent.agent_available() is True
    assert seen["argv"][0] == "/native/ssh-add.exe"
