"""Cross-platform SSH-agent detection.

Unix convention uses ``SSH_AUTH_SOCK``; Windows OpenSSH uses a named
pipe (``\\\\.\\pipe\\openssh-ssh-agent``) and never sets the env var.
This module abstracts the detection so callers don't need to special-case.
"""

from __future__ import annotations

import os
import subprocess
import sys

__all__ = ["agent_available", "agent_detail"]


def agent_available() -> bool:
    """Return True if a usable SSH agent is reachable.

    On non-Windows: ``SSH_AUTH_SOCK`` set is sufficient (the historical
    check). On Windows: also accept a successful ``ssh-add -l`` exit,
    which talks to the Windows OpenSSH named-pipe agent regardless of
    the env var.
    """
    if os.environ.get("SSH_AUTH_SOCK"):
        return True
    if sys.platform != "win32":
        return False
    # Windows: probe the named-pipe agent via ssh-add. Return code 0 means
    # agent reachable AND has at least one key. Return code 1 means agent
    # reachable but no keys. Return code 2 means agent unreachable.
    try:
        rc = subprocess.run(
            ["ssh-add", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return rc in (0, 1)


def agent_detail() -> str:
    """Return a human-readable summary of the agent state for diagnostics."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    if sock:
        return f"SSH_AUTH_SOCK={sock}"
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return f"Windows named-pipe agent unreachable: {exc}"
        if r.returncode == 0:
            first_line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else "agent reachable"
            return f"Windows named-pipe agent at \\\\.\\pipe\\openssh-ssh-agent ({first_line})"
        if r.returncode == 1:
            return "Windows named-pipe agent reachable but no keys loaded"
        return f"Windows named-pipe agent unreachable (ssh-add rc={r.returncode})"
    return "SSH_AUTH_SOCK is not set"
