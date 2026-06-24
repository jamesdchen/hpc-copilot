"""SSH option-building helpers (ControlPersist multiplexing, persist interval).

Extracted from :mod:`hpc_agent.infra.remote` so the remote-IO module can
stay focused on subprocess plumbing. The helpers here are pure config —
they consult environment variables and return the SSH option list that
``ssh_run`` / ``rsync_push`` splice into their argv.

Re-exported from :mod:`hpc_agent.infra.remote` for backwards
compatibility (the names are underscore-prefixed and were not part of
the public ``__all__``, but external callers may still reach for them
on private internals).

Environment variables consulted here
------------------------------------
``HPC_NO_SSH_MULTIPLEX=1``
    Opt out of multiplexing entirely (always wins).
``HPC_SSH_PERSIST_INTERVAL``
    Override the ControlPersist window — see :func:`_ssh_multiplex_opts`.
``HPC_SSH_NAMED_PIPE=0`` *(opt-out, Windows-only)*
    Connection multiplexing on native Windows OpenSSH — via a named-pipe
    ``ControlPath`` (``\\\\.\\pipe\\openssh-hpc-cm-%C``) — is now the
    **default** on Windows (it was an ``=1`` opt-in through 0.10.x). Set
    ``HPC_SSH_NAMED_PIPE=0`` to opt back out to the legacy
    ``ControlMaster=no`` / ``ControlPath=none`` override. Named-pipe
    ``ControlPath`` requires local OpenSSH ≥ 8.x; a one-time probe at the
    first :func:`_ssh_multiplex_opts` call warns and falls back to the
    legacy override when the local binary reports an older version.
    ``HPC_NO_SSH_MULTIPLEX=1`` still short-circuits ahead of all of this.
``HPC_SSH_CONNECT_TIMEOUT``
    Bound the TCP-connect phase of every ssh-family call — see
    :func:`_ssh_connect_opts`. Default ``15`` (seconds); set to ``default``
    to drop the override and let OpenSSH / ``ssh_config`` decide.
``HPC_SSH_CIPHER`` / ``HPC_SSH_MAC`` / ``HPC_SSH_COMPRESSION``
    Cipher / MAC / compression tuning spliced into every ssh-family call —
    see :func:`_ssh_crypto_opts`. Defaults favour AES-NI-accelerated GCM
    ciphers + ETM MACs and pin ``Compression=no``; set any to ``default``
    to drop that override.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

__all__ = [
    "_local_openssh_supports_gcm",
    "_named_pipe_runtime_broken",
    "_resolve_ssh_persist_interval",
    "_rsync_rsh_env",
    "_scp_binary",
    "_ssh_add_binary",
    "_ssh_binary",
    "_ssh_config_override_opts",
    "_ssh_config_forces_no_multiplex",
    "_ssh_connect_opts",
    "_ssh_crypto_opts",
    "_ssh_multiplex_opts",
    "_windows_openssh_named_pipe_supported",
    "mark_named_pipe_broken",
    "reset_named_pipe_runtime_verdict",
    "run_with_named_pipe_retry",
    "ssh_argv",
    "ssh_env",
]


# Native Windows OpenSSH location. On Windows the agent's Bash tool is
# usually Git Bash, whose ``/usr/bin/ssh`` (Git's bundled OpenSSH)
# shadows the system binary on PATH. Git's ssh cannot reach the Windows
# OpenSSH named-pipe ``ssh-agent`` (it wants a Unix ``SSH_AUTH_SOCK``
# which Windows never sets), so key-based auth that works from
# PowerShell fails silently from Git Bash. Prefer the native binary when
# present so the ssh that actually runs can talk to the pipe agent that
# ``infra.ssh_agent`` already detects.
_WIN_OPENSSH_SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"
_WIN_OPENSSH_SCP = r"C:\Windows\System32\OpenSSH\scp.exe"
_WIN_OPENSSH_SSH_ADD = r"C:\Windows\System32\OpenSSH\ssh-add.exe"


def _resolve_binary(*, env_var: str, win_default: str, name: str) -> str:
    """Resolve an ssh-family binary, preferring an explicit override then
    (on Windows) the native OpenSSH executable, else the bare PATH name.

    *env_var* (e.g. ``HPC_SSH_BINARY``) wins unconditionally when set so a
    user can pin the binary on any platform. On Windows, when no override
    is set, the native ``C:\\Windows\\System32\\OpenSSH`` executable is
    used when it exists. Everywhere else (and when the native binary is
    absent) the bare *name* is returned so normal PATH resolution applies
    — preserving the existing Linux/macOS behaviour exactly.
    """
    override = os.environ.get(env_var)
    if override:
        return override
    if sys.platform == "win32" and os.path.isfile(win_default):
        return win_default
    return name


def _ssh_binary() -> str:
    """Path/name of the ``ssh`` binary to invoke. See :func:`_resolve_binary`.

    Override with ``HPC_SSH_BINARY``.
    """
    return _resolve_binary(env_var="HPC_SSH_BINARY", win_default=_WIN_OPENSSH_SSH, name="ssh")


def _scp_binary() -> str:
    """Path/name of the ``scp`` binary to invoke. See :func:`_resolve_binary`.

    Override with ``HPC_SCP_BINARY``.
    """
    return _resolve_binary(env_var="HPC_SCP_BINARY", win_default=_WIN_OPENSSH_SCP, name="scp")


def _ssh_add_binary() -> str:
    """Path/name of the ``ssh-add`` binary to invoke. See :func:`_resolve_binary`.

    Override with ``HPC_SSH_ADD_BINARY``. Mirrors :func:`_ssh_binary`: on
    Windows the agent probe in :mod:`hpc_agent.infra.ssh_agent` must reach
    the native OpenSSH named-pipe agent, but a bare ``ssh-add`` from Git
    Bash resolves to Git's ``/usr/bin/ssh-add`` — which only knows
    ``SSH_AUTH_SOCK`` (never set on Windows) and so reports a false
    "agent unreachable". Preferring the native binary fixes that probe.
    """
    return _resolve_binary(
        env_var="HPC_SSH_ADD_BINARY", win_default=_WIN_OPENSSH_SSH_ADD, name="ssh-add"
    )


def _rsync_rsh_env() -> dict[str, str]:
    """Return env overrides pinning rsync's remote shell to :func:`_ssh_binary`.

    rsync invokes its own ``ssh`` for the transport unless ``RSYNC_RSH``
    (or ``-e``) says otherwise; on Windows that picks up Git Bash's ssh,
    same as the bare call sites. Returns ``{"RSYNC_RSH": <cmd>}`` when a
    non-default remote shell should be used, else ``{}`` so PATH
    resolution is unchanged. Respects a caller-set ``RSYNC_RSH`` by
    leaving it alone.

    On Windows the ``RSYNC_RSH`` command also carries the transfer-sized
    :func:`_ssh_config_override_opts` override (``-o ControlMaster=no -o
    ControlPath=none``) so rsync's own ssh can't pick up the user's
    ``~/.ssh/config`` multiplexing — which native Windows OpenSSH cannot
    honour — any more than the bare ``scp`` call site can. rsync is a
    one-shot transfer like scp, so it follows the *transfer* override and is
    deliberately NOT swept up by the named-pipe multiplex default that
    :func:`_ssh_multiplex_opts` applies to the long-lived ``ssh`` command
    channel — a one-shot transfer gains nothing from being a multiplex
    client. (Both helpers return the same override on Windows today; routing
    rsync through the transfer one keeps it that way after the flip.)
    """
    if os.environ.get("RSYNC_RSH"):
        return {}
    ssh = _ssh_binary()
    # Crypto tuning (#256) rides rsync's own ssh too. On Windows the transfer
    # override is appended as before. Only emit RSYNC_RSH when we actually have
    # something to override: a bare ``ssh`` with no extra opts is exactly
    # rsync's default, so leave the env unset to preserve PATH resolution and
    # the pre-tuning no-op POSIX path.
    parts = [ssh, *_ssh_connect_opts(), *_ssh_crypto_opts()]
    if sys.platform == "win32":
        parts += _ssh_config_override_opts()
    if parts == ["ssh"]:
        return {}
    return {"RSYNC_RSH": " ".join(parts)}


# Default ControlPersist window. Tunable via ``HPC_SSH_PERSIST_INTERVAL``;
# see :func:`_ssh_multiplex_opts` for the accepted shapes.
_DEFAULT_SSH_PERSIST_INTERVAL = "10m"

# Characters that must never appear in the persist-interval env var. OpenSSH
# accepts plain ints (seconds), suffixed durations (``30m``, ``2h``), ``0``
# (persist until master exits), and ``no``/``yes``; none of those need any
# of these chars, so any occurrence indicates a typo or an injection attempt.
_DISALLOWED_PERSIST_CHARS = " \t\n\r;|&`$<>\"'\\*?!()=/"


# Minimum local OpenSSH major version that supports a named-pipe
# ``ControlPath`` on Windows. %C substitution inside the ``\\.\pipe\...``
# namespace works from OpenSSH 8.x onward; older binaries abort the moment
# ControlMaster is in effect, so we fall back to the legacy override there.
_MIN_NAMED_PIPE_OPENSSH_MAJOR = 8


def _local_openssh_major() -> int | None:
    """Major version of the local ``ssh`` binary, or ``None`` if undeterminable.

    Parses ``ssh -V`` (which prints to stderr on every OpenSSH build —
    ``OpenSSH_8.9p1 ...`` on POSIX, ``OpenSSH_for_Windows_8.6p1 ...`` on
    native Windows). Returns the integer major version, or ``None`` when the
    probe can't run or its output doesn't parse — the caller treats ``None``
    as "don't demote the default on a probe hiccup".
    """
    try:
        proc = subprocess.run(
            [_ssh_binary(), "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = f"{proc.stderr or ''}{proc.stdout or ''}"
    match = re.search(r"OpenSSH(?:_for_Windows)?_(\d+)\.\d+", blob)
    if match is None:
        return None
    return int(match.group(1))


@functools.cache
def _windows_openssh_named_pipe_supported() -> bool:
    """Whether the local Windows OpenSSH can use a named-pipe ``ControlPath``.

    Named-pipe ``ControlPath`` needs OpenSSH ≥ 8.x. Returns ``True`` when the
    probed major is ≥ 8 *or* the version can't be determined (we don't retreat
    from the new default on a probe hiccup, and OpenSSH < 8 is rare on the
    Windows 10/11 builds that ship the native binary). Returns ``False`` —
    with a one-time stderr warning — only on a positively-detected < 8.x
    binary, where :func:`_ssh_multiplex_opts` falls back to the legacy
    ``ControlMaster=no`` / ``ControlPath=none`` override.

    Cached: the local binary does not change mid-process, and this keeps the
    warning to a single emission.
    """
    major = _local_openssh_major()
    if major is not None and major < _MIN_NAMED_PIPE_OPENSSH_MAJOR:
        print(
            f"hpc-agent: local OpenSSH {major}.x is older than 8.x, which does "
            "not support a named-pipe ControlPath; falling back to "
            "unmultiplexed SSH on Windows (each call pays a fresh handshake). "
            "Upgrade Windows OpenSSH to ≥ 8.x to enable connection "
            "multiplexing, or set HPC_NO_SSH_MULTIPLEX=1 to silence this.",
            file=sys.stderr,
        )
        return False
    return True


# Runtime verdict for the named-pipe ControlMaster feature. The version probe
# above (necessary-but-not-sufficient) cannot catch the syscall-layer failure
# mode observed on at least one Windows OpenSSH 8.x+ build (2026-06-04):
# ``getsockname failed: Not a socket`` at named-pipe bind time, despite a clean
# version check. ``infra.remote`` subprocess plumbing calls
# :func:`mark_named_pipe_broken` when it detects the marker in ssh stderr, and
# :func:`_ssh_multiplex_opts` then switches to the legacy ``ControlMaster=no``
# fallback for the rest of the process. None=untested, True=works (no negative
# evidence), False=broken (marker observed).
_NAMED_PIPE_RUNTIME_VERDICT: bool | None = None


def mark_named_pipe_broken() -> None:
    """Record that this process's local ssh failed the named-pipe bind.

    Idempotent — subsequent calls no-op (verdict already set, warning already
    emitted). Subprocess plumbing in :mod:`hpc_agent.infra.remote` calls this
    when it detects ``getsockname failed: Not a socket`` in ssh's stderr, the
    failure mode the version probe in
    :func:`_windows_openssh_named_pipe_supported` cannot catch. Once marked,
    :func:`_ssh_multiplex_opts` switches to the legacy ``ControlMaster=no`` /
    ``ControlPath=none`` fallback for the rest of the process.
    """
    global _NAMED_PIPE_RUNTIME_VERDICT
    if _NAMED_PIPE_RUNTIME_VERDICT is False:
        return
    _NAMED_PIPE_RUNTIME_VERDICT = False
    print(
        "hpc-agent: this Windows OpenSSH build failed the named-pipe "
        "ControlMaster bind ('getsockname failed: Not a socket') despite "
        "passing the OpenSSH version probe. Falling back to unmultiplexed "
        "SSH for the rest of this session. Set HPC_SSH_NAMED_PIPE=0 in "
        "your environment to skip the first-failure cost on future runs.",
        file=sys.stderr,
    )


def _named_pipe_runtime_broken() -> bool:
    """True when :func:`mark_named_pipe_broken` has been called this process."""
    return _NAMED_PIPE_RUNTIME_VERDICT is False


def reset_named_pipe_runtime_verdict() -> None:
    """Reset the runtime verdict to ``None`` (testing seam)."""
    global _NAMED_PIPE_RUNTIME_VERDICT
    _NAMED_PIPE_RUNTIME_VERDICT = None


def run_with_named_pipe_retry(
    rebuild_and_run: Callable[[], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Run *rebuild_and_run*; on the named-pipe ``getsockname`` failure, retry once.

    Detects ``getsockname failed: Not a socket`` in the returned
    :class:`subprocess.CompletedProcess` stderr — the syscall-layer named-pipe
    ControlMaster bind failure the version probe in
    :func:`_windows_openssh_named_pipe_supported` cannot catch (observed on a
    Windows OpenSSH 8.x+ build, 2026-06-04). On detection,
    :func:`mark_named_pipe_broken` is called (short-circuits future named-pipe
    attempts in :func:`_ssh_multiplex_opts`), then *rebuild_and_run* is invoked
    again — it MUST rebuild any argv or env derived from the now-updated
    multiplex opts (i.e. call ``ssh_argv("ssh")`` / ``ssh_env()`` *inside* the
    closure, not before).

    Returns the proc as-is when no marker is present, when the verdict is
    already broken (this process has already done its one retry, or another
    code path marked it), or when the retry itself also fails. Exactly one
    retry per process — the verdict is sticky.
    """
    proc = rebuild_and_run()
    if _named_pipe_runtime_broken():
        return proc
    if proc.returncode != 0 and proc.stderr and "getsockname failed: Not a socket" in proc.stderr:
        mark_named_pipe_broken()
        return rebuild_and_run()
    return proc


