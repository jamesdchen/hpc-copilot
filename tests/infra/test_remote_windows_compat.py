"""Windows-compat regression tests for :func:`_ssh_multiplex_opts`.

Native Windows OpenSSH does not support the ``ControlPath`` Unix socket
that connection multiplexing relies on — ``ssh.exe`` aborts with
``getsockname failed: Not a socket`` the moment ``ControlMaster`` is in
effect. On ``win32`` we therefore emit an explicit ``ControlMaster=no`` /
``ControlPath=none`` override rather than just omitting our own flags:
omitting them would still leave a user's ``~/.ssh/config`` ``ControlMaster``
to bite. ``HPC_NO_SSH_MULTIPLEX=1`` still short-circuits to ``[]`` first.

The tempfile test pins the ``/tmp`` fallback fix: when ``XDG_RUNTIME_DIR``
is unset, the ``ControlPath`` must derive from :func:`tempfile.gettempdir`
rather than a hardcoded ``/tmp``.
"""

from __future__ import annotations

import tempfile

import pytest

from hpc_agent.infra import ssh_options


@pytest.fixture(autouse=True)
def _ensure_multiplex_enabled(monkeypatch):
    """Clear ``HPC_NO_SSH_MULTIPLEX`` so each test sees the multiplex branch."""
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    monkeypatch.delenv("HPC_SSH_PERSIST_INTERVAL", raising=False)


def _control_path_values(opts: list[str]) -> list[str]:
    """Return every ``ControlPath=...`` value present in *opts*."""
    return [
        opt.split("=", 1)[1]
        for opt in opts
        if isinstance(opt, str) and opt.startswith("ControlPath=")
    ]


def test_ssh_multiplex_override_on_windows(monkeypatch):
    # On win32 the function must emit an explicit ControlMaster=no /
    # ControlPath=none override. Returning [] would only omit OUR flags; a
    # user's ~/.ssh/config ControlMaster would still drive ssh.exe into the
    # ``getsockname failed: Not a socket`` failure. A command-line -o beats
    # the config file, so the override neutralises it.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    assert ssh_options._ssh_multiplex_opts() == [
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]


def test_ssh_multiplex_env_optout_wins_on_windows(monkeypatch):
    # HPC_NO_SSH_MULTIPLEX=1 is checked before the platform branch, so the
    # explicit opt-out still yields [] even on win32.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setenv("HPC_NO_SSH_MULTIPLEX", "1")
    assert ssh_options._ssh_multiplex_opts() == []


def test_ssh_multiplex_uses_tempfile_fallback(monkeypatch):
    # When XDG_RUNTIME_DIR is unset on a non-Windows platform, the
    # ControlPath must come from tempfile.gettempdir(), not a hardcoded
    # /tmp literal — covers locked-down /tmp and non-Linux Unixes.
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    opts = ssh_options._ssh_multiplex_opts()
    paths = _control_path_values(opts)
    assert len(paths) == 1
    expected_prefix = f"{tempfile.gettempdir()}/hpc-cm-"
    assert paths[0].startswith(expected_prefix), (
        f"ControlPath {paths[0]!r} should start with {expected_prefix!r}"
    )


# ---------------------------------------------------------------------------
# ssh/scp/rsync binary resolution (Git Bash vs native Windows OpenSSH)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_binary_overrides(monkeypatch):
    """Each binary-resolution test starts from a clean env."""
    for var in ("HPC_SSH_BINARY", "HPC_SCP_BINARY", "HPC_SSH_ADD_BINARY", "RSYNC_RSH"):
        monkeypatch.delenv(var, raising=False)


def test_ssh_binary_defaults_to_bare_name_on_posix(monkeypatch):
    # On Linux/macOS we must keep bare PATH resolution — no behaviour change.
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    assert ssh_options._ssh_binary() == "ssh"
    assert ssh_options._scp_binary() == "scp"
    assert ssh_options._rsync_rsh_env() == {}


def test_explicit_override_wins_on_any_platform(monkeypatch):
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.setenv("HPC_SSH_BINARY", "/opt/openssh/bin/ssh")
    monkeypatch.setenv("HPC_SCP_BINARY", "/opt/openssh/bin/scp")
    assert ssh_options._ssh_binary() == "/opt/openssh/bin/ssh"
    assert ssh_options._scp_binary() == "/opt/openssh/bin/scp"
    # A non-default ssh binary propagates to rsync's remote shell.
    assert ssh_options._rsync_rsh_env() == {"RSYNC_RSH": "/opt/openssh/bin/ssh"}


def test_windows_prefers_native_openssh_when_present(monkeypatch):
    # On win32, when the native binary exists, prefer it over Git Bash's
    # ssh so the resolved ssh can reach the named-pipe agent.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: True)
    assert ssh_options._ssh_binary() == ssh_options._WIN_OPENSSH_SSH
    assert ssh_options._scp_binary() == ssh_options._WIN_OPENSSH_SCP
    assert ssh_options._ssh_add_binary() == ssh_options._WIN_OPENSSH_SSH_ADD
    # On Windows the RSYNC_RSH command carries the multiplex override too,
    # so rsync's own ssh can't pick up the user's ssh-config ControlMaster.
    assert ssh_options._rsync_rsh_env() == {
        "RSYNC_RSH": f"{ssh_options._WIN_OPENSSH_SSH} -o ControlMaster=no -o ControlPath=none"
    }


