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

import shlex
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from hpc_agent.infra import remote, ssh_options


@pytest.fixture(autouse=True)
def _ensure_multiplex_enabled(monkeypatch):
    """Clear the multiplex env vars so each test sees the default branch.

    Also resets the cached OpenSSH-version probe so a test that monkeypatches
    :func:`_local_openssh_major` is not shadowed by a prior test's cached
    verdict.
    """
    monkeypatch.delenv("HPC_NO_SSH_MULTIPLEX", raising=False)
    monkeypatch.delenv("HPC_SSH_PERSIST_INTERVAL", raising=False)
    monkeypatch.delenv("HPC_SSH_NAMED_PIPE", raising=False)
    ssh_options._windows_openssh_named_pipe_supported.cache_clear()
    ssh_options._ssh_config_forces_no_multiplex.cache_clear()
    # Reset the runtime verdict so a test that calls mark_named_pipe_broken()
    # doesn't leak its False verdict into the next test's _ssh_multiplex_opts()
    # branch — the verdict is a module-level mutable.
    ssh_options.reset_named_pipe_runtime_verdict()
    # Default: no ~/.ssh/config in play, so the #243 probe never fires unless a
    # test opts in by stubbing this. Keeps tests off the runner's real home.
    monkeypatch.setattr(ssh_options, "_read_ssh_config_text", lambda: None)
    yield
    ssh_options._windows_openssh_named_pipe_supported.cache_clear()
    ssh_options._ssh_config_forces_no_multiplex.cache_clear()
    ssh_options.reset_named_pipe_runtime_verdict()


def _control_path_values(opts: list[str]) -> list[str]:
    """Return every ``ControlPath=...`` value present in *opts*."""
    return [
        opt.split("=", 1)[1]
        for opt in opts
        if isinstance(opt, str) and opt.startswith("ControlPath=")
    ]


def test_ssh_multiplex_default_named_pipe_on_windows(monkeypatch):
    # The default on win32 (0.10.6+) is named-pipe multiplexing, not the
    # legacy override — provided the local OpenSSH is ≥ 8.x (probed). With no
    # env vars set, the function must emit ControlMaster=auto + a named-pipe
    # ControlPath.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 8)
    opts = ssh_options._ssh_multiplex_opts()
    assert "ControlMaster=auto" in opts
    paths = _control_path_values(opts)
    assert paths == [r"\\.\pipe\openssh-hpc-cm-%C"]


def test_ssh_multiplex_opt_out_restores_legacy_override_on_windows(monkeypatch):
    # HPC_SSH_NAMED_PIPE=0 opts back out to the legacy ControlMaster=no /
    # ControlPath=none override. Returning [] would only omit OUR flags; a
    # user's ~/.ssh/config ControlMaster would still drive ssh.exe into the
    # ``getsockname failed: Not a socket`` failure. A command-line -o beats
    # the config file, so the override neutralises it.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "0")
    assert ssh_options._ssh_multiplex_opts() == [
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]


def test_ssh_multiplex_falls_back_when_openssh_too_old(monkeypatch, capsys):
    # A positively-detected < 8.x local OpenSSH demotes to the legacy override
    # and warns once — named-pipe ControlPath needs 8.x.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 7)
    assert ssh_options._ssh_multiplex_opts() == [
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]
    warning = capsys.readouterr().err
    assert "older than 8" in warning


def test_ssh_multiplex_keeps_default_when_version_undeterminable(monkeypatch):
    # A probe that can't read the version (None) must NOT demote the default:
    # OpenSSH < 8 is rare on the Windows builds that ship the native binary.
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: None)
    opts = ssh_options._ssh_multiplex_opts()
    assert _control_path_values(opts) == [r"\\.\pipe\openssh-hpc-cm-%C"]


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
    """Each binary-resolution test starts from a clean env.

    The cipher/MAC/compression tuning (#256) and the ConnectTimeout bound are
    pinned to ``default`` here so these binary-resolution + Windows-multiplex
    assertions stay focused on the RSYNC_RSH *binary/override* shape and aren't
    coupled to those opts, which have their own coverage in
    ``test_ssh_options_cipher.py`` / ``test_ssh_options_connect_timeout.py``.
    """
    for var in ("HPC_SSH_BINARY", "HPC_SCP_BINARY", "HPC_SSH_ADD_BINARY", "RSYNC_RSH"):
        monkeypatch.delenv(var, raising=False)
    for var in ("HPC_SSH_CIPHER", "HPC_SSH_MAC", "HPC_SSH_COMPRESSION", "HPC_SSH_CONNECT_TIMEOUT"):
        monkeypatch.setenv(var, "default")


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


