"""Cipher / MAC / compression tuning spliced into ssh-family calls (#256)."""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset the cached version probe + clear the crypto env knobs per test.

    ``_local_openssh_supports_gcm`` is ``@functools.cache``d; a test that
    monkeypatches the version probe must not leak its verdict to the next.
    """
    ssh_options._local_openssh_supports_gcm.cache_clear()
    yield
    ssh_options._local_openssh_supports_gcm.cache_clear()


def _force_modern(monkeypatch):
    """Pin the local-OpenSSH probe to a modern (gcm-capable) major."""
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
    ssh_options._local_openssh_supports_gcm.cache_clear()


def test_default_opts_include_gcm_ciphers_and_etm_macs(monkeypatch):
    for var in ("HPC_SSH_CIPHER", "HPC_SSH_MAC", "HPC_SSH_COMPRESSION"):
        monkeypatch.delenv(var, raising=False)
    _force_modern(monkeypatch)

    opts = ssh_options._ssh_crypto_opts()
    joined = " ".join(opts)

    assert f"Ciphers={ssh_options._DEFAULT_SSH_CIPHERS}" in joined
    assert "aes128-gcm@openssh.com" in joined
    assert f"MACs={ssh_options._DEFAULT_SSH_MACS}" in joined
    assert "umac-128-etm@openssh.com" in joined
    assert "Compression=no" in joined


@pytest.mark.parametrize("kind", ["ssh", "scp"])
def test_ssh_argv_splices_crypto_opts(monkeypatch, kind):
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    _force_modern(monkeypatch)
    argv = ssh_options.ssh_argv(kind)
    assert "Ciphers=aes128-gcm@openssh.com,aes256-gcm@openssh.com" in argv
    assert "Compression=no" in argv
    # BatchMode still leads; crypto opts come after it.
    assert argv.index("BatchMode=yes") < argv.index(
        "Ciphers=aes128-gcm@openssh.com,aes256-gcm@openssh.com"
    )


def test_cipher_default_token_drops_override(monkeypatch):
    monkeypatch.setenv("HPC_SSH_CIPHER", "default")
    monkeypatch.delenv("HPC_SSH_MAC", raising=False)
    monkeypatch.delenv("HPC_SSH_COMPRESSION", raising=False)
    _force_modern(monkeypatch)
    opts = ssh_options._ssh_crypto_opts()
    assert not any(o.startswith("Ciphers=") for o in opts)
    # MAC + compression overrides still present.
    assert any(o.startswith("MACs=") for o in opts)
    assert "Compression=no" in opts


def test_env_overrides_are_honoured(monkeypatch):
    monkeypatch.setenv("HPC_SSH_CIPHER", "aes256-ctr")
    monkeypatch.setenv("HPC_SSH_MAC", "hmac-sha1")
    monkeypatch.setenv("HPC_SSH_COMPRESSION", "yes")
    _force_modern(monkeypatch)
    opts = ssh_options._ssh_crypto_opts()
    assert "Ciphers=aes256-ctr" in opts
    assert "MACs=hmac-sha1" in opts
    assert "Compression=yes" in opts


def test_old_local_openssh_drops_default_cipher_and_mac(monkeypatch):
    for var in ("HPC_SSH_CIPHER", "HPC_SSH_MAC", "HPC_SSH_COMPRESSION"):
        monkeypatch.delenv(var, raising=False)
    # Positively-detected OpenSSH 5.x — pre-dates universal aes-gcm / ETM.
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 5)
    ssh_options._local_openssh_supports_gcm.cache_clear()

    opts = ssh_options._ssh_crypto_opts()
    assert not any(o.startswith("Ciphers=") for o in opts)
    assert not any(o.startswith("MACs=") for o in opts)
    # Compression is universally supported, so it is still pinned.
    assert "Compression=no" in opts


def test_old_local_openssh_still_honours_explicit_cipher(monkeypatch):
    # An explicit override beats the version probe — the operator pinned it.
    monkeypatch.setenv("HPC_SSH_CIPHER", "aes256-ctr")
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 5)
    ssh_options._local_openssh_supports_gcm.cache_clear()
    opts = ssh_options._ssh_crypto_opts()
    assert "Ciphers=aes256-ctr" in opts


def test_undeterminable_version_keeps_default(monkeypatch):
    for var in ("HPC_SSH_CIPHER", "HPC_SSH_MAC", "HPC_SSH_COMPRESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: None)
    ssh_options._local_openssh_supports_gcm.cache_clear()
    opts = ssh_options._ssh_crypto_opts()
    assert any(o.startswith("Ciphers=") for o in opts)


def test_rsync_rsh_env_carries_crypto_opts(monkeypatch):
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.setattr(ssh_options, "_ssh_binary", lambda: "ssh")
    _force_modern(monkeypatch)
    env = ssh_options._rsync_rsh_env()
    # Pre-#256 this was {} on POSIX; now it pins the cipher via rsync's ssh.
    assert "RSYNC_RSH" in env
    assert env["RSYNC_RSH"].startswith("ssh ")
    assert "Ciphers=aes128-gcm@openssh.com,aes256-gcm@openssh.com" in env["RSYNC_RSH"]


def test_rsync_rsh_env_respects_caller_override(monkeypatch):
    monkeypatch.setenv("RSYNC_RSH", "ssh -p 2222")
    assert ssh_options._rsync_rsh_env() == {}