# ControlMaster values that ENABLE multiplexing (a client/master is set up).
# ``no`` and ``false`` disable it; everything here makes ssh open or reuse a
# master at the configured ControlPath.
_ENABLING_CONTROLMASTER_VALUES = frozenset({"auto", "yes", "ask", "autoask", "true"})


def _read_ssh_config_text() -> str | None:
    """Return the contents of ``~/.ssh/config``, or ``None`` if unreadable/absent.

    A thin seam so the config probe (:func:`_ssh_config_forces_no_multiplex`)
    can be unit-tested without touching the real home directory. ``Path.home()``
    resolves ``%USERPROFILE%`` on Windows, so this finds the same file
    ``ssh.exe`` reads.
    """
    try:
        path = Path.home() / ".ssh" / "config"
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _ssh_config_declares_unix_socket_global_master(text: str) -> bool:
    """True when an ``ssh_config`` *text* has a global (``Host *``) stanza that
    enables ``ControlMaster`` with a non-named-pipe (Unix-socket) ``ControlPath``.

    This is the configuration that breaks native Windows OpenSSH: a
    ``Host *`` ``ControlMaster auto`` with a ``ControlPath`` like
    ``~/.ssh/cm-%r@%h:%p`` makes every ``ssh.exe`` try to ``getsockname`` a
    Unix socket and abort with ``getsockname failed: Not a socket`` — and a
    command-line ``-o ControlPath=none`` does not reliably override it on
    Windows (field finding on #243). Keywords are case-insensitive; a
    ``ControlPath`` of ``none`` or a ``\\\\.\\pipe\\...`` named pipe is fine.
    """
    in_global = False
    cm_enabled = False
    bad_path = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # ssh_config separates keyword from value with whitespace and/or a
        # single ``=`` — accept both shapes.
        match = re.match(r"(\S+?)\s*(?:=|\s)\s*(.*)$", line)
        if match is None:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key in ("host", "match"):
            # A new block begins. Settle the verdict on the block we just left
            # before re-evaluating which kind of block we're entering.
            if in_global and cm_enabled and bad_path:
                return True
            in_global = key == "host" and "*" in value.split()
            cm_enabled = False
            bad_path = False
            continue
        if not in_global:
            continue
        if key == "controlmaster":
            cm_enabled = value.lower() in _ENABLING_CONTROLMASTER_VALUES
        elif key == "controlpath":
            v = value.strip().strip('"').strip("'")
            if v.lower() != "none" and not v.lower().startswith(r"\\.\pipe"):
                bad_path = True
    return in_global and cm_enabled and bad_path


