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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
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


# ── exec/dial counting instrumentation (latency-elimination Unit 1.0) ─────────
# Landed ONCE here so every Wave-1/2 counting unit reads the SAME shapes
# read-only (ARCHITECT-MEMO §12). A missing counter shape goes to the
# integrator — it is never grown on a unit branch. The three named counters:
#
#   * exec  — one remote command execution (one ``ssh_run`` call).
#   * dial  — one contact with a host. At this seam (the per-command ``ssh_run``
#             boundary) the cold-dial-per-op baseline makes an exec and a dial
#             coincide, so a dial is counted per exec and the useful lever is the
#             per-host breakdown (:attr:`FakeSSH.dials_by_host`) plus the window
#             helpers (:meth:`FakeSSH.mark` / :meth:`FakeSSH.execs_since`) that
#             let a test assert "zero intervening dials" across a wait.
#   * pull-cycle — one transport pull round-trip (one ``rsync_pull`` /
#             ``tar_ssh_pull`` call), counted by :class:`FakePull`.


@dataclass(frozen=True)
class Call:
    """One recorded ``ssh_run`` dispatch — command + the host it dialed + op tag.

    :attr:`FakeSSH.sent` keeps the flat command-string history the existing
    assertion helpers use; :attr:`FakeSSH.calls` is the structured parallel the
    exec/dial counters read (``sent`` and ``calls`` stay length-locked).
    """

    cmd: str
    ssh_target: str
    op: str | None = None


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
    calls: list[Call] = field(default_factory=list)

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
        self.calls.append(Call(cmd=cmd, ssh_target=ssh_target, op=op))
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

    # -- exec / dial counters (read-only; consumed by the counting units) ----
    @property
    def exec_count(self) -> int:
        """Total remote command executions dispatched so far (one per call)."""
        return len(self.calls)

    @property
    def dials_by_host(self) -> Counter[str]:
        """Per-host dial breakdown — ``{ssh_target: n}``.

        The "1/host" fleet assertions read this: a fold that collapses a
        per-task/per-run fan-out to one contact per host shows up as each host's
        count dropping to 1.
        """
        return Counter(c.ssh_target for c in self.calls)

    @property
    def execs_by_op(self) -> Counter[str | None]:
        """Per-``op`` exec breakdown — ``{op_label: n}`` (``None`` = untagged)."""
        return Counter(c.op for c in self.calls)

    def mark(self) -> int:
        """Snapshot the current exec count, for a window measurement.

        Pair with :meth:`execs_since` to assert "zero intervening dials" across a
        wait: ``m = fake.mark(); …wait…; assert fake.execs_since(m) == 0``.
        """
        return len(self.calls)

    def execs_since(self, mark: int) -> int:
        """How many execs (dials) landed since *mark* was taken."""
        return len(self.calls) - mark


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


# ── pull-cycle counting instrumentation (latency-elimination Unit 1.0) ────────


@dataclass(frozen=True)
class PullCall:
    """One recorded transport pull round-trip.

    Mirrors the keyword surface of ``infra.transport.rsync_pull`` /
    ``tar_ssh_pull`` — the seam the fingerprint/harvest pulls go through (NOT
    ``ssh_run``). ``include`` is captured as a tuple so :class:`PullCall` stays
    hashable/comparable in assertions.
    """

    ssh_target: str
    remote_path: str
    remote_subdir: str
    local_dir: str
    include: tuple[str, ...] = ()


@dataclass
class FakePull:
    """A counting stand-in for ``infra.transport.rsync_pull`` (the pull seam).

    Each call records a :class:`PullCall` and returns success by default, so a
    test can assert the *pull-cycle* count — e.g. the double-canary fold pulls
    both samples in ONE cycle (2 round-trips, not 4). Signature-compatible with
    ``rsync_pull``: ``monkeypatch.setattr(mod, "rsync_pull", FakePull(...))``.

    A pull often has to *materialise* the file the caller then reads (a bare
    rc-0 with no file on disk is the caller's "nothing pulled" raise, which is a
    different case). Supply *responder* — ``callable(PullCall, store) ->
    CompletedProcess | None`` — to write fixture files and/or return a custom
    result; the default responder ``mkdir -p``'s *local_dir* (as the real
    ``rsync_pull`` does) and returns rc-0. :attr:`store` is a plain dict the
    responder may thread state through.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    responder: Callable[[PullCall, dict], object] | None = None
    store: dict = field(default_factory=dict)
    pulls: list[PullCall] = field(default_factory=list)

    def __call__(
        self,
        *,
        ssh_target: str,
        remote_path: str,
        remote_subdir: str,
        local_dir: str | Path,
        include: list[str] | None = None,
        timeout: object = None,
        **_kw: object,
    ) -> subprocess.CompletedProcess[str]:
        call = PullCall(
            ssh_target=ssh_target,
            remote_path=remote_path,
            remote_subdir=remote_subdir,
            local_dir=str(local_dir),
            include=tuple(include or ()),
        )
        self.pulls.append(call)
        if self.responder is not None:
            result = self.responder(call, self.store)
            if isinstance(result, subprocess.CompletedProcess):
                return result
        # Default: behave like a successful rsync_pull — the dest dir exists.
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return completed(self.stdout, stderr=self.stderr, returncode=self.returncode)

    @property
    def pull_count(self) -> int:
        """Total pull round-trips dispatched (the pull-cycle primitive)."""
        return len(self.pulls)

    def pulls_to(self, ssh_target: str) -> list[PullCall]:
        """Every recorded pull that dialed *ssh_target*."""
        return [p for p in self.pulls if p.ssh_target == ssh_target]
