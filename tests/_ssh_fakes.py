"""A shared, stateful ``ssh_run`` fake with ack-sentinel awareness.

Most of the suite fakes the ``remote.ssh_run`` seam by hand — a per-file closure
or a small scripted class (``tests/ops/monitor/test_watcher_install.py::_ScriptedSSH``
is the canonical shape: substring rules, first-match-wins, every command
recorded). Each such closure also has to hand-write the positive-evidence
sentinel a real remote command echoes (``__HPC_SCHED_ACK__=0``, ``__HPC_WAVE_ACK__``,
…) so the seam under test believes the channel ran to completion. That sentinel
line is boilerplate, and getting it wrong (wrong prefix, missing ``=$?`` rc,
forgotten newline) is exactly the mistake these tests exist to catch.

This module hoists that shape into one importable, stateful fake and — the point
of the upgrade — makes it *ack-aware*: :class:`FakeSSH` scans each outgoing
command for the ``__HPC_*_ACK__`` echo the production code appended (via
:func:`hpc_agent.infra.ssh_validation.wrap_with_ack` or a bare ``printf`` token)
and appends the matching sentinel to the reply automatically, carrying the rule's
return code. A test states the *result* (rc, stdout); it never re-types the
sentinel the transport contract already pins.

ADDITIVE by design: this does not replace the existing per-file closures — they
keep working untouched, and callers migrate to :class:`FakeSSH` incrementally
(the reuse ledger's no-flag-day rule). Import it beside the other root-level
shared helpers::

    from tests._ssh_fakes import FakeSSH, Reply, SCHED_ACK, completed

The ``__HPC_*_ACK__`` convention is uniform across the suite; the prefixes below
mirror the production constants (``_engine._SCHED_ACK_PREFIX``,
``cluster_status._STATUS_ACK_PREFIX``, ``runner._OUTPUTS_ACK_PREFIX``,
``announce._ANNOUNCE_ACK``, ``reconcile._WAVE_ACK``) so a drift in either surfaces
as a test failure rather than a silent divergence.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# ── the uniform ack-sentinel vocabulary ──────────────────────────────────────
# rc-carrying prefixes (``wrap_with_ack`` form: ``…; echo "<prefix>$?"``). The
# trailing ``=`` is part of the prefix, matching the production constants and
# ``split_ack``'s parse contract.
SCHED_ACK = "__HPC_SCHED_ACK__="
STATUS_ACK = "__HPC_STATUS_ACK__="
OUTPUTS_ACK = "__HPC_OUTPUTS_ACK__="
TEST_ACK = "__HPC_TEST_ACK__="
# affirmation-only tokens (bare ``printf`` form: presence proves the command
# reached the echo; they carry no rc).
ANNOUNCE_ACK = "__HPC_ANNOUNCE_ACK__"
WAVE_ACK = "__HPC_WAVE_ACK__"

# Any ``__HPC_…_ACK__`` token appearing in an outgoing command. Used to detect
# which sentinel the production code expects echoed back.
_ACK_TOKEN_RE = re.compile(r"__HPC_[A-Z0-9_]*?ACK__")


def completed(
    stdout: str = "",
    *,
    stderr: str = "",
    returncode: int = 0,
    args: object = "",
) -> subprocess.CompletedProcess[str]:
    """A ``CompletedProcess`` builder — the shared shape of every fake's reply."""
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def ack_line(prefix: str, rc: int = 0) -> str:
    r"""The exact sentinel line a real remote shell would echo for *prefix*.

    An ``=``-terminated *prefix* (the :func:`wrap_with_ack` form) carries *rc*:
    ``ack_line(SCHED_ACK, 1) == "__HPC_SCHED_ACK__=1\n"``. A bare token (the
    ``printf`` affirmation form) is echoed alone: ``ack_line(WAVE_ACK)``.
    """
    if prefix.endswith("="):
        return f"{prefix}{rc}\n"
    return f"{prefix}\n"


@dataclass
class Reply:
    """A single rule's response: an rc + optional stdout/stderr.

    ``auto_ack`` (default) lets :class:`FakeSSH` append whatever ``__HPC_*_ACK__``
    sentinel the *command* asked for, carrying :attr:`returncode`. Set it False
    for the deliberate channel-silence / truncation cases a test wants to model
    (an rc-0 but ack-LESS read — the UNKNOWN signal).
    """

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    auto_ack: bool = True


# A rule's response may be a ready :class:`Reply`, a bare ``CompletedProcess``,
# or a callable given ``(cmd, store)`` for stateful behaviour (crontab-style
# read-modify-write over :attr:`FakeSSH.store`).
_Response = Union[Reply, subprocess.CompletedProcess, "Callable[[str, dict], object]"]
_Rule = tuple[str, _Response]