# ---------------------------------------------------------------------------
# #64: RSYNC_RSH is whitespace-tokenized by rsync — a spaced binary path
# (Program Files) must stay one shell word, or rsync execs ``C:\Program``.
# The RSH env is the transport entry point the win32 operand/quoting matrix
# (push src, pull dst, deploy staging via ``_msys_local``) had not yet covered.
# ---------------------------------------------------------------------------


def test_rsync_rsh_quotes_spaced_binary_on_windows(monkeypatch):
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    # Pin a Program Files ssh.exe (spaces are the norm there) and make the
    # native-OpenSSH probe see it as present so _ssh_binary returns it verbatim.
    spaced = r"C:\Program Files\Git\usr\bin\ssh.exe"
    monkeypatch.setenv("HPC_SSH_BINARY", spaced)
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: True)
    env = ssh_options._rsync_rsh_env()
    rsh = env["RSYNC_RSH"]
    # The binary is one double-quoted token; the split keeps it whole while the
    # option flags that follow remain separately splittable.
    assert rsh.startswith(f'"{spaced}" ')
    assert shlex.split(rsh)[0] == spaced


def test_rsync_rsh_leaves_unspaced_binary_unquoted(monkeypatch):
    # The common case (bare ``ssh`` or an unspaced native path) keeps the exact
    # pre-fix bytes — no gratuitous quoting.
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    monkeypatch.delenv("HPC_SSH_BINARY", raising=False)
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")
    monkeypatch.setattr(ssh_options.os.path, "isfile", lambda p: False)
    rsh = ssh_options._rsync_rsh_env()["RSYNC_RSH"]
    assert rsh.startswith("ssh ")
    assert '"' not in rsh


def test_rsync_rsh_quotes_spaced_binary_posix_shlex(monkeypatch):
    # On POSIX the same hazard exists (rsync word-splits RSYNC_RSH); shlex.quote
    # is the canonical shell-safe form.
    monkeypatch.delenv("RSYNC_RSH", raising=False)
    monkeypatch.setattr(ssh_options.sys, "platform", "linux")
    monkeypatch.setenv("HPC_SSH_BINARY", "/opt/my ssh/ssh")
    rsh = ssh_options._rsync_rsh_env()["RSYNC_RSH"]
    assert shlex.split(rsh)[0] == "/opt/my ssh/ssh"


# ---------------------------------------------------------------------------
# #65: Path.home() raises RuntimeError (not OSError) in a stripped Windows env
# (USERPROFILE/HOMEDRIVE+HOMEPATH/USERNAME all unset). The config probe must
# fail open (no override) instead of taking down every ssh_argv call.
# ---------------------------------------------------------------------------


def test_read_ssh_config_text_fails_open_on_home_runtimeerror(monkeypatch):
    def _boom():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(ssh_options.Path, "home", staticmethod(_boom))
    assert ssh_options._read_ssh_config_text() is None


def test_config_forces_no_multiplex_fails_open_on_home_runtimeerror(monkeypatch):
    # The win32-only probe consuming _read_ssh_config_text must not crash the
    # transport when home is unresolvable — it degrades to "no override".
    monkeypatch.setattr(ssh_options.sys, "platform", "win32")

    def _boom():
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(ssh_options.Path, "home", staticmethod(_boom))
    assert ssh_options._ssh_config_forces_no_multiplex() is False


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


