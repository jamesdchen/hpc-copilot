"""Tests for the cross-process per-host SSH-establishment RATE limiter
(infra.ssh_pacing) — the proving-run-#15 MaxStartups-burst guard.

Time is mocked via the injectable ``clock``/``sleep`` parameters — no real
sleeps except the one coarse integration case at the bottom. State isolation
comes from the suite-wide autouse ``_isolated_journal_home`` fixture (the pacer
resolves its bucket dir through ``run_record.current_homedir``, same as the
circuit breaker and the slot limiter).

The suite-wide autouse ``_default_no_ssh_pacing`` fixture pins the limiter OFF,
so every test here re-enables it explicitly (``monkeypatch.delenv``).
"""

from __future__ import annotations

import json
import time

import pytest

from hpc_agent.infra import ssh_pacing
from hpc_agent.infra.ssh_pacing import (
    _RATE,
    NO_PACING_ENV,
    PACING_BURST,
    PACING_JITTER_FRAC,
    PACING_MAX_WAIT_SEC,
    PACING_MIN_SPACING_SEC,
    bucket_path,
    pace_establishment,
    pacing_disabled,
)

HOST = "login.cluster.edu"
TARGET = f"user@{HOST}"


class FakeClock:
    """Injectable wall clock (starts at the real epoch — state written under a
    fake clock is also read by production paths gating on ``time.time()``)."""

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _no_sleep(seconds: float) -> None:
    pytest.fail(f"unexpected sleep({seconds}): a token was available / pacing disabled")


def _recording_sleep(clock: FakeClock) -> tuple[list[float], object]:
    """A sleep stub that records its arg AND advances the fake clock (so the
    bucket refills across calls exactly as wall time would)."""
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    return slept, _sleep


@pytest.fixture(autouse=True)
def _enable_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-enable the pacer (the suite pins it off) for every test in this file."""
    monkeypatch.delenv(NO_PACING_ENV, raising=False)


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the jitter so spacing/cap waits are exact (a dedicated test below
    exercises the jitter bounds with the real fraction)."""
    monkeypatch.setattr(ssh_pacing, "PACING_JITTER_FRAC", 0.0)


# ---------------------------------------------------------------------------
# Escape hatch + no-op cases
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_escape_hatch_disables(self, monkeypatch):
        monkeypatch.setenv(NO_PACING_ENV, "1")
        assert pacing_disabled() is True
        clock = FakeClock(1000.0)
        # Even far past the burst, a disabled pacer never sleeps.
        for _ in range(PACING_BURST + 20):
            pace_establishment(TARGET, clock=clock, sleep=_no_sleep, pid=100)

    def test_unset_enables(self, monkeypatch):
        monkeypatch.delenv(NO_PACING_ENV, raising=False)
        assert pacing_disabled() is False

    @pytest.mark.parametrize("falsey", ["0", "false", "no", "off", ""])
    def test_falsey_values_keep_pacer_on(self, monkeypatch, falsey):
        monkeypatch.setenv(NO_PACING_ENV, falsey)
        assert pacing_disabled() is False

    def test_empty_host_is_noop(self):
        pace_establishment("", sleep=_no_sleep)

    def test_fail_open_on_broken_state(self, monkeypatch):
        """A wedged/broken bucket store must NEVER block SSH — pace nothing."""
        from hpc_agent.infra import io

        def _boom(*_a, **_k):
            raise OSError("state dir is broken")

        monkeypatch.setattr(io, "atomic_locked_update", _boom)
        clock = FakeClock(1000.0)
        for _ in range(PACING_BURST + 5):
            pace_establishment(TARGET, clock=clock, sleep=_no_sleep, pid=100)


# ---------------------------------------------------------------------------
# Burst allowance + steady-state spacing
# ---------------------------------------------------------------------------