@functools.cache
def _ssh_config_forces_no_multiplex() -> bool:
    """True (Windows only) when ``~/.ssh/config`` has a Unix-socket global
    ``ControlMaster`` stanza that would break native Windows OpenSSH; warns once.

    When this fires the framework forces ``HPC_NO_SSH_MULTIPLEX`` semantics
    (emit no multiplex flags at all) — the only thing observed to clear the
    ``getsockname failed: Not a socket`` failure for users with a problematic
    ``Host *`` ``ControlMaster auto`` + Unix-socket ``ControlPath`` (#243),
    because the per-command ``-o`` override does not reliably win against the
    config on Windows. The one-time warning points the user at the fix
    (rewrite to a ``\\\\.\\pipe\\...`` named pipe, or scope by host).

    POSIX returns ``False`` immediately (a Unix-socket ``ControlPath`` is
    exactly what works there). Cached: the config file isn't expected to change
    mid-process, and this keeps both the file read and the warning to once.
    """
    if sys.platform != "win32":
        return False
    text = _read_ssh_config_text()
    if text is None:
        return False
    if not _ssh_config_declares_unix_socket_global_master(text):
        return False
    print(
        "hpc-agent: detected a Unix-socket ControlMaster in ~/.ssh/config "
        "(a `Host *` stanza with a non-named-pipe ControlPath) — native "
        "Windows OpenSSH would fail it with `getsockname failed: Not a "
        "socket`, and a command-line override does not reliably win, so SSH "
        "connection multiplexing is DISABLED for this session. To keep the "
        r"speedup, rewrite that ControlPath to a named pipe (\\.\pipe\...) "
        "or scope the ControlMaster stanza by host instead of `Host *`.",
        file=sys.stderr,
    )
    return True