class TestSshNamedPipeDefault:
    """Pin the named-pipe ``ControlPath`` default contract (0.10.6+).

    OpenSSH ≥ 8.x on native Windows accepts a ``\\\\.\\pipe\\<name>``
    ControlPath (named-pipe transport, the Win32 equivalent of a Unix
    domain socket). By default :func:`_ssh_multiplex_opts` emits real
    ``ControlMaster=auto`` + named-pipe ``ControlPath`` on Windows;
    ``HPC_SSH_NAMED_PIPE=0`` opts back out to the legacy
    ``ControlMaster=no``/``ControlPath=none`` shape; and
    ``HPC_NO_SSH_MULTIPLEX=1`` still wins over everything.
    """

    def test_default_enables_named_pipe_multiplex_on_windows(self, monkeypatch):
        # No env vars, OpenSSH ≥ 8.x → real multiplexing on Windows via a
        # named-pipe ControlPath (the new default).
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts
        paths = _control_path_values(opts)
        assert len(paths) == 1
        # The Windows named-pipe namespace literally starts with ``\\.\pipe\``.
        assert paths[0].startswith(r"\\.\pipe" + "\\")
        # Be precise: full documented path with the %C token that OpenSSH
        # substitutes (connection-tuple hash) at runtime.
        assert paths[0] == r"\\.\pipe\openssh-hpc-cm-%C"

    def test_no_multiplex_wins_over_named_pipe_default(self, monkeypatch):
        # HPC_NO_SSH_MULTIPLEX=1 short-circuits ahead of the platform branch.
        # Returning [] is the documented master-switch behaviour.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_NO_SSH_MULTIPLEX", "1")
        assert ssh_options._ssh_multiplex_opts() == []

    def test_opt_out_restores_legacy_windows_shape(self, monkeypatch):
        # HPC_SSH_NAMED_PIPE=0 restores the legacy ControlMaster=no /
        # ControlPath=none override on win32, so a user's ~/.ssh/config
        # ControlMaster can't drive ssh.exe into the getsockname-failure path.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setenv("HPC_SSH_NAMED_PIPE", "0")
        assert ssh_options._ssh_multiplex_opts() == [
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
        ]

    def test_named_pipe_default_is_windows_only(self, monkeypatch):
        # On POSIX the Windows default is irrelevant: the Unix-socket
        # ControlPath under XDG_RUNTIME_DIR / tempfile.gettempdir() is used.
        monkeypatch.setattr(ssh_options.sys, "platform", "linux")
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts
        paths = _control_path_values(opts)
        assert len(paths) == 1
        # Must be the POSIX Unix-socket shape, never the named-pipe shape.
        assert not paths[0].startswith(r"\\.\pipe")
        assert paths[0].startswith(f"{tempfile.gettempdir()}/hpc-cm-")

    def test_named_pipe_default_does_not_affect_transfer_override(self, monkeypatch):
        # The transfer-sized helper (used by scp / tar-fallback push / rsync)
        # is deliberately NOT swept up by the named-pipe default: one-shot
        # transfers don't benefit from being a multiplex client.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        assert ssh_options._ssh_config_override_opts() == [
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
        ]

    def test_named_pipe_default_honours_persist_interval(self, monkeypatch):
        # The named-pipe path must still respect HPC_SSH_PERSIST_INTERVAL
        # exactly like the POSIX branch — same _resolve_ssh_persist_interval().
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        monkeypatch.setenv("HPC_SSH_PERSIST_INTERVAL", "30m")
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlPersist=30m" in opts


class TestLocalOpenSshProbe:
    """The ``ssh -V`` version probe behind the named-pipe fallback."""

    def test_parses_posix_version_string(self, monkeypatch):
        monkeypatch.setattr(
            ssh_options.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="", stderr="OpenSSH_8.9p1, OpenSSL 3.0\n"),
        )
        assert ssh_options._local_openssh_major() == 8

    def test_parses_windows_version_string(self, monkeypatch):
        monkeypatch.setattr(
            ssh_options.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(
                stdout="", stderr="OpenSSH_for_Windows_8.6p1, LibreSSL 3.3.3\n"
            ),
        )
        assert ssh_options._local_openssh_major() == 8

    def test_returns_none_when_probe_cannot_run(self, monkeypatch):
        def _boom(*_a, **_k):
            raise FileNotFoundError("no ssh")

        monkeypatch.setattr(ssh_options.subprocess, "run", _boom)
        assert ssh_options._local_openssh_major() is None

    def test_returns_none_on_unparseable_output(self, monkeypatch):
        monkeypatch.setattr(
            ssh_options.subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="", stderr="garbage with no version\n"),
        )
        assert ssh_options._local_openssh_major() is None


_UNIX_SOCKET_GLOBAL_CONFIG = """
Host *
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
"""

_NAMED_PIPE_GLOBAL_CONFIG = r"""
Host *
    ControlMaster auto
    ControlPath \\.\pipe\openssh-cm-%r@%h:%p
    ControlPersist 10m
"""

_SCOPED_UNIX_SOCKET_CONFIG = """
Host hoffman2 discovery
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
"""