class TestSpacing:
    def test_burst_allowance_goes_through_free(self, no_jitter):
        """A fresh (full) bucket admits PACING_BURST establishments with no wait."""
        clock = FakeClock(1000.0)
        slept, sleep = _recording_sleep(clock)
        for i in range(PACING_BURST):
            pace_establishment(TARGET, clock=clock, sleep=sleep, pid=100 + i)
        assert slept == []

    def test_past_burst_paces_at_min_spacing(self, no_jitter):
        clock = FakeClock(1000.0)
        slept, sleep = _recording_sleep(clock)
        # Drain the burst (free), then the next two calls pace at ~min spacing.
        for i in range(PACING_BURST + 2):
            pace_establishment(TARGET, clock=clock, sleep=sleep, pid=100 + i)
        assert len(slept) == 2
        for waited in slept:
            assert waited == pytest.approx(PACING_MIN_SPACING_SEC, abs=1e-6)

    def test_idle_refills_the_burst(self, no_jitter):
        """After the bucket drains, a quiet spell refills it back to the burst
        ceiling so a later flurry again goes through free."""
        clock = FakeClock(1000.0)
        slept, sleep = _recording_sleep(clock)
        for i in range(PACING_BURST):
            pace_establishment(TARGET, clock=clock, sleep=sleep, pid=100 + i)
        # Quiet for well over burst*spacing so the bucket is full again.
        clock.advance(PACING_BURST * PACING_MIN_SPACING_SEC + 10.0)
        for i in range(PACING_BURST):
            pace_establishment(TARGET, clock=clock, sleep=sleep, pid=200 + i)
        assert slept == []


# ---------------------------------------------------------------------------
# Jitter bounds
# ---------------------------------------------------------------------------


class TestJitter:
    def test_wait_stays_within_jitter_bounds(self):
        """Every paced wait past the burst is the base spacing ± the jitter
        fraction — deterministic per pid, never outside the band."""
        lo = PACING_MIN_SPACING_SEC * (1.0 - PACING_JITTER_FRAC)
        hi = PACING_MIN_SPACING_SEC * (1.0 + PACING_JITTER_FRAC)
        for pid in (101, 250, 500, 843, 996):
            # Fresh host per pid so each sees an empty-by-one bucket at the same
            # deficit (drain the burst first with a disabled-jitter helper).
            host = f"jit-{pid}.edu"
            target = f"u@{host}"
            path = bucket_path(host)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Seed the bucket at exactly zero tokens so the next reserve waits
            # exactly one base spacing (pre-jitter).
            path.write_text(json.dumps({"tokens": 0.0, "last_refill": 1000.0}))
            clock = FakeClock(1000.0)
            slept, sleep = _recording_sleep(clock)
            pace_establishment(target, clock=clock, sleep=sleep, pid=pid)
            assert len(slept) == 1
            assert lo - 1e-9 <= slept[0] <= hi + 1e-9


# ---------------------------------------------------------------------------
# Mirror drift pins: the shared-with-ssh_slots helpers must not regrow
# ---------------------------------------------------------------------------


class TestMirrorPins:
    def test_jitter_mod_matches_slots(self):
        """``ssh_pacing._JITTER_MOD`` is a deliberate twin of
        ``ssh_slots._JITTER_MOD`` — the two pid→jitter maps share one prime
        modulus so both guards spread back-to-back pids identically."""
        from hpc_agent.infra import ssh_slots

        assert ssh_pacing._JITTER_MOD == ssh_slots._JITTER_MOD

    def test_safe_name_matches_slots(self):
        """``ssh_pacing._safe_name`` is a deliberate twin of
        ``ssh_slots._safe_name`` — all per-host guards filename a host alike."""
        from hpc_agent.infra import ssh_slots

        for host in ("login.cluster.edu", "hoffman2.idre.ucla.edu", "weird host:22/x"):
            assert ssh_pacing._safe_name(host) == ssh_slots._safe_name(host)


# ---------------------------------------------------------------------------
# Cross-process coupling via the shared bucket file
# ---------------------------------------------------------------------------