def _ssh_config_override_opts() -> list[str]:
    """SSH ``-o`` options that neutralise a user's ssh-config multiplexing on
    Windows; ``[]`` on POSIX.

    Native Windows OpenSSH can't use a ``ControlPath`` Unix socket
    (``getsockname failed: Not a socket``). For one-shot transfers (scp, the
    tar-fallback push) we don't want to *be* a multiplex master — but we must
    still stop the user's ``~/.ssh/config`` (often a ``Host *`` ``ControlMaster``
    stanza) from forcing multiplexing, since a command-line ``-o`` beats the
    config file. On POSIX nothing is needed. ``HPC_NO_SSH_MULTIPLEX=1`` disables
    it everywhere. This is the transfer-sized subset of :func:`_ssh_multiplex_opts`.

    The named-pipe ``ControlPath`` that :func:`_ssh_multiplex_opts` now emits
    by default on Windows (opt out with ``HPC_SSH_NAMED_PIPE=0``) is
    *deliberately* not mirrored here: one-shot transfers gain nothing from
    being a multiplex client, so we keep the override and skip the master-setup
    overhead even when long-lived ssh sessions are multiplexed.
    """
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1" or _ssh_config_forces_no_multiplex():
        return []
    if sys.platform == "win32":
        return ["-o", "ControlMaster=no", "-o", "ControlPath=none"]
    return []


