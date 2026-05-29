"""Lint: no call site outside ``ssh_options`` builds a bare ssh-family argv.

Binary resolution + SSH option assembly are owned by the single seam
``hpc_agent.infra.ssh_options.ssh_argv`` (#158). A bare
``["ssh"/"scp"/"ssh-add", ...]`` argv at a call site re-introduces the exact
regression class this session chased:

* it picks up Git Bash's shadowed ``/usr/bin/ssh`` etc. on Windows instead of
  the native OpenSSH binary (#145 / #156), and
* it misses the ``ControlMaster=no`` / ``ControlPath=none`` override that lets
  native Windows OpenSSH ignore a hostile ssh-config (#154).

Routing every ssh/scp/ssh-add invocation through ``ssh_argv`` makes "invoke
ssh correctly for this platform" an invariant one component owns, so a new
call site cannot silently regress.

``rsync`` is intentionally exempt: it runs the bare PATH ``rsync`` binary (not
OpenSSH-family, not Git-Bash-shadowed) and pins its *own* ssh transport via
``ssh_env()`` — the env-var twin of this seam.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"

# A list literal whose first element is a bare ssh-family binary name, e.g.
# ``["ssh", ...]`` / ``['scp', ...]`` / ``["ssh-add", ...]``. ``ssh_argv``
# returns ``[_ssh_binary(), ...]`` (a call, not a string literal), so correct
# call sites never match.
_BARE_SSH_ARGV = re.compile(r"""\[\s*["'](ssh|scp|ssh-add)["']\s*,""")

# The builder module legitimately references these names (kind dispatch /
# resolver fallbacks); it IS the seam, so it's exempt.
_EXEMPT_FILES = {"ssh_options.py"}


def test_no_bare_ssh_family_argv_outside_builder() -> None:
    offenders: list[str] = []
    for py in sorted(SRC.rglob("*.py")):
        if py.name in _EXEMPT_FILES:
            continue
        text = py.read_text(encoding="utf-8")
        for m in _BARE_SSH_ARGV.finditer(text):
            ln = text[: m.start()].count("\n") + 1
            rel = py.relative_to(SRC.parent.parent).as_posix()
            offenders.append(f"  {rel}:{ln}: {m.group(0).strip()}")

    if offenders:
        raise AssertionError(
            "bare ssh-family argv found outside the ssh_options.ssh_argv seam — "
            "route it through ssh_argv(kind=...) so native-binary resolution and "
            "the Windows ControlMaster override apply (#145/#154/#156/#158):\n"
            + "\n".join(offenders)
        )
