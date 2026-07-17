"""Breaker + slot-limiter drills — 'every breaker / deadline actually FIRES'.

The AUDIT (§4) confirms the READ channels are almost universally safe; the
protection layers that must be proven to FIRE under injected fault are the
circuit breaker and the cross-process slot limiter.

Audit rows drilled (§7):
  * "3 consecutive connect failures" → breaker opens; next attempt fast-fails;
    half-open probe closes it.
  * "Slot exhaustion (N=2 held) under a 3rd acquirer" → bounded 120 s wait then
    ``SshSlotWaitTimeout``.

Injection point is ``guarded_call`` — the ONE breaker+slot seam every ssh dial
funnels through. We inject a severing ``fn`` (a connection-marked result) and
assert the BREAKER changes the observable outcome, not any state-file internal.
Time is driven by ``FakeClock`` — no real sleeps.
"""

from __future__ import annotations

import pytest

from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
from hpc_agent.infra import ssh_circuit, ssh_slots

from .conftest import FakeClock, proc

HOST = "login.faultinject.edu"
TARGET = f"user@{HOST}"


def _severed_proc():
    """A CompletedProcess that ``classify_connection_failure`` reads as a
    connection-level failure (the breaker's trip evidence)."""
    return proc(255, stderr="ssh: connect to host login port 22: Connection refused")


def test_breaker_opens_after_threshold_and_fast_fails(fake_clock: FakeClock) -> None:
    """AUDIT §7 '3 consecutive connect failures' → breaker OPENS, the next dial
    fast-fails with ``SshCircuitOpen`` WITHOUT invoking ``fn`` (no new connection
    the intrusion filter would count).
    """
    calls = {"n": 0}

    def severing_fn():
        calls["n"] += 1
        return _severed_proc()

    # Drive exactly CIRCUIT_THRESHOLD consecutive connection failures through the
    # real breaker seam.
    for _ in range(ssh_circuit.CIRCUIT_THRESHOLD):
        guarded = ssh_circuit.guarded_call(
            TARGET, severing_fn, clock=fake_clock, sleep=fake_clock.sleep
        )
        assert guarded.returncode == 255  # each attempt reached fn and failed
    reached_before = calls["n"]
    assert reached_before == ssh_circuit.CIRCUIT_THRESHOLD

    # The breaker is now OPEN: the next guarded_call must fast-fail and NEVER call fn.
    with pytest.raises(SshCircuitOpen):
        ssh_circuit.guarded_call(TARGET, severing_fn, clock=fake_clock, sleep=fake_clock.sleep)
    assert calls["n"] == reached_before  # fn was NOT invoked behind the open breaker


def test_breaker_half_open_probe_recovers(fake_clock: FakeClock) -> None:
    """AUDIT §7 half-open: after the cooldown, ONE probe is allowed and a SUCCESS
    closes the circuit — recovery actually fires, the breaker is not a one-way latch.
    """
    for _ in range(ssh_circuit.CIRCUIT_THRESHOLD):
        ssh_circuit.guarded_call(TARGET, _severed_proc, clock=fake_clock, sleep=fake_clock.sleep)
    with pytest.raises(SshCircuitOpen):
        ssh_circuit.guarded_call(TARGET, _severed_proc, clock=fake_clock, sleep=fake_clock.sleep)

    # Wait out the cooldown, then let the single half-open probe succeed.
    fake_clock.advance(ssh_circuit.BASE_COOLDOWN_SEC + 1.0)
    ok = ssh_circuit.guarded_call(
        TARGET, lambda: proc(0, stdout="ok"), clock=fake_clock, sleep=fake_clock.sleep
    )
    assert ok.returncode == 0

    # Circuit is closed again: a subsequent success proceeds without SshCircuitOpen.
    ok2 = ssh_circuit.guarded_call(
        TARGET, lambda: proc(0, stdout="ok"), clock=fake_clock, sleep=fake_clock.sleep
    )
    assert ok2.returncode == 0


def test_slot_exhaustion_wait_deadline_fires(fake_clock: FakeClock) -> None:
    """AUDIT §7 'Slot exhaustion (N=2 held) under a 3rd acquirer' → the bounded
    ``SLOT_WAIT_MAX_SEC`` wait actually EXPIRES with ``SshSlotWaitTimeout`` — the
    deadline fires (driven by FakeClock, no real 120 s wait) instead of wedging.
    """
    assert ssh_slots.resolve_max_connections() == 2  # the N=2 default under test
    alive = lambda _pid: True  # noqa: E731 - held slots must look alive, not reclaimable

    # Two distinct live holders claim both per-host slots.
    t1 = ssh_slots.acquire_slot(TARGET, clock=fake_clock, pid=111, pid_alive=alive)
    t2 = ssh_slots.acquire_slot(TARGET, clock=fake_clock, pid=222, pid_alive=alive)
    assert t1 is not None and t2 is not None

    # A 3rd acquirer finds no free slot; its bounded wait is driven to expiry.
    with pytest.raises(SshSlotWaitTimeout):
        ssh_slots.acquire_slot(
            TARGET, clock=fake_clock, sleep=fake_clock.sleep, pid=333, pid_alive=alive
        )