def _ssh_multiplex_opts() -> list[str]:
    """Return SSH options that enable connection multiplexing.

    First call to a host opens the master socket; subsequent calls within
    the ControlPersist window reuse it. For an agent polling ``status``
    every 30s during a 4-hour job, this is the difference between hundreds
    of full handshakes and a single one.

    Env vars
    --------
    ``HPC_NO_SSH_MULTIPLEX=1``
        Opt out of multiplexing entirely (some clusters disallow
        multiplexed sessions, e.g. due to PAM-based session limits).
        On Windows multiplexing historically couldn't work at all —
        native Windows OpenSSH has no ``ControlPath`` Unix socket
        (``ssh.exe`` aborts with ``getsockname failed: Not a socket``) —
        so instead of the usual ``ControlMaster=auto`` we emit an
        explicit ``ControlMaster=no`` / ``ControlPath=none`` override.
        Returning ``[]`` would only drop our own flags, leaving a user's
        ``~/.ssh/config`` ``ControlMaster`` (often a ``Host *`` stanza)
        to bite; a command-line ``-o`` beats the config file *most* of the
        time. ``HPC_NO_SSH_MULTIPLEX=1`` short-circuits to ``[]`` first and
        wins over the named-pipe default below.

        The same ``[]`` is forced *implicitly* by
        :func:`_ssh_config_forces_no_multiplex`: on Windows a ``~/.ssh/config``
        with a global (``Host *``) ``ControlMaster`` whose ``ControlPath`` is a
        Unix socket (not a ``\\\\.\\pipe\\...`` named pipe) makes ``ssh.exe``
        abort with ``getsockname failed: Not a socket`` — and the per-command
        ``-o ControlPath=none`` override does NOT reliably win against it on
        Windows (field finding, #243). Emitting no multiplex flags is the only
        behaviour observed to clear it, so the probe warns once and disables
        multiplexing for the session.
    ``HPC_SSH_PERSIST_INTERVAL``
        Override the ControlPersist window. The value is passed verbatim
        to OpenSSH, so any shape ``ssh_config(5)`` accepts works:

        * plain integer seconds (e.g. ``600``)
        * suffixed durations (e.g. ``30m``, ``2h``, ``1h30m``)
        * ``0`` — persist until the master exits
        * ``no`` / ``yes`` — disable persist (master exits with last
          session) / persist forever

        Defaults to ``10m`` for backwards compatibility. The value is
        validated loosely: whitespace and shell metacharacters are
        rejected; on rejection we log to stderr (no raise) and fall back
        to the default so a typo cannot break every cluster call.

        When the value is ``no``, no ``ControlPersist`` option is emitted —
        OpenSSH's default behaviour applies (master exits with the last
        client session).
    ``HPC_SSH_NAMED_PIPE=0`` *(opt-out, Windows-only)*
        Connection multiplexing on native Windows OpenSSH via a named-pipe
        ``ControlPath`` of the form ``\\\\.\\pipe\\openssh-hpc-cm-%C``
        (OpenSSH computes the ``%C`` token — connection-tuple hash — at
        runtime; ``%C`` substitution works inside the named-pipe path on
        Windows OpenSSH ≥ 8.x) is now the **default** on Windows — it was an
        ``=1`` opt-in through 0.10.x. Without it the Windows branch pays a
        fresh handshake per call (~1-2 seconds each, hundreds of seconds per
        submit/monitor/aggregate cycle). Set ``HPC_SSH_NAMED_PIPE=0`` to opt
        back out to the legacy ``ControlMaster=no`` / ``ControlPath=none``
        override. A one-time probe (:func:`_windows_openssh_named_pipe_supported`)
        warns and falls back to that legacy override when the local OpenSSH is
        older than 8.x (named-pipe ``ControlPath`` support landed in 8.x). The
        ``HPC_NO_SSH_MULTIPLEX=1`` short-circuit above still wins.
        ``ControlPersist`` is honoured exactly as on POSIX. Note this only
        affects :func:`_ssh_multiplex_opts` (the long-lived ``ssh`` command
        channel): the transfer-sized :func:`_ssh_config_override_opts` (used
        by ``scp`` and the tar-fallback push) stays ``ControlMaster=no`` /
        ``ControlPath=none`` regardless — one-shot transfers don't benefit
        from being a multiplex client.
    """
    # HPC_NO_SSH_MULTIPLEX=1 is the explicit kill switch; the ssh-config probe
    # is the *implicit* one — a Windows ~/.ssh/config whose Unix-socket
    # ControlMaster would break native OpenSSH forces the same no-flags
    # behaviour (the only thing that reliably clears the getsockname failure;
    # #243), after warning the user once how to fix it.
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1" or _ssh_config_forces_no_multiplex():
        return []
    if sys.platform == "win32":
        # Named-pipe multiplexing is the default on Windows; opt out with
        # HPC_SSH_NAMED_PIPE=0, or when the local OpenSSH is too old to honour
        # a named-pipe ControlPath (the probe warns once and demotes us).
        if (
            os.environ.get("HPC_SSH_NAMED_PIPE") == "0"
            or _named_pipe_runtime_broken()
            or not _windows_openssh_named_pipe_supported()
        ):
            # Legacy fallback: native Windows OpenSSH can't use a ControlPath
            # Unix socket (getsockname failed: Not a socket), so emit the
            # explicit ControlMaster=no/ControlPath=none override (see
            # _ssh_config_override_opts) — returning [] would only omit OUR
            # flags and let a user's ~/.ssh/config ControlMaster bite.
            # _named_pipe_runtime_broken() catches the version-passes-but-bind-
            # fails case (mark_named_pipe_broken() is called by infra.remote
            # when it detects the marker in stderr; see that module).
            return _ssh_config_override_opts()
        # Default: the ``\\.\pipe\<name>`` namespace is the Windows equivalent
        # of a Unix domain socket; %C is substituted by ssh at runtime
        # (verified working on Windows OpenSSH ≥ 8.x), so different
        # hosts/users/ports get isolated masters automatically.
        opts = [
            "-o",
            "ControlMaster=auto",
            "-o",
            r"ControlPath=\\.\pipe\openssh-hpc-cm-%C",
        ]
        persist = _resolve_ssh_persist_interval()
        if persist is not None:
            opts += ["-o", f"ControlPersist={persist}"]
        return opts
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    opts = [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={runtime_dir}/hpc-cm-%C",
    ]
    persist = _resolve_ssh_persist_interval()
    if persist is not None:
        opts += ["-o", f"ControlPersist={persist}"]
    return opts


