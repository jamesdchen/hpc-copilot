"""Tests for the cross-process per-host SSH connection-slot limiter
(infra.ssh_slots) — the proving-run-#4 burst-prevention guard.

Time is mocked via the injectable ``clock``/``sleep`` parameters — no real
sleeps. Fake claimant pids are paired with an injected ``pid_alive`` (the
real liveness probe would see them as dead and reclaim instantly). State
isolation comes from the suite-wide autouse ``_isolated_journal_home``
fixture (the limiter resolves its state dir through
``run_record._current_homedir``, same as the circuit breaker).
"""

from __future__ import annotations

import json
import time

import pytest

from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
from hpc_agent.infra.ssh_slots import (
    _JITTER_MOD,
    DEFAULT_MAX_CONNECTIONS,
    SLOT_JITTER_MAX_SEC,
    SLOT_POLL_BASE_SEC,
    SLOT_TTL_SEC,
    SLOT_WAIT_MAX_SEC,
    acquire_slot,
    connection_slot,
    release_slot,
    resolve_max_connections,
    slot_paths,
)

HOST = "login.cluster.edu"
TARGET = f"user@{HOST}"

_ALIVE = lambda pid: True  # noqa: E731 — fake pids must read as live claimants


class FakeClock:
    """Injectable wall clock (starts at the real epoch — see ssh_circuit's
    FakeClock for why: state written under a fake clock is also read by
    production paths gating on ``time.time()``)."""

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _no_sleep(seconds: float) -> None:
    pytest.fail(f"unexpected sleep({seconds}): a free slot must not wait")


def _jitter(pid: int) -> float:
    return (pid % _JITTER_MOD) / _JITTER_MOD * SLOT_JITTER_MAX_SEC


def _claim_n(n: int, clock: FakeClock, *, first_pid: int = 100) -> list:
    tokens = []
    for i in range(n):
        tok = acquire_slot(
            TARGET, clock=clock, sleep=_no_sleep, pid=first_pid + i, pid_alive=_ALIVE
        )
        assert tok is not None
        tokens.append(tok)
    return tokens


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


class TestResolve:
    def test_default_is_two(self, monkeypatch):
        monkeypatch.delenv("HPC_SSH_MAX_CONNECTIONS", raising=False)
        assert resolve_max_connections() == DEFAULT_MAX_CONNECTIONS == 2

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MAX_CONNECTIONS", "5")
        assert resolve_max_connections() == 5

    def test_zero_disables(self, monkeypatch):
        monkeypatch.setenv("HPC_SSH_MAX_CONNECTIONS", "0")
        assert resolve_max_connections() == 0
        assert acquire_slot(TARGET, sleep=_no_sleep) is None  # no-op token

    @pytest.mark.parametrize("bad", ["-3", "abc", "2.5"])
    def test_invalid_warns_and_keeps_default(self, monkeypatch, capsys, bad):
        """Default-on guard: a typo degrades to 'still guarded', never to
        'silently unlimited' (unlike the opt-in safe_interval throttle)."""
        monkeypatch.setenv("HPC_SSH_MAX_CONNECTIONS", bad)
        assert resolve_max_connections() == DEFAULT_MAX_CONNECTIONS
        assert "HPC_SSH_MAX_CONNECTIONS" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Slot cap: N hold, the rest wait (deterministically jittered)
# ---------------------------------------------------------------------------


class TestCap:
    def test_n_plus_two_claimants_only_n_hold(self):
        clock = FakeClock()
        tokens = _claim_n(DEFAULT_MAX_CONNECTIONS, clock)
        assert all(t.exists() for t in tokens)
        # Claimants N+1 and N+2 cannot claim without waiting.
        for pid in (301, 302):
            slept: list[float] = []

            def hold_then_give_up(seconds: float, slept=slept) -> None:
                slept.append(seconds)
                clock.advance(seconds)

            with pytest.raises(SshSlotWaitTimeout):
                acquire_slot(
                    TARGET, clock=clock, sleep=hold_then_give_up, pid=pid, pid_alive=_ALIVE
                )
            assert slept  # actually waited (did not sneak a third slot)

    def test_waiter_acquires_when_a_slot_frees_and_jitter_is_pid_derived(self):
        clock = FakeClock()
        tokens = _claim_n(2, clock)
        pid = 303
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)
            if len(slept) == 2:  # a holder finishes while we wait
                release_slot(tokens[0])

        tok = acquire_slot(TARGET, clock=clock, sleep=sleeper, pid=pid, pid_alive=_ALIVE)
        assert tok is not None and tok.exists()
        # Deterministic backoff: base interval + pid jitter, then doubled.
        assert slept[0] == pytest.approx(SLOT_POLL_BASE_SEC + _jitter(pid))
        assert slept[1] == pytest.approx(SLOT_POLL_BASE_SEC * 2 + _jitter(pid))

    def test_two_waiters_with_different_pids_desynchronize(self):
        """The point of pid-derived jitter: co-started waiters re-poll at
        different instants instead of stampeding in lockstep."""
        assert _jitter(301) != _jitter(302)

    def test_hosts_are_independent(self):
        clock = FakeClock()
        _claim_n(2, clock)  # saturate HOST
        other = acquire_slot(
            "user@other.cluster.edu", clock=clock, sleep=_no_sleep, pid=400, pid_alive=_ALIVE
        )
        assert other is not None

    def test_user_prefix_is_normalized_to_host(self):
        """user@host and a bare alias share one slot pool (same
        normalization as the breaker and the safe_interval throttle)."""
        clock = FakeClock()
        _claim_n(2, clock)
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout):
            acquire_slot(HOST, clock=clock, sleep=sleeper, pid=999, pid_alive=_ALIVE)


