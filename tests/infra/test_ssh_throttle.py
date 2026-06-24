"""Per-host SSH connection-open throttle (the safe_interval ban-driver guard).

A cluster's fail2ban / rate-limiter counts connection *frequency*; this throttle
caps it. Tests inject sleep/clock so no test actually sleeps, and reset the
module's per-host state around each case.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_throttle


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HPC_SSH_SAFE_INTERVAL", raising=False)
    ssh_throttle.reset_throttle_state()
    yield
    ssh_throttle.reset_throttle_state()


def test_disabled_by_default():
    assert ssh_throttle.resolve_safe_interval() == 0.0


def test_resolve_reads_env(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    assert ssh_throttle.resolve_safe_interval() == 30.0


def test_empty_disables_silently(monkeypatch, capsys):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "")
    assert ssh_throttle.resolve_safe_interval() == 0.0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("bad", ["-5", "abc", "10s"])
def test_invalid_disables_and_warns(monkeypatch, capsys, bad):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", bad)
    assert ssh_throttle.resolve_safe_interval() == 0.0
    assert "HPC_SSH_SAFE_INTERVAL" in capsys.readouterr().err


def test_disabled_never_sleeps(monkeypatch):
    slept: list[float] = []
    waited = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=lambda: 1000.0)
    assert waited == 0.0
    assert slept == []


def test_first_call_does_not_wait(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    slept: list[float] = []
    waited = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=lambda: 1000.0)
    assert waited == 0.0
    assert slept == []


def test_second_call_within_interval_waits(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    slept: list[float] = []
    t = {"now": 1000.0}
    clock = lambda: t["now"]  # noqa: E731
    ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    t["now"] = 1010.0  # 10s later — within the 30s interval
    waited = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    assert waited == pytest.approx(20.0)
    assert slept == [pytest.approx(20.0)]


def test_call_after_interval_does_not_wait(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    slept: list[float] = []
    t = {"now": 1000.0}
    clock = lambda: t["now"]  # noqa: E731
    ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    t["now"] = 1040.0  # 40s later — past the interval
    waited = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    assert waited == 0.0


def test_hosts_are_independent(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    slept: list[float] = []
    clock = lambda: 1000.0  # noqa: E731
    ssh_throttle.throttle_connection("u@host-a", sleep=slept.append, clock=clock)
    waited = ssh_throttle.throttle_connection("u@host-b", sleep=slept.append, clock=clock)
    assert waited == 0.0  # different host: not throttled


def test_burst_at_one_instant_reserves_staggered_slots(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "10")
    slept: list[float] = []
    clock = lambda: 1000.0  # time does not advance: all three fire "at once"  # noqa: E731
    w1 = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    w2 = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    w3 = ssh_throttle.throttle_connection("u@h", sleep=slept.append, clock=clock)
    assert (w1, w2, w3) == (0.0, pytest.approx(10.0), pytest.approx(20.0))


def test_bare_alias_target_is_a_valid_host(monkeypatch):
    monkeypatch.setenv("HPC_SSH_SAFE_INTERVAL", "30")
    slept: list[float] = []
    t = {"now": 1000.0}
    clock = lambda: t["now"]  # noqa: E731
    ssh_throttle.throttle_connection("hoffman2", sleep=slept.append, clock=clock)
    t["now"] = 1005.0
    waited = ssh_throttle.throttle_connection("hoffman2", sleep=slept.append, clock=clock)
    assert waited == pytest.approx(25.0)