def _resolve_ssh_persist_interval() -> str | None:
    """Return the ControlPersist value to emit, or ``None`` to omit the opt.

    Reads ``HPC_SSH_PERSIST_INTERVAL`` and applies the validation rule
    documented on :func:`_ssh_multiplex_opts`. Returns ``None`` when the
    operator explicitly disabled persist (``no``); otherwise returns the
    string to embed after ``ControlPersist=``. Invalid values fall back
    to :data:`_DEFAULT_SSH_PERSIST_INTERVAL` after a stderr warning.
    """
    raw = os.environ.get("HPC_SSH_PERSIST_INTERVAL")
    if raw is None or raw == "":
        return _DEFAULT_SSH_PERSIST_INTERVAL
    bad = sorted({c for c in _DISALLOWED_PERSIST_CHARS if c in raw})
    if bad:
        # Don't raise — a malformed env var in someone's shell rc should
        # not break every ssh call. Warn loudly and fall back to default.
        print(
            f"hpc-agent: ignoring HPC_SSH_PERSIST_INTERVAL={raw!r} "
            f"(disallowed characters {bad!r}); using default "
            f"{_DEFAULT_SSH_PERSIST_INTERVAL!r}",
            file=sys.stderr,
        )
        return _DEFAULT_SSH_PERSIST_INTERVAL
    if raw.lower() == "no":
        return None
    return raw