# ---------------------------------------------------------------------------
# Bounded give-up envelope
# ---------------------------------------------------------------------------


class TestGiveUp:
    def test_wait_is_bounded_and_envelope_is_clear(self, capsys):
        clock = FakeClock()
        start = clock()
        _claim_n(2, clock)

        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout) as excinfo:
            acquire_slot(TARGET, clock=clock, sleep=sleeper, pid=500, pid_alive=_ALIVE)
        # Bounded: gave up right at the wait ceiling, not a new wedge class.
        assert clock() - start <= SLOT_WAIT_MAX_SEC + SLOT_POLL_BASE_SEC
        exc = excinfo.value
        assert exc.error_code == "ssh_unreachable"  # reused code (no breaking envelope change)
        assert exc.retry_safe is True
        assert exc.category == "network"
        msg = str(exc)
        assert HOST in msg
        assert "HPC_SSH_MAX_CONNECTIONS" in msg  # how to raise/disable the cap
        assert exc.remediation and "_ssh_throttle" in exc.remediation
        assert "waiting" in capsys.readouterr().err  # announced once, loudly

    def test_sleeps_never_overshoot_the_deadline(self):
        clock = FakeClock()
        deadline = clock() + SLOT_WAIT_MAX_SEC
        _claim_n(2, clock)
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout):
            acquire_slot(TARGET, clock=clock, sleep=sleeper, pid=501, pid_alive=_ALIVE)
        assert clock() <= deadline + 1e-6  # each sleep clamped to time remaining


# ---------------------------------------------------------------------------
# Breaker interplay: open circuit ⇒ fail fast, never queue
# ---------------------------------------------------------------------------


class TestBreakerInterplay:
    def test_open_circuit_fails_waiter_fast_instead_of_queueing(self):
        from hpc_agent.infra.ssh_circuit import CIRCUIT_THRESHOLD, record_connection_failure

        clock = FakeClock()
        _claim_n(2, clock)
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail="connect timeout", clock=clock)
        slept: list[float] = []
        with pytest.raises(SshCircuitOpen):
            acquire_slot(TARGET, clock=clock, sleep=slept.append, pid=600, pid_alive=_ALIVE)
        assert slept == []  # raised before the first backoff sleep

    def test_guarded_call_checks_breaker_before_slot_queue(self):
        """guarded_call's gate order: breaker first, so an open circuit
        never spends the 120s slot-wait budget."""
        from hpc_agent.infra.ssh_circuit import (
            CIRCUIT_THRESHOLD,
            guarded_call,
            record_connection_failure,
        )

        clock = FakeClock()
        _claim_n(2, clock)  # saturated host
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail="connect timeout", clock=clock)
        with pytest.raises(SshCircuitOpen):
            guarded_call(
                TARGET, lambda: pytest.fail("must not spawn"), clock=clock, sleep=_no_sleep
            )


# ---------------------------------------------------------------------------
# Release semantics
# ---------------------------------------------------------------------------


class TestRelease:
    def test_slot_released_on_exception(self):
        clock = FakeClock()
        with (  # noqa: PT012 — the ctx manager's finally IS the unit under test
            pytest.raises(RuntimeError),
            connection_slot(TARGET, clock=clock, sleep=_no_sleep, pid=700, pid_alive=_ALIVE),
        ):
            assert any(p.exists() for p in slot_paths(HOST))
            raise RuntimeError("boom mid-hold")
        assert not any(p.exists() for p in slot_paths(HOST))

    def test_guarded_call_holds_during_fn_and_releases_on_timeout(self):
        from hpc_agent.infra.ssh_circuit import guarded_call

        def boom():
            assert any(p.exists() for p in slot_paths(HOST))  # held around the attempt
            raise TimeoutError("ssh timed out")

        with pytest.raises(TimeoutError):
            guarded_call(TARGET, boom, sleep=_no_sleep)
        assert not any(p.exists() for p in slot_paths(HOST))

    def test_release_none_is_noop(self):
        release_slot(None)