class TestSshConfigDeclaresProblem:
    """The pure scanner over ~/.ssh/config text (no platform / IO)."""

    def test_detects_global_unix_socket_master(self):
        assert ssh_options._ssh_config_declares_unix_socket_global_master(
            _UNIX_SOCKET_GLOBAL_CONFIG
        )

    def test_named_pipe_global_is_fine(self):
        assert not ssh_options._ssh_config_declares_unix_socket_global_master(
            _NAMED_PIPE_GLOBAL_CONFIG
        )

    def test_scoped_host_block_is_not_global(self):
        # The breakage the probe guards is specifically a `Host *` default; a
        # host-scoped block is the recommended fix shape, not a trigger.
        assert not ssh_options._ssh_config_declares_unix_socket_global_master(
            _SCOPED_UNIX_SOCKET_CONFIG
        )

    def test_controlmaster_no_is_not_a_problem(self):
        assert not ssh_options._ssh_config_declares_unix_socket_global_master(
            "Host *\n    ControlMaster no\n    ControlPath ~/.ssh/cm-%r\n"
        )

    def test_controlpath_none_is_fine(self):
        assert not ssh_options._ssh_config_declares_unix_socket_global_master(
            "Host *\n    ControlMaster auto\n    ControlPath none\n"
        )

    def test_equals_syntax_and_case_insensitivity(self):
        assert ssh_options._ssh_config_declares_unix_socket_global_master(
            "host=*\n  CONTROLMASTER=auto\n  controlpath=~/.ssh/cm\n"
        )


class TestSshConfigForcesNoMultiplex:
    """The cached, Windows-only probe that wires the scanner into the gate."""

    def test_problematic_config_disables_multiplex_and_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(
            ssh_options, "_read_ssh_config_text", lambda: _UNIX_SOCKET_GLOBAL_CONFIG
        )
        # Even with OpenSSH ≥ 8.x (named pipe otherwise available), a broken
        # global config forces the no-flags fallback.
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        assert ssh_options._ssh_multiplex_opts() == []
        # The transfer path is forced off too (same HPC_NO_SSH_MULTIPLEX shape).
        assert ssh_options._ssh_config_override_opts() == []
        assert "getsockname" in capsys.readouterr().err

    def test_warns_only_once(self, monkeypatch, capsys):
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(
            ssh_options, "_read_ssh_config_text", lambda: _UNIX_SOCKET_GLOBAL_CONFIG
        )
        ssh_options._ssh_config_forces_no_multiplex()
        ssh_options._ssh_config_forces_no_multiplex()
        # Cached → the file is read and the warning printed exactly once.
        assert capsys.readouterr().err.count("getsockname") == 1

    def test_probe_is_windows_only(self, monkeypatch):
        # On POSIX a Unix-socket ControlPath is exactly what works, so the same
        # config must NOT disable multiplexing.
        monkeypatch.setattr(ssh_options.sys, "platform", "linux")
        monkeypatch.setattr(
            ssh_options, "_read_ssh_config_text", lambda: _UNIX_SOCKET_GLOBAL_CONFIG
        )
        assert ssh_options._ssh_config_forces_no_multiplex() is False
        opts = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts  # POSIX multiplexing intact

    def test_named_pipe_config_keeps_default_multiplex(self, monkeypatch):
        # A Windows-friendly named-pipe global config is not a problem, so the
        # named-pipe default still applies.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(ssh_options, "_read_ssh_config_text", lambda: _NAMED_PIPE_GLOBAL_CONFIG)
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        paths = _control_path_values(ssh_options._ssh_multiplex_opts())
        assert paths == [r"\\.\pipe\openssh-hpc-cm-%C"]


# ---------------------------------------------------------------------------
# Runtime verdict + retry wrapper — version-probe-missed bind failure (2026-06-04)
# ---------------------------------------------------------------------------