# --- ConnectTimeout (ban-driver hardening) -----------------------------------
#
# OpenSSH ships no default ``ConnectTimeout`` — it defers to the OS TCP timeout,
# which on a misconfigured host (wrong ``HostName``, an unreachable login node,
# a hostname that matches no ssh-config key) is tens of seconds to minutes. Each
# such call then hangs until ``infra.remote``'s ``SSH_TIMEOUT_SEC`` (60s)
# hard-kill. Slow failures pile up — and a burst of them from one IP is exactly
# what a cluster's fail2ban / connection-rate limiter bans. A tight connect
# bound fails fast and surfaces the misconfig before the pile-up forms. It caps
# only how long we wait to ESTABLISH the connection; a legitimately
# long-running remote command keeps the larger ``SSH_TIMEOUT_SEC`` budget.
_DEFAULT_SSH_CONNECT_TIMEOUT = "15"


def _ssh_connect_opts() -> list[str]:
    """SSH ``-o ConnectTimeout`` option bounding the TCP-connect wait.

    Spliced into the ``ssh`` command channel and one-shot ``scp`` by
    :func:`ssh_argv`, and into rsync's own ssh by :func:`_rsync_rsh_env`, so
    every connection this framework opens fails fast on an unreachable/
    misconfigured host instead of hanging to the subprocess hard-kill.

    Tunable via ``HPC_SSH_CONNECT_TIMEOUT`` (positive integer seconds). The
    literal ``default`` drops the override entirely (let OpenSSH / ssh_config
    decide). A non-positive or non-integer value warns to stderr — no raise, a
    typo must not break every ssh call — and falls back to
    :data:`_DEFAULT_SSH_CONNECT_TIMEOUT`.
    """
    raw = (os.environ.get("HPC_SSH_CONNECT_TIMEOUT") or _DEFAULT_SSH_CONNECT_TIMEOUT).strip()
    if raw.lower() == "default":
        return []
    if not raw.isdigit() or int(raw) <= 0:
        print(
            f"hpc-agent: ignoring HPC_SSH_CONNECT_TIMEOUT={raw!r} "
            f"(want a positive integer of seconds, or 'default'); using "
            f"{_DEFAULT_SSH_CONNECT_TIMEOUT!r}",
            file=sys.stderr,
        )
        raw = _DEFAULT_SSH_CONNECT_TIMEOUT
    return ["-o", f"ConnectTimeout={raw}"]


# --- Cipher / MAC / compression tuning (#256) --------------------------------
#
# OpenSSH's portable defaults favour broad compatibility: the
# chacha20-poly1305 cipher and a conservative MAC. On the modern x86 CPUs that
# reach HPC clusters over fast campus/VPN links, AES-NI makes the aes*-gcm
# ciphers noticeably faster, and the umac-128 / ETM MACs shave the integrity
# step too. We pin those by default and turn compression off (already OpenSSH's
# default, but an explicit ``-o`` beats a user's ``Compression yes`` ssh-config
# on a fast link, where compression only adds CPU). All three are env-tunable;
# set the env var to ``default`` to drop that one override and let ssh_config /
# the built-in default stand.
_DEFAULT_SSH_CIPHERS = "aes128-gcm@openssh.com,aes256-gcm@openssh.com"
_DEFAULT_SSH_MACS = "umac-128-etm@openssh.com,hmac-sha2-256-etm@openssh.com"
_DEFAULT_SSH_COMPRESSION = "no"

# AES-GCM ciphers and ETM MACs have been universal since OpenSSH 6.2 (2013); a
# positively-detected older LOCAL binary may reject them with "no matching
# cipher", so we drop the cipher/MAC overrides there (the same ``ssh -V`` probe
# seam #243 added). 6.0/6.1 slip through this coarse major check but are
# vanishingly rare in 2026 — and the env override is the escape hatch.
_MIN_GCM_OPENSSH_MAJOR = 6


@functools.cache
def _local_openssh_supports_gcm() -> bool:
    """Whether the local ssh is new enough for aes-gcm / ETM MAC overrides.

    ``True`` unless :func:`_local_openssh_major` *positively* reports a major
    older than :data:`_MIN_GCM_OPENSSH_MAJOR`; an undeterminable version returns
    ``True`` (don't demote on a probe hiccup — the binary is almost certainly
    modern). Cached: the local binary doesn't change mid-process and ``ssh -V``
    is a subprocess we must not pay on every ssh call.
    """
    major = _local_openssh_major()
    return not (major is not None and major < _MIN_GCM_OPENSSH_MAJOR)