# ---------------------------------------------------------------------------
# Cross-process sharing (file-based state)
# ---------------------------------------------------------------------------


class TestCrossProcess:
    def test_claims_by_one_process_count_against_another(self, monkeypatch, tmp_path):
        """Same HPC_JOURNAL_DIR ⇒ same slot pool, regardless of process."""
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "shared_home"))
        clock = FakeClock()
        # "Process A" (pid 100) and "process B" (pid 101) each hold a slot...
        _claim_n(2, clock)
        # ...so "process C" cannot claim without waiting.
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout):
            acquire_slot(TARGET, clock=clock, sleep=sleeper, pid=102, pid_alive=_ALIVE)
        assert (tmp_path / "shared_home" / "_ssh_throttle").is_dir()

    def test_claim_doc_records_pid_and_claimed_at(self):
        clock = FakeClock()
        (token,) = _claim_n(1, clock, first_pid=4242)
        doc = json.loads(token.read_text(encoding="utf-8"))
        assert doc["pid"] == 4242
        assert doc["host"] == HOST
        assert doc["claimed_at"] == pytest.approx(clock())


# ---------------------------------------------------------------------------
# Stale-claim reclaim (crashed holders must not leak capacity)
# ---------------------------------------------------------------------------


class TestReclaim:
    def test_dead_pid_slot_is_reclaimed_immediately(self):
        clock = FakeClock()
        _claim_n(2, clock, first_pid=100)  # pids 100, 101
        tok = acquire_slot(
            TARGET,
            clock=clock,
            sleep=_no_sleep,  # no wait: the dead claimant's slot frees instantly
            pid=200,
            pid_alive=lambda pid: pid != 100,  # claimant 100 "crashed"
        )
        assert tok is not None
        assert json.loads(tok.read_text(encoding="utf-8"))["pid"] == 200

    def test_ttl_expired_slot_is_reclaimed_even_if_pid_looks_alive(self):
        clock = FakeClock()
        _claim_n(2, clock)
        clock.advance(SLOT_TTL_SEC + 1)
        tok = acquire_slot(TARGET, clock=clock, sleep=_no_sleep, pid=201, pid_alive=_ALIVE)
        assert tok is not None

    def test_fresh_live_claim_is_never_stolen(self):
        clock = FakeClock()
        _claim_n(2, clock)
        clock.advance(SLOT_TTL_SEC / 2)  # aged, but inside the TTL
        slept: list[float] = []

        def sleeper(seconds: float) -> None:
            slept.append(seconds)
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout):
            acquire_slot(TARGET, clock=clock, sleep=sleeper, pid=202, pid_alive=_ALIVE)


# ---------------------------------------------------------------------------
# Fail-open robustness
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_corrupt_slot_file_reads_as_stale_and_is_reclaimed(self):
        clock = FakeClock()
        paths = slot_paths(HOST)
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_text("{not json", encoding="utf-8")  # corrupt claim
        _claim_n(1, clock, first_pid=100)  # takes slot1 (slot0 "held")
        tok = acquire_slot(TARGET, clock=clock, sleep=_no_sleep, pid=300, pid_alive=_ALIVE)
        assert tok is not None  # corrupt claim did not permanently eat a slot

    def test_broken_state_dir_fails_open(self):
        """A file squatting on the state-dir path ⇒ limiter inactive, SSH
        proceeds unguarded (protection layer, not a correctness gate)."""
        from hpc_agent.state.run_record import _current_homedir

        home = _current_homedir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "_ssh_throttle").write_text("not a directory", encoding="utf-8")
        assert acquire_slot(TARGET, sleep=_no_sleep, pid_alive=_ALIVE) is None

    def test_guarded_call_proceeds_when_limiter_fails_open(self):
        from types import SimpleNamespace

        from hpc_agent.infra.ssh_circuit import guarded_call
        from hpc_agent.state.run_record import _current_homedir

        home = _current_homedir()
        home.mkdir(parents=True, exist_ok=True)
        (home / "_ssh_throttle").write_text("not a directory", encoding="utf-8")
        cp = guarded_call(
            TARGET,
            lambda: SimpleNamespace(stdout="ok", stderr="", returncode=0),
            sleep=_no_sleep,
        )
        assert cp.returncode == 0
