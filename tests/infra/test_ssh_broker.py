"""Tests for the in-process SSH connection broker (infra.ssh_broker).

The broker's contract is: reuse ONE persistent channel per host (fewer
connections = faster AND ban-safer), preserve split stdout/stderr and the
real remote exit code, gate the connection open on the circuit breaker, and
degrade to :class:`BrokerUnavailable` (never a hang, never a wrong answer)
so ``ssh_run`` can fall back to one-shot.

No cluster and no ssh: the pool's ``_spawn`` seam is pointed at a LOCAL
``/bin/sh`` (Git Bash provides it on Windows), so the full framing / reader-
thread / sentinel / rc protocol runs for real over a local shell. State
isolation for the breaker comes from the autouse ``_isolated_journal_home``
fixture.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

import pytest

from hpc_agent.infra import ssh_broker, ssh_circuit
from hpc_agent.infra.ssh_broker import BrokerUnavailable, _Pool

_SH = shutil.which("sh") or shutil.which("bash")
needs_sh = pytest.mark.skipif(_SH is None, reason="no local POSIX shell for the broker channel")


def _local_shell_spawn(_ssh_target: str) -> subprocess.Popen[bytes]:
    """Stand in for ``ssh -T host /bin/sh`` with a LOCAL shell reading stdin."""
    return subprocess.Popen(
        [_SH],  # type: ignore[list-item]
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )


@pytest.fixture
def pool() -> Iterator[_Pool]:
    p = _Pool()
    p._spawn = _local_shell_spawn
    try:
        yield p
    finally:
        p.shutdown_all()


@needs_sh
def test_stdout_stderr_and_rc_are_separated(pool: _Pool) -> None:
    """The core protocol: split streams + the real exit code over one channel."""
    r = pool.run("echo out; echo err 1>&2; exit 3", ssh_target="u@h", timeout=15)
    assert r.returncode == 3
    assert r.stdout.strip() == "out"
    assert r.stderr.strip() == "err"


@needs_sh
def test_one_connection_reused_across_commands(pool: _Pool) -> None:
    """The whole point: N commands, ONE spawned process (one handshake)."""
    spawns = {"n": 0}
    inner = pool._spawn

    def _counting(target: str) -> subprocess.Popen[bytes]:
        spawns["n"] += 1
        return inner(target)

    pool._spawn = _counting
    for i in range(5):
        r = pool.run(f"echo {i}", ssh_target="u@h", timeout=15)
        assert r.stdout.strip() == str(i)
    assert spawns["n"] == 1  # five commands, one connection


@needs_sh
def test_a_wedged_command_raises_unavailable_not_hangs(pool: _Pool) -> None:
    """A command that never returns hits the deadline → BrokerUnavailable (the
    caller then falls back), and the poisoned channel is discarded."""
    with pytest.raises(BrokerUnavailable):
        pool.run("sleep 30", ssh_target="u@h", timeout=0.5)
    # Channel discarded, so the next call re-opens and works.
    r = pool.run("echo alive", ssh_target="u@h", timeout=15)
    assert r.stdout.strip() == "alive"


@needs_sh
def test_output_containing_sentinel_prefix_is_not_confused(pool: _Pool) -> None:
    """A command whose output merely resembles the sentinel base can't spoof the
    frame — the per-command nonce makes the real end-marker unique."""
    r = pool.run("echo __HPC_BRK_OUT_; echo real", ssh_target="u@h", timeout=15)
    assert r.returncode == 0
    assert "real" in r.stdout
    assert "__HPC_BRK_OUT_" in r.stdout  # the decoy line survives as data


@needs_sh
def test_open_is_gated_by_the_circuit_breaker(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OPEN circuit refuses to open the persistent connection (no spawn)."""
    from hpc_agent.errors import SshCircuitOpen

    def _open_circuit(ssh_target: str, **_k: object) -> None:
        raise SshCircuitOpen("circuit open (test)")

    monkeypatch.setattr(ssh_circuit, "check_circuit", _open_circuit)
    spawned = {"n": 0}
    inner = pool._spawn

    def _counting(target: str) -> subprocess.Popen[bytes]:
        spawned["n"] += 1
        return inner(target)

    pool._spawn = _counting
    with pytest.raises(SshCircuitOpen):
        pool.run("echo x", ssh_target="u@h", timeout=15)
    assert spawned["n"] == 0  # never spawned against an open circuit


@needs_sh
def test_successful_open_records_breaker_success(
    pool: _Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_circuit, "record_connection_success", lambda t: recorded.append(t))
    pool.run("echo ok", ssh_target="u@h", timeout=15)
    assert "u@h" in recorded


@needs_sh
def test_idle_channel_is_reaped(pool: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle channel self-closes so no login-node session lingers."""
    # -1.0, not 0.0: with a 0.0 threshold the reap needs idle_for() STRICTLY
    # positive, and two back-to-back monotonic() reads can be EQUAL on a fast
    # Windows runner (CI flake on 51175a3b) — any real idle beats -1.0.
    monkeypatch.setattr(ssh_broker, "IDLE_CLOSE_SEC", -1.0)
    r1 = pool.run("echo one", ssh_target="u@h", timeout=15)
    assert r1.stdout.strip() == "one"
    first = pool._channels["h"]
    r2 = pool.run("echo two", ssh_target="u@h", timeout=15)  # idle>0 → reaped+reopened
    assert r2.stdout.strip() == "two"
    assert pool._channels["h"] is not first


def test_broker_disabled_by_default_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public facade refuses when the opt-in flag is unset — ssh_run then
    uses its one-shot path, i.e. today's behaviour, unchanged."""
    monkeypatch.delenv(ssh_broker.BROKER_ENV, raising=False)
    assert ssh_broker.broker_enabled() is False
    with pytest.raises(BrokerUnavailable):
        ssh_broker.broker_ssh_run("echo x", ssh_target="u@h", timeout=5)


def test_broker_enabled_flag_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv(ssh_broker.BROKER_ENV, truthy)
        assert ssh_broker.broker_enabled() is True
    for falsy in ("0", "", "off", "no"):
        monkeypatch.setenv(ssh_broker.BROKER_ENV, falsy)
        assert ssh_broker.broker_enabled() is False


def test_parse_rc_and_strip_helpers() -> None:
    from hpc_agent.infra.ssh_broker import _parse_rc, _strip_sentinel

    s = "hello\n__DONE__0\n"
    assert _parse_rc(s, "__DONE__") == 0
    assert _strip_sentinel(s, "__DONE__") == "hello"
    assert _parse_rc("no sentinel yet", "__DONE__") is None  # not terminal → keep waiting