def _ssh_crypto_opts() -> list[str]:
    """SSH ``-o`` options pinning a faster cipher/MAC and disabling compression.

    The list spliced into every ssh-family invocation that opens a connection:
    :func:`ssh_argv` for the ``ssh`` command channel and one-shot ``scp``, and
    :func:`_rsync_rsh_env` for rsync's own ssh. Each knob is independently
    tunable via an env var; the value is passed verbatim to OpenSSH, and the
    literal ``default`` drops that override entirely:

    ``HPC_SSH_CIPHER``
        OpenSSH ``Ciphers`` list. Default :data:`_DEFAULT_SSH_CIPHERS`.
    ``HPC_SSH_MAC``
        OpenSSH ``MACs`` list. Default :data:`_DEFAULT_SSH_MACS`.
    ``HPC_SSH_COMPRESSION``
        ``no`` (default) / ``yes`` — OpenSSH ``Compression``.

    On a positively-detected local OpenSSH older than
    :data:`_MIN_GCM_OPENSSH_MAJOR` (:func:`_local_openssh_supports_gcm`) the
    *default* cipher+MAC overrides are dropped — the binary may not know
    aes-gcm / ETM — while compression (universally supported) is still pinned.
    An explicit env value is the operator's call and is honoured regardless of
    the probe. Not cached (env is read live so tests / a mid-session export
    take effect); the costly version probe behind it is.

    Cluster-side compatibility: the server's sshd must also offer the cipher.
    AES-GCM has been universal cluster-side since OpenSSH 6.2; an ancient
    cluster that rejects it surfaces as "no matching cipher" — set
    ``HPC_SSH_CIPHER=default`` (and/or ``HPC_SSH_MAC=default``) to fall back.
    """
    cipher = os.environ.get("HPC_SSH_CIPHER") or _DEFAULT_SSH_CIPHERS
    mac = os.environ.get("HPC_SSH_MAC") or _DEFAULT_SSH_MACS
    compression = os.environ.get("HPC_SSH_COMPRESSION") or _DEFAULT_SSH_COMPRESSION

    # Drop the aes-gcm / ETM defaults on an old local binary, but honour an
    # explicit env override there (the user pinned it deliberately).
    gcm_ok = _local_openssh_supports_gcm()

    opts: list[str] = []
    if cipher.lower() != "default" and (gcm_ok or os.environ.get("HPC_SSH_CIPHER")):
        opts += ["-o", f"Ciphers={cipher}"]
    if mac.lower() != "default" and (gcm_ok or os.environ.get("HPC_SSH_MAC")):
        opts += ["-o", f"MACs={mac}"]
    if compression.lower() != "default":
        opts += ["-o", f"Compression={compression}"]
    return opts


def ssh_argv(kind: str, *, extra_opts: Iterable[str] = ()) -> list[str]:
    """Leading argv (resolved binary + platform-correct options) for an
    ssh-family command of *kind* — the single seam for ssh invocation.

    Owns both binary resolution (native Windows OpenSSH / ``HPC_*_BINARY``
    override / bare PATH name) and option assembly, so no call site can get
    either wrong — the regression class behind #145 / #154 / #156. Callers
    append only the per-invocation positionals (ssh target + remote command,
    scp src + dst, ``ssh-add -l``) via *extra_opts* and/or by extending the
    returned list.

    * ``"ssh"`` — command channel (``ssh_run``, the tar-fallback push):
      ``[<ssh>, -o BatchMode=yes, *_ssh_connect_opts(), *_ssh_multiplex_opts()]``.
      ``BatchMode`` fails fast on a missing key instead of hanging on a prompt;
      :func:`_ssh_connect_opts` bounds the connect phase so an unreachable host
      fails fast instead of hanging to the subprocess hard-kill;
      :func:`_ssh_multiplex_opts` reuses one connection on POSIX and applies
      the ControlMaster override on Windows.
    * ``"scp"`` — one-shot transfer: ``[<scp>, -o BatchMode=yes,
      *_ssh_connect_opts(), *_ssh_config_override_opts()]``. A transfer needn't
      be a multiplex master, but on Windows it still must neutralise the user's
      ssh-config ControlMaster, so it gets the override only (nothing on POSIX),
      plus the same connect bound.
    * ``"ssh-add"`` — local agent probe, no remote host: ``[<ssh-add>]``
      (no BatchMode, no multiplexing).

    rsync is env-based, not argv-based — see :func:`ssh_env`.
    """
    if kind == "ssh":
        return [
            _ssh_binary(),
            "-o",
            "BatchMode=yes",
            *_ssh_connect_opts(),
            *_ssh_crypto_opts(),
            *_ssh_multiplex_opts(),
            *extra_opts,
        ]
    if kind == "scp":
        return [
            _scp_binary(),
            "-o",
            "BatchMode=yes",
            *_ssh_connect_opts(),
            *_ssh_crypto_opts(),
            *_ssh_config_override_opts(),
            *extra_opts,
        ]
    if kind == "ssh-add":
        return [_ssh_add_binary(), *extra_opts]
    raise ValueError(f"unknown ssh-family kind {kind!r}; expected 'ssh', 'scp', or 'ssh-add'")


def ssh_env() -> dict[str, str]:
    """Env overrides for a subprocess that spawns its *own* ssh (rsync).

    rsync runs the bare ``rsync`` binary but invokes its own ssh for the
    transport; :func:`_rsync_rsh_env` pins that to the resolved binary and, on
    Windows, the multiplex override — the env-var twin of what :func:`ssh_argv`
    splices into an argv. Merge into ``os.environ`` for the rsync subprocess.
    """
    return _rsync_rsh_env()
