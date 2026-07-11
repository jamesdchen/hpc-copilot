"""Keepalives spliced into ssh-family calls (run-12 finding 24).

Several framework remote legs are silent on the wire for many minutes (the
status reporter's per-task walk, a combiner reduce). A NAT'd client drops an
idle TCP flow at its idle threshold — observed live 2026-07-11 at ~100s: the
channel died mid-reporter with empty stdout while the remote half ground on as
an orphan. ``_ssh_keepalive_opts`` pins protocol-level keepalives so the NAT
state stays alive regardless of application silence, framework-owned rather
than delegated to the user's ssh_config (which is how the live failure
happened).
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _modern(monkeypatch):
    """Pin a modern local OpenSSH so the version probe never shells out (the
    crypto opts share ssh_argv with the keepalive opts)."""
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
    ssh_options._local_openssh_supports_gcm.cache_clear()
    yield
    ssh_options._local_openssh_supports_gcm.cache_clear()


def test_default_keepalive_emitted(monkeypatch):
    monkeypatch.delenv("HPC_SSH_KEEPALIVE_INTERVAL", raising=False)
    assert ssh_options._ssh_keepalive_opts() == [
        "-o",
        f"ServerAliveInterval={ssh_options._DEFAULT_SSH_KEEPALIVE_INTERVAL}",
        "-o",
        f"ServerAliveCountMax={ssh_options._DEFAULT_SSH_KEEPALIVE_COUNT_MAX}",
    ]


@pytest.mark.parametrize("kind", ["ssh", "scp"])
def test_ssh_argv_splices_keepalives(monkeypatch, kind):
    monkeypatch.delenv("HPC_SSH_KEEPALIVE_INTERVAL", raising=False)
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    argv = ssh_options.ssh_argv(kind)
    joined = " ".join(argv)
    assert "ServerAliveInterval=30" in joined
    assert "ServerAliveCountMax=60" in joined


def test_literal_default_drops_the_override(monkeypatch):
    monkeypatch.setenv("HPC_SSH_KEEPALIVE_INTERVAL", "default")
    assert ssh_options._ssh_keepalive_opts() == []


def test_custom_interval_respected(monkeypatch):
    monkeypatch.setenv("HPC_SSH_KEEPALIVE_INTERVAL", "10")
    opts = ssh_options._ssh_keepalive_opts()
    assert "ServerAliveInterval=10" in " ".join(opts)


def test_bad_value_warns_and_falls_back(monkeypatch, capsys):
    monkeypatch.setenv("HPC_SSH_KEEPALIVE_INTERVAL", "-5")
    opts = ssh_options._ssh_keepalive_opts()
    assert f"ServerAliveInterval={ssh_options._DEFAULT_SSH_KEEPALIVE_INTERVAL}" in " ".join(opts)
    assert "ignoring HPC_SSH_KEEPALIVE_INTERVAL" in capsys.readouterr().err