class TestNamedPipeRuntimeFallback:
    """Pin the runtime auto-fallback for the version-probe-missed Windows
    OpenSSH named-pipe bind failure.

    The version probe (:func:`_windows_openssh_named_pipe_supported`) checks
    the local OpenSSH major version, but at least one Windows OpenSSH 8.x+
    build still fails the named-pipe ControlMaster bind at the syscall layer
    (``getsockname failed: Not a socket``) despite passing the version check
    — the guard tests one thing, the failure mode is at another layer.

    :func:`run_with_named_pipe_retry` is the recovery: it detects the
    ``getsockname failed: Not a socket`` marker in the returned
    CompletedProcess's stderr, calls :func:`mark_named_pipe_broken` (which
    flips :func:`_ssh_multiplex_opts` to the legacy ControlMaster=no override
    for the rest of the process), and retries the closure once.
    """

    def test_no_marker_returns_proc_unchanged(self):
        proc: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 0, stdout="ok", stderr=""
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return proc

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is proc
        assert len(calls) == 1  # no retry on success
        assert not ssh_options._named_pipe_runtime_broken()

    def test_marker_triggers_one_retry_and_marks_broken(self):
        # rc=127: the empirically-observed client-failure rc of the Win32
        # mux-bind failure (native OpenSSH 9.5p2, 2026-07-20 — see
        # ssh_options._NAMED_PIPE_CLIENT_FAILURE_RCS).
        bad = subprocess.CompletedProcess(
            ["ssh", "host"], 127, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        good = subprocess.CompletedProcess(["ssh", "host"], 0, stdout="ok", stderr="")
        outcomes = iter([bad, good])
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return next(outcomes)

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is good
        assert len(calls) == 2  # exactly one retry
        assert ssh_options._named_pipe_runtime_broken()  # verdict marked

    def test_already_broken_skips_retry_even_on_marker(self):
        # If a prior code path already marked the verdict broken, the wrapper
        # short-circuits the retry — exactly one attempt regardless of the
        # marker. Avoids infinite retry on a host that consistently fails.
        # (rc=127 — a gated rc — so the pin still discriminates: remove the
        # already-broken short-circuit and this goes RED at 2 calls.)
        ssh_options.mark_named_pipe_broken()
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 127, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return bad

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is bad
        assert len(calls) == 1  # no retry

    def test_other_failure_not_retried(self):
        # A non-zero exit with a different stderr is NOT our marker — return
        # as-is, don't mark the verdict, don't retry. (Some other code path
        # owns that failure mode.)
        proc: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 1, stdout="", stderr="Permission denied (publickey)."
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return proc

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is proc
        assert len(calls) == 1
        assert not ssh_options._named_pipe_runtime_broken()

    def test_remote_rc_with_marker_neither_marks_nor_retries(self):
        # THE GATE (fleet conflation-sweep residual #1): a non-zero rc
        # OUTSIDE the client-failure set means the REMOTE command exited
        # (ssh propagates its status) — the marker in stderr is then
        # remote-controlled content (a log line, or forged). No
        # mark_named_pipe_broken(), no re-run: the failure propagates so a
        # possibly partially-executed command is never re-executed. rc=1 is
        # exactly the shape the pre-gate helper retried on (mutation tooth:
        # the ungated matcher goes RED here).
        proc: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 1, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return proc

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is proc
        assert len(calls) == 1  # NO re-run
        assert not ssh_options._named_pipe_runtime_broken()  # NO mark

    def test_success_rc_with_marker_is_returned_unchanged(self):
        # rc=0 with the marker in stderr: the command SUCCEEDED and its
        # (remote) output merely contains the string (e.g. a log echo) —
        # neither a break nor a re-run.
        proc: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 0, stdout="ok", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return proc

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is proc
        assert len(calls) == 1
        assert not ssh_options._named_pipe_runtime_broken()

    def test_rc255_marker_triggers_one_retry(self):
        # 255 = OpenSSH's classic client-side failure rc — kept in the gate
        # so a Win32 build whose mux-bind failure takes that exit still
        # recovers (the 2026-06-04 incident's rc was never recorded).
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 255, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        good: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 0, stdout="ok", stderr=""
        )
        outcomes = iter([bad, good])
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return next(outcomes)

        result = ssh_options.run_with_named_pipe_retry(rebuild)
        assert result is good
        assert len(calls) == 2  # exactly one retry
        assert ssh_options._named_pipe_runtime_broken()

    def test_non_idempotent_marker_marks_broken_but_never_reruns(self):
        # The qsub/sbatch guard (F54/F55 doctrine): a declared non-idempotent
        # command records the broken verdict (future legs demote to
        # ControlMaster=no on their first attempt) but is NEVER re-executed —
        # the gated rc+marker pair is only strong evidence the command never
        # dispatched (a remote command exiting 127 with the marker in its own
        # stderr is indistinguishable at this layer), and a duplicate submit
        # is unrecoverable while a surfaced failure is not.
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 127, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return bad

        result = ssh_options.run_with_named_pipe_retry(rebuild, idempotent=False)
        assert result is bad  # the failure propagates
        assert len(calls) == 1  # NO re-run
        assert ssh_options._named_pipe_runtime_broken()  # verdict recorded

    def test_non_idempotent_rc255_marker_never_reruns(self):
        # Same guard at the doctrine rc (255): a remote command exiting 255
        # with the marker in its stderr must not be double-fired either.
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh", "host"], 255, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[None] = []

        def rebuild() -> subprocess.CompletedProcess[str]:
            calls.append(None)
            return bad

        result = ssh_options.run_with_named_pipe_retry(rebuild, idempotent=False)
        assert result is bad
        assert len(calls) == 1
        assert ssh_options._named_pipe_runtime_broken()

    def test_ssh_run_non_idempotent_scope_never_reruns(self, monkeypatch):
        # End-to-end at the ssh_run seam: under non_idempotent_remote() (the
        # scheduler-submit leg, backends/_remote_base._execute_command) a
        # gated 127+marker failure records the verdict but the command is
        # executed exactly ONCE — the named-pipe recovery can never
        # double-fire a qsub.
        monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")  # isolate the helper
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh"], 127, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        calls: list[list[str]] = []

        def _fake_capture(argv, *, timeout):  # matches remote.capture_via_select
            calls.append(argv)
            return bad

        monkeypatch.setattr(remote, "capture_via_select", _fake_capture)

        with remote.non_idempotent_remote():
            proc = remote.ssh_run("qsub job.sh", ssh_target="u@h")
        assert proc is bad  # the failure surfaces, honestly
        assert len(calls) == 1  # NO re-execution of the submit
        assert ssh_options._named_pipe_runtime_broken()  # verdict recorded

    def test_ssh_run_idempotent_marker_failure_retries_and_recovers(self, monkeypatch):
        # Idempotent control for the scope test above: the same gated
        # 127+marker failure outside non_idempotent_remote() retries once
        # and recovers (the pre-gate behaviour, preserved on the gated rc).
        monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
        bad: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh"], 127, stdout="", stderr="getsockname failed: Not a socket\n"
        )
        good: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(
            ["ssh"], 0, stdout="ok", stderr=""
        )
        outcomes = iter([bad, good])
        calls: list[list[str]] = []

        def _fake_capture(argv, *, timeout):
            calls.append(argv)
            return next(outcomes)

        monkeypatch.setattr(remote, "capture_via_select", _fake_capture)

        proc = remote.ssh_run("qstat", ssh_target="u@h")
        assert proc is good
        assert len(calls) == 2  # failed once, retried, succeeded
        assert ssh_options._named_pipe_runtime_broken()

    def test_mark_broken_demotes_multiplex_opts_to_legacy(self, monkeypatch):
        # Once the runtime verdict flips to False, _ssh_multiplex_opts switches
        # to the legacy ControlMaster=no / ControlPath=none override on win32
        # even when (a) the env default would otherwise emit named-pipe and
        # (b) the version probe says the local binary supports it. This is
        # the mechanism by which the retry's "rebuilt argv" actually picks
        # up the new option set inside the closure.
        monkeypatch.setattr(ssh_options.sys, "platform", "win32")
        monkeypatch.setattr(ssh_options, "_local_openssh_major", lambda: 9)
        opts_before = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=auto" in opts_before
        assert r"ControlPath=\\.\pipe\openssh-hpc-cm-%C" in opts_before

        ssh_options.mark_named_pipe_broken()

        opts_after = ssh_options._ssh_multiplex_opts()
        assert "ControlMaster=no" in opts_after
        assert "ControlPath=none" in opts_after

    def test_mark_broken_is_idempotent(self):
        # Subsequent mark_named_pipe_broken() calls no-op: the verdict is
        # already False, and the user-facing stderr warning fires once per
        # process. (Captured via :func:`capsys` is overkill here; just
        # confirm the verdict stays sticky-False.)
        assert not ssh_options._named_pipe_runtime_broken()
        ssh_options.mark_named_pipe_broken()
        assert ssh_options._named_pipe_runtime_broken()
        ssh_options.mark_named_pipe_broken()
        ssh_options.mark_named_pipe_broken()
        assert ssh_options._named_pipe_runtime_broken()  # still True, no flip
