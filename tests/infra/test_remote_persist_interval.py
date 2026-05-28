"""Tests for the ``HPC_SSH_PERSIST_INTERVAL`` env-var override.

The default ControlPersist window (10m) is too short for long-running
monitor loops on flaky clusters — the master socket drops between polls
and the next call pays a fresh handshake. Operators can override the
window via the env var; these tests pin the contract documented on
``_ssh_multiplex_opts``.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _ensure_multiplex_enabled(monkeypatch):
    """Clear ``HPC_NO_SSH_MULTIPLEX`` so each test sees the multiplex branch."""
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    # Start each test with a clean slate for the var under test, so tests
    # that want the default don't pick up another test's override.
    monkeypatch.delenv("HPC_SSH_PERSIST_INTERVAL", raising=False)
    # Force a non-Windows platform so the Windows auto-disable short-circuit
    # (added for ssh.exe Unix-socket incompatibility) doesn't swallow the
    # branch under test when this suite runs on Windows.
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")


def _persist_values(opts: list[str]) -> list[str]:
    """Return every ``ControlPersist=...`` value present in *opts*."""
    return [
        opt.split("=", 1)[1]
        for opt in opts
        if isinstance(opt, str) and opt.startswith("ControlPersist=")
    ]


class TestPersistIntervalDefault:
    def test_default_is_10m(self):
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["10m"]

    def test_empty_env_var_falls_back_to_default(self, monkeypatch):
        # Empty string should be treated as "not set" rather than as a
        # literal empty ControlPersist value, which OpenSSH would reject.
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["10m"]


class TestPersistIntervalOverride:
    def test_long_window_passes_through(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "30m")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["30m"]

    def test_hours_suffix_passes_through(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "2h")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["2h"]

    def test_zero_means_persist_until_master_exits(self, monkeypatch):
        # ``0`` is a valid ssh_config value meaning "do not auto-exit"; we
        # pass it through verbatim.
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "0")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["0"]


class TestPersistIntervalNo:
    def test_no_drops_control_persist_option(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "no")
        opts = ssh_options._ssh_multiplex_opts()
        # No ControlPersist option should be emitted at all.
        assert _persist_values(opts) == []
        # ControlMaster/ControlPath are still in place — multiplexing
        # itself is still enabled, just without an explicit persist window.
        assert "ControlMaster=auto" in opts
        assert any(o.startswith("ControlPath=") for o in opts)

    def test_no_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "NO")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == []


class TestPersistIntervalRejection:
    def test_semicolon_falls_back_to_default(self, monkeypatch, capsys):
        # Shell metachar: must not flow into argv. Documented behaviour
        # is to log to stderr and fall back to the safe default.
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "30m;rm -rf /")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["10m"]
        captured = capsys.readouterr()
        assert "HPC_SSH_PERSIST_INTERVAL" in captured.err
        # The tainted value must never appear in the emitted argv.
        assert not any("rm -rf" in o for o in opts)
        assert not any(";" in o for o in opts if o.startswith("ControlPersist="))

    def test_whitespace_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "30 m")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["10m"]
        assert "HPC_SSH_PERSIST_INTERVAL" in capsys.readouterr().err

    def test_backtick_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "`whoami`")
        opts = ssh_options._ssh_multiplex_opts()
        assert _persist_values(opts) == ["10m"]
        assert "HPC_SSH_PERSIST_INTERVAL" in capsys.readouterr().err


class TestMultiplexFullyDisabledStillWins:
    def test_no_multiplex_overrides_persist_interval(self, monkeypatch):
        # Setting both: HPC_NO_SSH_MULTIPLEX=1 should still produce an
        # empty opts list, ignoring any persist interval value.
        monkeypatch.setenv("HPC_NO_SSH_MULTIPLEX", "1")
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "2h")
        assert ssh_options._ssh_multiplex_opts() == []
