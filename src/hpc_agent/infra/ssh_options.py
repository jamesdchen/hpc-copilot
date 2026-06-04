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
``HPC_SSH_NAMED_PIPE=1`` *(opt-in, Windows-only)*
    Enable connection multiplexing on native Windows OpenSSH via a
    named-pipe ``ControlPath`` (``\\\\.\\pipe\\openssh-hpc-cm-%C``).
    Validated against OpenSSH ≥ 8.x on Windows; without it the Windows
    branch keeps emitting ``ControlMaster=no`` / ``ControlPath=none``
    (the legacy default that avoids the Unix-socket
    ``getsockname failed: Not a socket`` failure). ``HPC_NO_SSH_MULTIPLEX=1``
    still short-circuits ahead of this opt-in. The framework is expected to
    flip this on by default once live-tested across the supported clusters;
    until then it stays opt-in so demos can adopt it before the rollout.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterable

__all__ = [
    "_resolve_ssh_persist_interval",
    "_rsync_rsh_env",
    "_scp_binary",
    "_ssh_add_binary",
    "_ssh_binary",
    "_ssh_config_override_opts",
    "_ssh_multiplex_opts",
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

    On Windows the ``RSYNC_RSH`` command also carries the
    :func:`_ssh_multiplex_opts` override (``-o ControlMaster=no -o
    ControlPath=none``) so rsync's own ssh can't pick up the user's
    ``~/.ssh/config`` multiplexing — which native Windows OpenSSH cannot
    honour — any more than the bare ``ssh_run`` call site can.
    """
    if os.environ.get("RSYNC_RSH"):
        return {}
    ssh = _ssh_binary()
    if sys.platform == "win32":
        return {"RSYNC_RSH": " ".join([ssh, *_ssh_multiplex_opts()])}
    if ssh == "ssh":
        return {}
    return {"RSYNC_RSH": ssh}


# Default ControlPersist window. Tunable via ``HPC_SSH_PERSIST_INTERVAL``;
# see :func:`_ssh_multiplex_opts` for the accepted shapes.
_DEFAULT_SSH_PERSIST_INTERVAL = "10m"

# Characters that must never appear in the persist-interval env var. OpenSSH
# accepts plain ints (seconds), suffixed durations (``30m``, ``2h``), ``0``
# (persist until master exits), and ``no``/``yes``; none of those need any
# of these chars, so any occurrence indicates a typo or an injection attempt.
_DISALLOWED_PERSIST_CHARS = " \t\n\r;|&`$<>\"'\\*?!()=/"


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

    The ``HPC_SSH_NAMED_PIPE=1`` opt-in (which makes
    :func:`_ssh_multiplex_opts` emit a named-pipe ``ControlPath`` on Windows)
    is *deliberately* not honoured here: one-shot transfers gain nothing from
    being a multiplex client, so we keep the override and skip the master-setup
    overhead even when long-lived ssh sessions are multiplexed.
    """
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1":
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
        to bite; a command-line ``-o`` beats the config file.
        ``HPC_NO_SSH_MULTIPLEX=1`` still short-circuits to ``[]`` first —
        it wins over the ``HPC_SSH_NAMED_PIPE`` opt-in below as well.
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
    ``HPC_SSH_NAMED_PIPE=1`` *(opt-in, Windows-only)*
        Enable multiplexing on native Windows OpenSSH via a named-pipe
        ``ControlPath`` of the form ``\\\\.\\pipe\\openssh-hpc-cm-%C``
        (OpenSSH computes the ``%C`` token — connection-tuple hash — at
        runtime; ``%C`` substitution does work inside the named-pipe path
        on Windows OpenSSH ≥ 8.x, which is what makes this opt-in viable).
        Without the opt-in, the Windows branch keeps emitting the legacy
        ``ControlMaster=no`` / ``ControlPath=none`` override and pays a
        fresh handshake per call — ~1-2 seconds each, hundreds of seconds
        per submit/monitor/aggregate cycle. The opt-in is validated against
        OpenSSH ≥ 8.x on Windows (the version that introduced named-pipe
        ``ControlPath`` support); the framework will likely flip the
        default after live-cluster validation. The ``HPC_NO_SSH_MULTIPLEX=1``
        short-circuit above still wins. ``ControlPersist`` is honoured
        exactly as on POSIX. Note this only affects :func:`_ssh_multiplex_opts`:
        the transfer-sized :func:`_ssh_config_override_opts` (used by ``scp``
        and the tar-fallback push) stays as ``ControlMaster=no`` /
        ``ControlPath=none`` even with the opt-in on — one-shot transfers
        don't benefit from being a multiplex client.
    """
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1":
        return []
    if sys.platform == "win32":
        if os.environ.get("HPC_SSH_NAMED_PIPE") == "1":
            # Opt-in: native Windows OpenSSH ≥ 8.x supports a named-pipe
            # ControlPath. The ``\\.\pipe\<name>`` namespace is the Windows
            # equivalent of a Unix domain socket; %C is substituted by ssh
            # at runtime (verified working on Windows OpenSSH ≥ 8.x), so
            # different hosts/users/ports get isolated masters automatically.
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
        # Legacy Windows default: native Windows OpenSSH can't use a
        # ControlPath Unix socket (getsockname failed: Not a socket), so
        # instead of POSIX multiplexing emit the explicit
        # ControlMaster=no/ControlPath=none override (see
        # _ssh_config_override_opts) — returning [] would only omit OUR
        # flags and let a user's ~/.ssh/config ControlMaster bite.
        return _ssh_config_override_opts()
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
      ``[<ssh>, -o BatchMode=yes, *_ssh_multiplex_opts()]``. ``BatchMode``
      fails fast on a missing key instead of hanging on a prompt;
      :func:`_ssh_multiplex_opts` reuses one connection on POSIX and applies
      the ControlMaster override on Windows.
    * ``"scp"`` — one-shot transfer: ``[<scp>, -o BatchMode=yes,
      *_ssh_config_override_opts()]``. A transfer needn't be a multiplex
      master, but on Windows it still must neutralise the user's ssh-config
      ControlMaster, so it gets the override only (nothing on POSIX).
    * ``"ssh-add"`` — local agent probe, no remote host: ``[<ssh-add>]``
      (no BatchMode, no multiplexing).

    rsync is env-based, not argv-based — see :func:`ssh_env`.
    """
    if kind == "ssh":
        return [_ssh_binary(), "-o", "BatchMode=yes", *_ssh_multiplex_opts(), *extra_opts]
    if kind == "scp":
        return [_scp_binary(), "-o", "BatchMode=yes", *_ssh_config_override_opts(), *extra_opts]
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
