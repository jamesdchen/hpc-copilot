"""ConnectTimeout bound spliced into ssh-family calls (ban-driver hardening).

OpenSSH ships no default ``ConnectTimeout``; an unreachable/misconfigured host
then hangs until ``infra.remote``'s ``SSH_TIMEOUT_SEC`` (60s) hard-kill. A burst
of such slow failures from one IP is exactly what a cluster's fail2ban /
connection-rate limiter bans. ``_ssh_connect_opts`` pins a tight connect bound
so the misconfig surfaces fast, before the pile-up forms — while a legitimate
long-running command keeps the larger command-phase budget.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _modern(monkeypatch):
    """Pin a modern local OpenSSH so the version probe never shells out and the
    crypto opts are present (they share ssh_argv with the connect bound)."""
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
    ssh_options._local_openssh_supports_gcm.cache_clear()
    yield
    ssh_options._local_openssh_supports_gcm.cache_clear()


def test_default_connect_timeout_emitted(monkeypatch):
    monkeypatch.delenv("HPC_SSH_CONNECT_TIMEOUT", raising=False)
    assert ssh_options._ssh_connect_opts() == [
        "-o",
        f"ConnectTimeout={ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT}",
    ]


@pytest.mark.parametrize("kind", ["ssh", "scp"])
def test_ssh_argv_splices_connect_timeout(monkeypatch, kind):
    monkeypatch.delenv("HPC_SSH_CONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    argv = ssh_options.ssh_argv(kind)
    bound = f"ConnectTimeout={ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT}"
    assert bound in argv
    # BatchMode still leads; the connect bound rides right behind it.
    assert argv.index("BatchMode=yes") < argv.index(bound)


def test_env_override_honoured(monkeypatch):
    monkeypatch.setenv("HPC_SSH_CONNECT_TIMEOUT", "7")
    assert ssh_options._ssh_connect_opts() == ["-o", "ConnectTimeout=7"]


def test_default_token_drops_override(monkeypatch):
    monkeypatch.setenv("HPC_SSH_CONNECT_TIMEOUT", "default")
    assert ssh_options._ssh_connect_opts() == []


def test_whitespace_is_tolerated(monkeypatch):
    monkeypatch.setenv("HPC_SSH_CONNECT_TIMEOUT", "  20  ")
    assert ssh_options._ssh_connect_opts() == ["-o", "ConnectTimeout=20"]


@pytest.mark.parametrize("bad", ["0", "-3", "12s", "abc"])
def test_invalid_value_warns_and_falls_back(monkeypatch, capsys, bad):
    monkeypatch.setenv("HPC_SSH_CONNECT_TIMEOUT", bad)
    opts = ssh_options._ssh_connect_opts()
    assert opts == ["-o", f"ConnectTimeout={ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT}"]
    assert "HPC_SSH_CONNECT_TIMEOUT" in capsys.readouterr().err


def test_empty_value_falls_back_silently(monkeypatch, capsys):
    # An empty string is the `or default` path, not a typo — no warning.
    monkeypatch.setenv("HPC_SSH_CONNECT_TIMEOUT", "")
    assert ssh_options._ssh_connect_opts() == [
        "-o",
        f"ConnectTimeout={ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT}",
    ]
    assert capsys.readouterr().err == ""


def test_rsync_rsh_env_carries_connect_timeout(monkeypatch):
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    monkeypatch.delenv("HPC_SSH_CONNECT_TIMEOUT", raising=False)
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.setattr(ssh_options, "_ssh_binary", lambda: "ssh")
    env = ssh_options._rsync_rsh_env()
    assert "RSYNC_RSH" in env
    assert f"ConnectTimeout={ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT}" in env["RSYNC_RSH"]