class TestCrossProcess:
    def test_two_processes_share_one_bucket(self, no_jitter):
        """Two independent 'processes' (distinct pids, one bucket file) draw from
        the SAME token pool: process A draining the burst forces process B to
        wait — the file is what couples them (an in-process bucket could not)."""
        clock = FakeClock(1000.0)
        slept_a, sleep_a = _recording_sleep(clock)
        slept_b, sleep_b = _recording_sleep(clock)
        # Process A (pid 100) drains the whole burst, free.
        for _ in range(PACING_BURST):
            pace_establishment(TARGET, clock=clock, sleep=sleep_a, pid=100)
        assert slept_a == []
        # Process B (pid 900) now finds the shared bucket empty and must pace.
        pace_establishment(TARGET, clock=clock, sleep=sleep_b, pid=900)
        assert len(slept_b) == 1
        assert slept_b[0] == pytest.approx(PACING_MIN_SPACING_SEC, abs=1e-6)

    def test_reservation_stacks_across_processes(self, no_jitter):
        """A second empty-bucket reserver waits LONGER than the first (each
        reservation drives the shared bucket further negative) — proving the
        wait is a real cross-process queue position, not a per-call constant."""
        clock = FakeClock(1000.0)
        # Seed an empty bucket (frozen clock ⇒ no refill between the two calls).
        path = bucket_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tokens": 0.0, "last_refill": clock()}))
        slept1, sleep1 = _recording_sleep(FakeClock(clock()))  # don't advance shared clock
        slept2, sleep2 = _recording_sleep(FakeClock(clock()))
        pace_establishment(TARGET, clock=clock, sleep=sleep1, pid=100)
        pace_establishment(TARGET, clock=clock, sleep=sleep2, pid=100)
        assert slept1[0] == pytest.approx(PACING_MIN_SPACING_SEC, abs=1e-6)
        assert slept2[0] == pytest.approx(2 * PACING_MIN_SPACING_SEC, abs=1e-6)


# ---------------------------------------------------------------------------
# Wait cap: disclose and proceed (never deadlock)
# ---------------------------------------------------------------------------


class TestWaitCap:
    def test_cap_discloses_and_proceeds(self, no_jitter, capsys):
        """Past the cap the pacer sleeps at most PACING_MAX_WAIT_SEC and prints a
        'pacing cap exceeded' line — it never deadlocks a leg on the limiter."""
        clock = FakeClock(1000.0)
        # Seed a deep deficit so the reservation blows past the cap.
        path = bucket_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        deep = -(PACING_MAX_WAIT_SEC * _RATE * 5)
        path.write_text(json.dumps({"tokens": deep, "last_refill": clock()}))
        slept, sleep = _recording_sleep(clock)
        pace_establishment(TARGET, clock=clock, sleep=sleep, pid=100)
        assert len(slept) == 1
        assert slept[0] == pytest.approx(PACING_MAX_WAIT_SEC, abs=1e-6)
        assert "pacing cap exceeded" in capsys.readouterr().err

    def test_cap_floors_the_recorded_deficit(self, no_jitter):
        """The recorded backlog is floored at the cap so recovery after a storm
        is bounded by PACING_MAX_WAIT_SEC, not by the storm's depth."""
        clock = FakeClock(1000.0)
        path = bucket_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tokens": -1000.0, "last_refill": clock()}))
        slept, sleep = _recording_sleep(clock)
        pace_establishment(TARGET, clock=clock, sleep=sleep, pid=100)
        doc = json.loads(path.read_text())
        # Floored to -(cap*rate) then one more reserve is folded in by the write;
        # never deeper than the cap's worth of backlog.
        assert doc["tokens"] >= -(PACING_MAX_WAIT_SEC * _RATE) - 1e-6
        # A normal (sub-cap) reservation right after must not itself exceed the cap.
        slept2, sleep2 = _recording_sleep(clock)
        pace_establishment(TARGET, clock=clock, sleep=sleep2, pid=100)
        assert slept2[0] <= PACING_MAX_WAIT_SEC + 1e-6


# ---------------------------------------------------------------------------
# Integration: guarded_call paces the one-shot / transfer establishment seam
# ---------------------------------------------------------------------------


