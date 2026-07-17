"""U5 breaker/slot uniformity — every standalone transfer-plane dial rides the
breaker + slot via ``_guarded_ssh_bounded`` (BR-12, transport-robustness AUDIT §6/§9).

Two families of pin per converted site:

* **Rides the guarded path** — the site funnels its dial through
  :func:`hpc_agent.infra.transport.guarded_call` (proven by spying that seam and
  introspecting the ``functools.partial`` it receives — no real ssh spawned).
  The captured ``remote_cmd`` is asserted byte-identical to the pre-U5 command
  string (the wrapper is a pure pass-through; guarding does NOT mutate the
  command, its timeout, or its ``what``).
* **Documented degradation under a breaker-open** — with ``guarded_call``
  patched to raise :class:`~hpc_agent.errors.SshCircuitOpen` /
  :class:`~hpc_agent.errors.SshSlotWaitTimeout`, a FAIL-OPEN dial degrades to
  its documented None/skip (never a new raise) and a FAIL-LOUD dial re-raises
  the same typed error its callers already classify.

The stage-swap legs inside ``_tar_ssh_push`` are the NAMED EXEMPTION (they ride
the enclosing ``guarded_call`` the push already holds; a second wrap would
self-deadlock the N=2 slot) — enforced by the AST contract test in
``tests/contracts/test_src_subprocess_timeout_discipline.py``.
"""

from __future__ import annotations

import base64
import functools
import shlex
import subprocess

import pytest

from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
from hpc_agent.infra import transport
from hpc_agent.infra.transport import _delta, _prune

_RAISERS = [
    pytest.param(lambda: SshCircuitOpen("login.test: circuit open"), id="circuit-open"),
    pytest.param(lambda: SshSlotWaitTimeout("login.test: slot wait timed out"), id="slot-timeout"),
]


class _GuardSpy:
    """Stand-in for ``guarded_call`` that records the call and (by default)
    returns a canned OK ``CompletedProcess`` WITHOUT dialing — so a site's dial
    is proven to funnel through the seam while nothing touches the network."""

    def __init__(self, *, raise_factory=None, stdout: str = "") -> None:
        self.calls: list[tuple[str, functools.partial]] = []
        self._raise_factory = raise_factory
        self._stdout = stdout

    def __call__(self, ssh_target, fn, **_kw):  # noqa: ANN001 — mirrors guarded_call
        self.calls.append((ssh_target, fn))
        if self._raise_factory is not None:
            raise self._raise_factory()
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=self._stdout, stderr="")

    @property
    def only_cmd(self) -> str:
        """The ``remote_cmd`` (2nd positional of the ``_ssh_bounded`` partial)
        of the single recorded call."""
        assert len(self.calls) == 1, self.calls
        _target, fn = self.calls[0]
        assert fn.func is transport._ssh_bounded
        cmd = fn.args[1]
        assert isinstance(cmd, str)
        return cmd


# ── the shared wrapper is a pure pass-through (byte-unchanged command) ──────────


def test_guarded_ssh_bounded_passes_command_and_kwargs_verbatim() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        transport._guarded_ssh_bounded("u@h", "echo hello world", timeout=5, what="probe")
    target, fn = spy.calls[0]
    assert target == "u@h"
    assert fn.func is transport._ssh_bounded
    # ssh_target + remote_cmd verbatim; timeout + what verbatim — guarding adds
    # NOTHING to the dial the site would have made bare.
    assert fn.args == ("u@h", "echo hello world")
    assert fn.keywords == {"timeout": 5, "what": "probe"}


# ── rides-the-guarded-path pins (command byte-unchanged) ───────────────────────


def test_push_run_sidecar_rides_guarded_call_command_unchanged() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        transport.push_run_sidecar(ssh_target="u@h", remote_path="/r", run_id="rid", content="{}")
    b64 = base64.b64encode(b"{}").decode("ascii")
    expected = (
        f"cd {shlex.quote('/r')} && mkdir -p .hpc/runs && "
        f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote('.hpc/runs/rid.json')}"
    )
    assert spy.only_cmd == expected
    assert spy.calls[0][0] == "u@h"


def test_write_deploy_manifest_rides_guarded_call_command_unchanged() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        transport._write_deploy_manifest(ssh_target="u@h", remote_path="/r", content="M")
    b64 = base64.b64encode(b"M").decode("ascii")
    expected = (
        f"cd {shlex.quote('/r')} && mkdir -p .hpc && "
        f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(transport._DEPLOY_MANIFEST_REL)}"
    )
    assert spy.only_cmd == expected