@dataclass
class FakeSSH:
    """A stateful, ack-aware stand-in for ``remote.ssh_run``.

    Rules are ``(substring, response)``; the first whose substring is in the
    command wins (order rules so a longer needle precedes a substring of it —
    ``"scrontab -l"`` before ``"crontab -l"``). An unmatched command succeeds
    empty. Every dispatched command is recorded on :attr:`sent`.

    A *response* is a :class:`Reply`, a bare ``CompletedProcess`` (passed through
    verbatim, no auto-ack), or ``callable(cmd, store) -> Reply | CompletedProcess``
    for stateful rules. :attr:`store` is a plain dict those callables read and
    mutate to model remote state across calls.

    When a :class:`Reply` opts into ``auto_ack``, the fake appends the sentinel
    the command actually requested — every ``__HPC_*_ACK__`` token found in the
    outgoing command that is not already present in the reply's stdout — carrying
    the reply's rc for the ``=$?`` form. So a test states the result and the
    positive-evidence contract is honoured for free.

    Signature-compatible with ``ssh_run``: ``monkeypatch.setattr(mod, "ssh_run", fake)``.
    """

    rules: list[_Rule] = field(default_factory=list)
    store: dict = field(default_factory=dict)
    sent: list[str] = field(default_factory=list)

    def rule(self, needle: str, response: _Response) -> FakeSSH:
        """Append a rule; returns self so construction can chain."""
        self.rules.append((needle, response))
        return self

    def __call__(
        self,
        cmd: str,
        *,
        ssh_target: str,
        capture: bool = True,
        timeout: object = None,
        op: str | None = None,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        self.sent.append(cmd)
        response = self._match(cmd)
        if callable(response) and not isinstance(response, subprocess.CompletedProcess):
            response = response(cmd, self.store)
        if isinstance(response, subprocess.CompletedProcess):
            # A raw CompletedProcess is passed through exactly — the caller took
            # full control of the bytes, including any sentinel.
            return response
        return self._render(cmd, response)

    def _match(self, cmd: str) -> _Response:
        for needle, response in self.rules:
            if needle in cmd:
                return response
        return Reply()  # default: rc-0, empty, auto-acked

    def _render(self, cmd: str, reply: Reply) -> subprocess.CompletedProcess[str]:
        stdout = reply.stdout
        if reply.auto_ack:
            stdout += self._acks_for(cmd, stdout, reply.returncode)
        return completed(stdout, stderr=reply.stderr, returncode=reply.returncode)

    def _acks_for(self, cmd: str, stdout: str, rc: int) -> str:
        """Sentinel lines for every ``__HPC_*_ACK__`` the command requested that
        the reply did not already emit — each carrying *rc* for the rc form."""
        extra = ""
        for token in dict.fromkeys(_ACK_TOKEN_RE.findall(cmd)):  # dedupe, keep order
            if token in stdout:
                continue
            if f"{token}=$?" in cmd:  # wrap_with_ack rc-carrying form
                extra += f"{token}={rc}\n"
            else:  # bare printf affirmation token
                extra += f"{token}\n"
        return extra

    # -- assertion helpers (mirror _ScriptedSSH.dispatched) ---------------
    def dispatched(self, needle: str) -> list[str]:
        """Every recorded command containing *needle* — for order/shape asserts."""
        return [c for c in self.sent if needle in c]

    def sent_once(self, needle: str) -> str:
        """The single dispatched command containing *needle* (asserts uniqueness)."""
        hits = self.dispatched(needle)
        assert len(hits) == 1, f"expected exactly one {needle!r} command, got {len(hits)}"
        return hits[0]


def stateful_crontab(*, key: str = "crontab") -> Callable[[str, dict], subprocess.CompletedProcess]:
    """A stateful callable rule modelling a remote crontab read-modify-write.

    A single in-memory table lives at ``store[key]`` (``None`` → user has no
    crontab). ``crontab -l`` reflects it; a piped ``| crontab -`` install
    captures the new body from the command. Demonstrates the stateful-rule shape
    (mirrors ``test_doctor_install.py::_FakeCrontab``, but over the ssh_run string
    seam rather than an argv seam).
    """

    def _rule(cmd: str, store: dict) -> subprocess.CompletedProcess[str]:
        body = store.get(key)
        if "crontab -l" in cmd:
            if body is None:
                return completed("no crontab for user\n", returncode=1)
            return completed(body, returncode=0)
        if "crontab -" in cmd:
            store[key] = cmd  # the installer's write; body is the command itself
            return completed(returncode=0)
        return completed(returncode=0)

    return _rule


def rules(*pairs: Iterable[object]) -> list[_Rule]:
    """Sugar: ``rules(("crontab -l", Reply(rc=1)), …)`` → a rule list."""
    return [(str(needle), resp) for needle, resp in pairs]  # type: ignore[misc]
