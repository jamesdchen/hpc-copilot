"""Pure validation helpers for SSH targets, remote paths, and remote JSON.

Extracted from :mod:`hpc_agent.infra.remote` so the remote-IO module can
stay focused on the actual subprocess plumbing. The helpers here have no
I/O: they validate strings up front and parse remote stdout into typed
shapes.

Re-exported from :mod:`hpc_agent.infra.remote` for backwards
compatibility with existing callers
(``from hpc_agent.infra.remote import validate_ssh_target``).
"""

from __future__ import annotations

import json
from typing import Any

from hpc_agent import errors
from hpc_agent.errors import RemoteCommandFailed

__all__ = [
    "parse_remote_json",
    "validate_remote_path",
    "validate_ssh_target",
]

# Characters that should never appear in an ssh_target. We intentionally
# do NOT require ``@`` — bare OpenSSH aliases (``usc-discovery``) are
# first-class. We just block whitespace and shell metachars so a stray
# value can't escape into argv as a separate token or into the shell.
_DISALLOWED_TARGET_CHARS = " \t\n\r;|&`$<>\"'\\"

# Characters that must never appear in a remote path. Mirrors the
# ssh_target set with the addition of ``*`` and ``?`` (glob), ``(``/``)``
# (subshell), and ``!`` (history); excludes ``/`` (legitimate in paths).
# Whitespace is also rejected so rsync/ssh don't see two tokens.
_DISALLOWED_REMOTE_PATH_CHARS = " \t\n\r;|&`$<>\"'\\*?!()"


def validate_remote_path(remote_path: str) -> str:
    """Return *remote_path* unchanged after a strict shape check.

    Reject empty strings, leading-dash arguments (an ssh / rsync
    argument-injection vector), and shell metachars / whitespace. The
    contract is "validate up front, then trust the value verbatim on the
    wire" — both :func:`rsync_push` and :func:`rsync_pull` rely on the
    string passing through to the remote shell unquoted.

    Permissive enough for HPC paths (``/u/home/user``, ``/scratch/$USER``-
    style names are NOT allowed — interpolate before calling), strict
    enough that a tampered campaign manifest can't push a payload like
    ``/tmp; rm -rf /``.

    Raises :class:`hpc_agent.errors.SpecInvalid`.
    """
    if not isinstance(remote_path, str) or not remote_path:
        raise errors.SpecInvalid(f"remote_path must be a non-empty string, got {remote_path!r}")
    if remote_path.startswith("-"):
        raise errors.SpecInvalid(f"remote_path must not start with '-': {remote_path!r}")
    bad = sorted({c for c in _DISALLOWED_REMOTE_PATH_CHARS if c in remote_path})
    if bad:
        raise errors.SpecInvalid(
            f"remote_path contains disallowed characters {bad!r}: {remote_path!r}"
        )
    return remote_path


def validate_ssh_target(ssh_target: str) -> str:
    """Return *ssh_target* unchanged after a permissive shape check.

    Accepts both explicit ``user@host`` strings and bare OpenSSH ``Host``
    aliases (no ``@``) — anything ``ssh`` itself would accept as a
    destination. Rejects empty strings and values containing whitespace
    or shell metacharacters so a typo can't shell-inject through argv.

    Used by submit/aggregate flows to validate cluster-spec
    ``ssh_target`` fields up front, then pass the same string verbatim
    into :func:`ssh_run`, :func:`rsync_push`, etc.

    Raises :class:`hpc_agent.errors.SpecInvalid`.
    """
    if not isinstance(ssh_target, str) or not ssh_target:
        raise errors.SpecInvalid(f"ssh_target must be a non-empty string, got {ssh_target!r}")
    if ssh_target.startswith("-"):
        # OpenSSH interprets ``-oProxyCommand=...`` etc. as option flags
        # when they appear as the destination arg. Reject any
        # leading-dash target to close the argument-injection vector.
        raise errors.SpecInvalid(f"ssh_target must not start with '-': {ssh_target!r}")
    bad = [c for c in _DISALLOWED_TARGET_CHARS if c in ssh_target]
    if bad:
        raise errors.SpecInvalid(
            f"ssh_target contains disallowed characters {bad!r}: {ssh_target!r}"
        )
    return ssh_target


def parse_remote_json(stdout: str, *, source_label: str) -> dict[str, Any]:
    """Parse JSON emitted by a remote process; raise typed error on failure.

    Centralises the ``json.loads + JSONDecodeError -> RemoteCommandFailed``
    pattern that ``_ssh_status_report`` and ``_read_remote_sidecar`` both
    needed. *source_label* is interpolated into the error message so the
    caller's diagnostic still pinpoints which remote read failed.
    """
    try:
        result: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        snippet = stdout[:200]
        raise RemoteCommandFailed(
            f"{source_label} returned invalid JSON: {exc}; first 200 chars: {snippet!r}"
        ) from exc
    return result