class TestGuardedCallIntegration:
    def test_guarded_call_paces_past_the_burst(self, no_jitter):
        """Every guarded_call spawns a fresh outbound process (a new
        establishment), so the 4th+ back-to-back call under a fixed clock paces —
        the one-shot ssh AND scp/rsync/tar all funnel through this seam."""
        import subprocess

        from hpc_agent.infra.ssh_circuit import guarded_call

        clock = FakeClock(1000.0)
        slept, sleep = _recording_sleep(clock)

        def _ok() -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args="ssh", returncode=0, stdout="", stderr="")

        for _ in range(PACING_BURST + 1):
            guarded_call(TARGET, _ok, clock=clock, sleep=sleep)
        # Burst free, then exactly one paced establishment.
        assert len(slept) == 1
        assert slept[0] == pytest.approx(PACING_MIN_SPACING_SEC, abs=1e-6)

    def test_open_circuit_skips_pacing(self, no_jitter, monkeypatch):
        """An OPEN breaker fails fast BEFORE the pacer — a refused host is never
        paced (the pace runs after the breaker gate)."""
        import subprocess

        from hpc_agent.errors import SshCircuitOpen
        from hpc_agent.infra import ssh_circuit

        calls: list[str] = []

        def _spy_pace(target, **_k):
            calls.append(target)

        # guarded_call imports ssh_pacing lazily, so patch the module attribute
        # it resolves (``from hpc_agent.infra import ssh_pacing``).
        monkeypatch.setattr(ssh_pacing, "pace_establishment", _spy_pace)

        def _raise_open(target, **_k):
            raise SshCircuitOpen("open")

        monkeypatch.setattr(ssh_circuit, "check_circuit", _raise_open)

        def _ok() -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args="ssh", returncode=0, stdout="", stderr="")

        with pytest.raises(SshCircuitOpen):
            ssh_circuit.guarded_call(TARGET, _ok)
        assert calls == []


# ---------------------------------------------------------------------------
# Coarse real-time integration (the ONE non-fake-clock case)
# ---------------------------------------------------------------------------


def test_real_sleep_spacing_is_bounded(monkeypatch):
    """One coarse wall-clock case: past the burst, a real establishment waits a
    real (small) interval and returns — the fake-clock tests own the exact math,
    this only proves the real sleep path is wired and bounded."""
    monkeypatch.delenv(NO_PACING_ENV, raising=False)
    monkeypatch.setattr(ssh_pacing, "PACING_JITTER_FRAC", 0.0)
    host = "realtime.cluster.edu"
    target = f"u@{host}"
    start = time.monotonic()
    for _ in range(PACING_BURST + 1):
        pace_establishment(target)  # real time.time / time.sleep
    elapsed = time.monotonic() - start
    # One paced call of ~PACING_MIN_SPACING_SEC; generously bounded above.
    assert PACING_MIN_SPACING_SEC * 0.5 <= elapsed <= PACING_MAX_WAIT_SEC


# ---------------------------------------------------------------------------
# Reuse exemption: a warm asyncssh channel never touches the bucket
# ---------------------------------------------------------------------------

asyncssh = pytest.importorskip("asyncssh")

from hpc_agent.infra import ssh_engine  # noqa: E402
from hpc_agent.infra.ssh_engine import _Engine  # noqa: E402


class _StubResult:
    def __init__(self, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.exit_status = returncode
        self.stdout = stdout
        self.stderr = stderr


class _StubConn:
    def __init__(self) -> None:
        self.closed = False
        self.run_calls: list[str] = []

    async def run(self, cmd: str, *, check: bool = False, timeout: float | None = None):
        self.run_calls.append(cmd)
        return _StubResult()

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_reused_channel_is_exempt_from_pacing(monkeypatch):
    """The engine paces the CONNECT (a new establishment) but a command run over
    the already-open connection is a REUSE — no new handshake reaches the host,
    so it never touches the bucket. Pinned via the engine exec path: two runs to
    one host ⇒ exactly ONE pace (the open), zero for the reused channel."""
    monkeypatch.delenv(NO_PACING_ENV, raising=False)
    paced: list[str] = []

    def _spy_pace(target, **_k):
        paced.append(target)

    monkeypatch.setattr(ssh_engine.ssh_pacing, "pace_establishment", _spy_pace)

    async def _fake_connect(ssh_target: str):
        import asyncio

        return _StubConn(), asyncio.Semaphore(8)

    monkeypatch.setattr(ssh_engine, "_do_connect", _fake_connect)

    eng = _Engine()
    try:
        eng.run("echo 1", ssh_target=TARGET, timeout=5)
        eng.run("echo 2", ssh_target=TARGET, timeout=5)  # reuse: no new establishment
    finally:
        eng.shutdown_all()
    assert paced == [TARGET]  # only the open paced; the reused channel did not