def test_windows_falls_back_to_path_when_native_absent(monkeypatch):
    # If the native binary isn't installed, fall back to bare PATH name
    # rather than pointing at a non-existent absolute path.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: False)
    assert ssh_options._ssh_binary() == "ssh"
    assert ssh_options._scp_binary() == "scp"
    assert ssh_options._ssh_add_binary() == "ssh-add"
    # Even with the bare name, Windows rsync still gets the multiplex
    # override so it can't honour the user's ssh-config ControlMaster.
    assert ssh_options._rsync_rsh_env() == {
        "RSYNC_RSH": "ssh -o ControlMaster=no -o ControlPath=none"
    }


def test_rsync_rsh_respects_caller_set_value(monkeypatch):
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: True)
    monkeypatch.setenv("RSYNC_RSH", "ssh -v")
    # A caller who already pinned RSYNC_RSH wins; we don't clobber it.
    assert ssh_options._rsync_rsh_env() == {}


def test_ssh_add_binary_defaults_to_bare_name_on_posix(monkeypatch):
    # On Linux/macOS, bare PATH resolution — no behaviour change.
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    assert ssh_options._ssh_add_binary() == "ssh-add"


def test_ssh_add_binary_override_wins_on_any_platform(monkeypatch):
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.setenv("HPC_SSH_ADD_BINARY", "/opt/openssh/bin/ssh-add")
    assert ssh_options._ssh_add_binary() == "/opt/openssh/bin/ssh-add"


def test_ssh_add_binary_prefers_native_openssh_on_windows(monkeypatch):
    # The ssh-add analog of native ssh/scp resolution: reach the
    # named-pipe agent instead of Git Bash's /usr/bin/ssh-add.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: True)
    assert ssh_options._ssh_add_binary() == ssh_options._WIN_OPENSSH_SSH_ADD


# ---------------------------------------------------------------------------
# HPC_SSH_NAMED_PIPE opt-in: named-pipe ControlPath on Windows OpenSSH ≥ 8.x
# ---------------------------------------------------------------------------


class TestSshNamedPipeOptIn:
    """Pin the ``HPC_SSH_NAMED_PIPE=1`` opt-in contract.

    OpenSSH ≥ 8.x on native Windows accepts a ``\\\\.\\pipe\\<name>``
    ControlPath (named-pipe transport, the Win32 equivalent of a Unix
    domain socket). With the env opt-in set, :func:`_ssh_multiplex_opts`
    must emit real ``ControlMaster=auto`` + named-pipe ``ControlPath``;
    without it, the legacy ``ControlMaster=no``/``ControlPath=none``
    shape is preserved; and ``HPC_NO_SSH_MULTIPLEX=1`` still wins.
    """

    def test_named_pipe_opt_in_enables_multiplex_on_windows(self, monkeypatch):
        # Opt-in set, multiplex not disabled → real multiplexing on Windows
        # via a named-pipe ControlPath.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "1")
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts
        paths = _control_path_values(opts)
        assert len(paths) == 1
        # The Windows named-pipe namespace literally starts with ``\\.\pipe\``.
        assert paths[0].startswith(r"\\.\pipe" + "\\")
        # Be precise: full documented path with the %C token that OpenSSH
        # substitutes (connection-tuple hash) at runtime.
        assert paths[0] == r"\\.\pipe\openssh-hpc-cm-%C"

    def test_named_pipe_opt_in_yields_to_no_multiplex(self, monkeypatch):
        # Both env vars set: the disable short-circuit wins. Returning [] is
        # the documented behaviour — HPC_NO_SSH_MULTIPLEX=1 is the master switch.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "1")
        monkeypatch.setenv("HPC_NO_SSH_MULTIPLEX", "1")
        assert ssh_options._ssh_multiplex_opts() == []

    def test_named_pipe_opt_in_unset_preserves_legacy_windows_shape(self, monkeypatch):
        # Regression guard: with the opt-in OFF, the legacy
        # ControlMaster=no / ControlPath=none override must still ship on win32,
        # so a user's ~/.ssh/config ControlMaster can't drive ssh.exe into the
        # getsockname-failure path.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.delenv("HPC_SSH_NAMED_PIPE", raising=False)
        assert ssh_options._ssh_multiplex_opts() == [
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
        ]

    def test_named_pipe_opt_in_is_windows_only(self, monkeypatch):
        # On POSIX the opt-in is a no-op: the Unix-socket ControlPath under
        # XDG_RUNTIME_DIR / tempfile.gettempdir() is what works there.
        monkeypatch.setattr(ssh_options.sys, "platform", "linux")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "1")
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts
        paths = _control_path_values(opts)
        assert len(paths) == 1
        # Must be the POSIX Unix-socket shape, never the named-pipe shape.
        assert not paths[0].startswith(r"\\.\pipe")
        assert paths[0].startswith(f"{tempfile.gettempdir()}/hpc-cm-")

    def test_named_pipe_opt_in_does_not_affect_transfer_override(self, monkeypatch):
        # The transfer-sized helper (used by scp / tar-fallback push) is
        # deliberately NOT toggled by HPC_SSH_NAMED_PIPE: one-shot transfers
        # don't benefit from being a multiplex client.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "1")
        assert ssh_options._ssh_config_override_opts() == [
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
        ]

    def test_named_pipe_opt_in_honours_persist_interval(self, monkeypatch):
        # The opt-in path must still respect HPC_SSH_PERSIST_INTERVAL exactly
        # like the POSIX branch — same _resolve_ssh_persist_interval() helper.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "1")
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "30m")
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlPersist=30m" in opts
