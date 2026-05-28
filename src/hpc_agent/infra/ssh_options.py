"""SSH option-building helpers (ControlPersist multiplexing, persist interval).

Extracted from :mod:`hpc_agent.infra.remote` so the remote-IO module can
stay focused on subprocess plumbing. The helpers here are pure config —
they consult environment variables and return the SSH option list that
``ssh_run`` / ``rsync_push`` splice into their argv.

Re-exported from :mod:`hpc_agent.infra.remote` for backwards
compatibility (the names are underscore-prefixed and were not part of
the public ``__all__``, but external callers may still reach for them
on private internals).
"""

from __future__ import annotations

import os
import sys
import tempfile

__all__ = ["_resolve_ssh_persist_interval", "_ssh_multiplex_opts"]


# Default ControlPersist window. Tunable via ``HPC_SSH_PERSIST_INTERVAL``;
# see :func:`_ssh_multiplex_opts` for the accepted shapes.
_DEFAULT_SSH_PERSIST_INTERVAL = "10m"

# Characters that must never appear in the persist-interval env var. OpenSSH
# accepts plain ints (seconds), suffixed durations (``30m``, ``2h``), ``0``
# (persist until master exits), and ``no``/``yes``; none of those need any
# of these chars, so any occurrence indicates a typo or an injection attempt.
_DISALLOWED_PERSIST_CHARS = " \t\n\r;|&`$<>\"'\\*?!()=/"


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
        Multiplexing is also auto-disabled on Windows because the
        ``ControlPath`` Unix socket isn't supported by native Windows
        OpenSSH (``ssh.exe`` aborts with ``getsockname failed: Not a
        socket``).
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
    """
    if os.environ.get("HPC_NO_SSH_MULTIPLEX") == "1":
        return []
    if sys.platform == "win32":
        return []
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