def test_delta_manifest_read_rides_guarded_call() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        _delta._remote_push_manifest(ssh_target="u@h", remote_path="/r", exclude=[], timeout=30)
    # The remote hash snippet: cd + base64-piped python3, byte-unchanged shape.
    cmd = spy.only_cmd
    assert cmd.startswith(f"cd {shlex.quote('/r')} && printf %s ")
    assert "| base64 -d | HPC_DELTA_EXCLUDES=" in cmd
    assert cmd.rstrip().endswith("python3")


def test_write_push_manifest_rides_guarded_call() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        _prune._write_push_manifest(ssh_target="u@h", remote_path="/r", paths=["a"], timeout=30)
    cmd = spy.only_cmd
    assert cmd.startswith(f"cd {shlex.quote('/r')} && mkdir -p .hpc && printf %s ")
    assert "HPC_PM_PAYLOAD=" in cmd and cmd.rstrip().endswith("python3")


def test_prune_and_reseal_rides_guarded_call() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        _prune._prune_and_reseal(
            ssh_target="u@h", remote_path="/r", prune_paths=["x"], seal_paths=["a"], timeout=30
        )
    cmd = spy.only_cmd
    assert cmd.startswith(f"cd {shlex.quote('/r')} && mkdir -p .hpc && printf %s ")
    assert "HPC_PM_PAYLOAD=" in cmd and cmd.rstrip().endswith("python3")


def test_read_prior_push_manifest_rides_guarded_call() -> None:
    spy = _GuardSpy(stdout='{"paths": ["a", "b"]}')
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        out = _prune._read_prior_push_manifest(ssh_target="u@h", remote_path="/r", timeout=30)
    assert out == {"a", "b"}
    assert spy.only_cmd == f"cat {shlex.quote('/r/.hpc/.push_manifest.json')} 2>/dev/null"


def test_deploy_transfer_tar_fallback_rides_guarded_call() -> None:
    spy = _GuardSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        mp.setattr(transport, "_have_rsync", lambda: False)
        transport._deploy_transfer(ssh_target="u@h", remote_path="/remote/path", items=[])
    assert len(spy.calls) == 1
    target, fn = spy.calls[0]
    assert target == "u@h"
    # The tar fallback dial rides guarded_call as a _tar_ssh_push partial, its
    # params byte-unchanged (delete=False, no stage-swap legs fire).
    assert fn.func is transport._tar_ssh_push
    assert fn.keywords["delete"] is False
    assert fn.keywords["ssh_target"] == "u@h"
    assert fn.keywords["remote_path"] == "/remote/path"


# ── breaker-open degradation: FAIL-OPEN sites (None / skip, never a new raise) ──


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_delta_manifest_read_breaker_open_returns_none(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        out = _delta._remote_push_manifest(
            ssh_target="u@h", remote_path="/r", exclude=[], timeout=30
        )
    assert out == (None, set())  # routes to the (guarded) full-copy fallback


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_read_prior_push_manifest_breaker_open_returns_empty(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        out = _prune._read_prior_push_manifest(ssh_target="u@h", remote_path="/r", timeout=30)
    assert out == set()  # unprovable manifest → every extra is an ANOMALY (fail-open)


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_write_push_manifest_breaker_open_is_fail_open(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        # No exception escapes — a lost checkpoint/seal only lags the next push.
        _prune._write_push_manifest(ssh_target="u@h", remote_path="/r", paths=["a"], timeout=30)
    assert len(spy.calls) == 1  # it DID try the guarded dial


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_prune_and_reseal_breaker_open_is_fail_open(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        _prune._prune_and_reseal(
            ssh_target="u@h", remote_path="/r", prune_paths=["x"], seal_paths=["a"], timeout=30
        )
    assert len(spy.calls) == 1


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_write_deploy_manifest_breaker_open_is_fail_open(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        transport._write_deploy_manifest(ssh_target="u@h", remote_path="/r", content="M")
    assert len(spy.calls) == 1


# ── breaker-open degradation: FAIL-LOUD sites (re-raise the typed error) ────────


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_push_run_sidecar_breaker_open_raises(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    expected = type(raise_factory())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        with pytest.raises(expected):
            transport.push_run_sidecar(
                ssh_target="u@h", remote_path="/r", run_id="rid", content="{}"
            )


@pytest.mark.parametrize("raise_factory", _RAISERS)
def test_deploy_transfer_tar_fallback_breaker_open_raises(raise_factory) -> None:
    spy = _GuardSpy(raise_factory=raise_factory)
    expected = type(raise_factory())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(transport, "guarded_call", spy)
        mp.setattr(transport, "_have_rsync", lambda: False)
        with pytest.raises(expected):
            transport._deploy_transfer(ssh_target="u@h", remote_path="/remote/path", items=[])
