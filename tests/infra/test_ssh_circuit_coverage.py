"""Behavior-pinning MUTATION coverage for :mod:`hpc_agent.infra.ssh_circuit`.

The breaker is the fleet-level ban-hammer: a silent bug here is a wedged
circuit that never opens (the 2026-07-04 all-night probe storm → IP ban) or
never closes (a host stays fenced off forever), or a probe-slot leak that lets
a fleet stampede a half-open host. This file pins the EXACT numeric boundaries
and branch conditions the state machine turns on, each assertion named with the
mutation it kills — a complement to ``test_ssh_circuit.py`` (which pins the
end-to-end behaviors) sharpened to the off-by-one / comparison-operator seams a
mutation tester flips.

Time is the injectable ``clock`` (no real sleeps); state isolation is free from
the suite-wide autouse ``_isolated_journal_home`` fixture (the breaker resolves
its state dir through ``run_record._current_homedir``).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
from hpc_agent.infra import ssh_circuit
from hpc_agent.infra.ssh_circuit import (
    BASE_COOLDOWN_SEC,
    CIRCUIT_THRESHOLD,
    CYCLE1_COOLDOWN_SEC,
    CYCLE2_COOLDOWN_SEC,
    CYCLE3_PLUS_COOLDOWN_SEC,
    PROBE_CLAIM_TTL_SEC,
    check_circuit,
    circuit_state_path,
    classify_connection_failure,
    effective_state,
    guarded_call,
    open_deadline,
    record_connection_failure,
    record_connection_success,
)

HOST = "login.cluster.edu"
TARGET = f"user@{HOST}"


class FakeClock:
    """Injectable wall clock starting at the real epoch (state written under a
    fake clock is also read by production paths gating on ``time.time()``)."""

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _fail_n(
    clock: FakeClock, n: int, target: str = TARGET, detail: str = "connect timeout"
) -> None:
    for _ in range(n):
        record_connection_failure(target, detail=detail, clock=clock)


def _state(host: str = HOST) -> dict:
    doc = json.loads(circuit_state_path(host).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


# ===========================================================================
# closed -> open threshold: the EXACT consecutive-failure count
# ===========================================================================


class TestOpenThresholdBoundary:
    def test_one_below_threshold_stays_closed(self):
        """Kills ``failures >= CIRCUIT_THRESHOLD`` -> ``failures >= N-1``: the
        (THRESHOLD-1)-th failure must NOT open. State stays closed and the exact
        counter is recorded."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        doc = _state()
        assert doc["state"] == "closed"
        assert doc["consecutive_failures"] == CIRCUIT_THRESHOLD - 1
        check_circuit(TARGET, clock=clock)  # closed: no raise

    def test_exactly_threshold_opens(self):
        """Kills ``failures > CIRCUIT_THRESHOLD`` (off-by-one late-open): the
        THRESHOLD-th failure opens, at the cycle-1 cooldown, counter == THRESHOLD."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["consecutive_failures"] == CIRCUIT_THRESHOLD
        assert doc["cooldown_sec"] == CYCLE1_COOLDOWN_SEC  # graduated: fresh open → cycle 1
        assert doc["opened_at"] == pytest.approx(clock.now)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_threshold_constant_is_three(self):
        """Pins the constant itself — an accidental bump silently widens the
        ban-risk window every downstream boundary test is calibrated against."""
        assert CIRCUIT_THRESHOLD == 3

    def test_success_resets_the_counter_so_threshold_is_CONSECUTIVE(self):
        """Kills a mutation that drops the reset: THRESHOLD-1 fails, a success
        zeroes the counter, then THRESHOLD-1 more fails must still be closed
        (the breaker counts CONSECUTIVE failures, not lifetime)."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        record_connection_success(TARGET)
        assert _state()["consecutive_failures"] == 0
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        assert _state()["state"] == "closed"
        check_circuit(TARGET, clock=clock)


# ===========================================================================
# open_deadline arithmetic + effective_state open->half_open boundary
# ===========================================================================


class TestOpenDeadlineBoundary:
    def test_open_deadline_is_opened_at_plus_cooldown(self):
        """Kills ``+`` -> ``-`` and a dropped ``cooldown_sec`` term."""
        assert open_deadline({"opened_at": 1000.0, "cooldown_sec": 300.0}, now=0.0) == 1300.0
        # A different cooldown proves the second term is actually added.
        assert open_deadline({"opened_at": 1000.0, "cooldown_sec": 60.0}, now=0.0) == 1060.0

    def test_open_deadline_fail_open_defaults(self):
        """Missing ``opened_at`` falls back to *now*; missing ``cooldown_sec``
        falls back to BASE — a malformed doc looks freshly opened, never crashes."""
        assert open_deadline({}, now=50.0) == 50.0 + BASE_COOLDOWN_SEC
        assert open_deadline({"opened_at": 10.0}, now=0.0) == 10.0 + BASE_COOLDOWN_SEC

    def test_just_before_deadline_reads_open(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        doc = _state()
        deadline = open_deadline(doc, now=clock.now)
        assert effective_state(doc, now=deadline - 0.001) == "open"

    def test_exactly_at_deadline_reads_half_open_eligible(self):
        """Kills ``now < open_deadline`` -> ``now <= open_deadline``: AT the
        deadline the cooldown is over, so the state is half_open_eligible, not a
        stale open that would fence the host one tick too long."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        doc = _state()
        deadline = open_deadline(doc, now=clock.now)
        assert effective_state(doc, now=deadline) == "half_open_eligible"

    def test_check_circuit_grants_probe_exactly_at_deadline(self):
        """The gate agrees with the read seam at the boundary: at the deadline
        check_circuit stops raising and claims the probe slot."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        deadline = open_deadline(_state(), now=clock.now)
        clock.now = deadline  # exactly at the boundary
        check_circuit(TARGET, clock=clock)  # claims probe: no raise
        assert _state()["probe_claimed_at"] == pytest.approx(deadline)

    def test_one_below_deadline_still_fails_fast(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        deadline = open_deadline(_state(), now=clock.now)
        clock.now = deadline - 0.001
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)


# ===========================================================================
# half-open resolution: success closes, failure re-opens with doubled cooldown
# ===========================================================================


class TestHalfOpenResolution:
    def test_probe_success_closes_and_resets_cooldown_to_base(self):
        """A successful probe fully resets: state closed, counter 0, cooldown
        back to the BASE placeholder (kills a mutation that leaves the escalated
        cooldown or the open state behind)."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 (15s)
        # Re-open once so cooldown escalates to cycle 2, proving the reset restores BASE.
        clock.advance(CYCLE1_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)
        record_connection_failure(TARGET, detail="still down", clock=clock)
        assert _state()["cooldown_sec"] == CYCLE2_COOLDOWN_SEC
        clock.advance(CYCLE2_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_success(TARGET)
        doc = _state()
        assert doc["state"] == "closed"
        assert doc["consecutive_failures"] == 0
        assert doc["cooldown_sec"] == BASE_COOLDOWN_SEC  # closed-doc placeholder
        assert doc["probe_claimed_at"] is None

    def test_probe_failure_escalates_to_next_cycle_and_clears_the_slot(self):
        """Kills a dropped escalation (cycle stays at 1) and a dropped slot-clear."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 (15s)
        clock.advance(CYCLE1_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        assert _state()["probe_claimed_at"] is not None
        record_connection_failure(TARGET, detail="still down", clock=clock)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["cooldown_sec"] == CYCLE2_COOLDOWN_SEC  # cycle 2 (60s)
        assert doc["probe_claimed_at"] is None  # slot cleared so a NEW probe can run
        assert doc["opened_at"] == pytest.approx(clock.now)  # re-opened at now

    def test_cooldown_schedule_caps_at_cycle3(self):
        """Kills an unbounded escalation: repeated probe failures ceiling at
        CYCLE3_PLUS_COOLDOWN_SEC (cycle 3+) and never exceed it."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        for _ in range(8):  # far more re-opens than needed to saturate the schedule
            cd = _state()["cooldown_sec"]
            clock.advance(cd + 1)
            check_circuit(TARGET, clock=clock)
            record_connection_failure(TARGET, detail="down", clock=clock)
            assert _state()["cooldown_sec"] <= CYCLE3_PLUS_COOLDOWN_SEC
        assert _state()["cooldown_sec"] == CYCLE3_PLUS_COOLDOWN_SEC

    def test_straggler_failure_with_no_claimed_slot_is_evidence_only(self):
        """Kills a mutation that drops the ``probe_claimed_at is not None`` guard
        on the re-open path: a concurrent in-flight failure while open (no probe
        claimed) must NOT escalate the cooldown, or a burst inflates it spuriously."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 (15s)
        assert _state()["probe_claimed_at"] is None
        record_connection_failure(TARGET, detail="straggler", clock=clock)
        doc = _state()
        assert doc["cooldown_sec"] == CYCLE1_COOLDOWN_SEC  # unchanged, not escalated


# ===========================================================================
# half-open probe slot: single-claimant, TTL reclaim boundary
# ===========================================================================


class TestProbeSlotClaim:
    def test_second_claimant_fails_fast_while_probe_in_flight(self):
        """Only ONE probe slot: the second concurrent claimant (before the TTL)
        fails fast with an in-flight message, never opening a second connection."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # first claims
        with pytest.raises(SshCircuitOpen) as ei:
            check_circuit(TARGET, clock=clock)  # second, same instant
        assert "probe" in str(ei.value)

    def test_claim_just_before_ttl_still_in_flight(self):
        """Kills ``now - claimed_at < PROBE_CLAIM_TTL_SEC`` -> ``<=``-side slack:
        at TTL-epsilon the slot is still owned; a new claimant fails fast."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim
        clock.advance(PROBE_CLAIM_TTL_SEC - 0.001)
        with pytest.raises(SshCircuitOpen) as ei:
            check_circuit(TARGET, clock=clock)
        assert "probe" in str(ei.value)

    def test_claim_at_exactly_ttl_is_reclaimable(self):
        """Kills ``< PROBE_CLAIM_TTL_SEC`` -> ``<= PROBE_CLAIM_TTL_SEC``: AT the
        TTL the abandoned slot is reclaimed (a crashed claimant must not wedge
        the circuit open forever)."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim, then "crash"
        claimed_at = _state()["probe_claimed_at"]
        clock.now = claimed_at + PROBE_CLAIM_TTL_SEC  # exactly at the TTL
        check_circuit(TARGET, clock=clock)  # reclaimed: no raise
        assert _state()["probe_claimed_at"] == pytest.approx(clock.now)

    def test_ttl_constant_is_120s(self):
        assert PROBE_CLAIM_TTL_SEC == 120.0


# ===========================================================================
# per-host isolation: one host's open circuit never fences another
# ===========================================================================


class TestPerHostIsolation:
    def test_open_circuit_on_host_a_does_not_block_host_b(self):
        """Kills a mutation that keys the breaker globally instead of per host:
        host A's tripped circuit must leave host B fully usable."""
        clock = FakeClock()
        other = "user@other.cluster.edu"
        _fail_n(clock, CIRCUIT_THRESHOLD, target=TARGET)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)
        check_circuit(other, clock=clock)  # host B untouched: no raise
        assert not circuit_state_path("other.cluster.edu").exists()

    def test_each_host_gets_its_own_state_file(self):
        clock = FakeClock()
        _fail_n(clock, 1, target=TARGET)
        _fail_n(clock, 1, target="user@second.host")
        assert circuit_state_path(HOST).exists()
        assert circuit_state_path("second.host").exists()
        assert circuit_state_path(HOST) != circuit_state_path("second.host")

    def test_host_normalization_strips_user_and_whitespace(self):
        """``user@host`` and a bare alias key one circuit; multiple ``@`` keeps
        the last segment; surrounding whitespace is stripped (kills a mutation
        that drops the ``.rsplit('@', 1)[-1].strip()`` normalization)."""
        assert ssh_circuit._host("u@host.edu") == "host.edu"
        assert ssh_circuit._host("a@b@host.edu") == "host.edu"
        assert ssh_circuit._host("  host.edu  ") == "host.edu"
        assert ssh_circuit._host("host.edu") == "host.edu"


# ===========================================================================
# classify_connection_failure: only connection-level markers count
# ===========================================================================


class TestClassifyBoundary:
    def test_returncode_zero_short_circuits_to_none(self):
        """Kills a dropped ``if cp.returncode == 0: return None`` guard: a
        success is never a connection failure even if its stdout happens to
        contain a marker substring."""
        assert classify_connection_failure(_cp(stderr="connection refused", returncode=0)) is None

    def test_marker_matched_case_insensitively_across_stderr_and_stdout(self):
        assert (
            classify_connection_failure(_cp(stderr="CONNECTION REFUSED", returncode=255))
            == "connection refused"
        )
        # Marker in stdout (not stderr) still matches — both streams are scanned.
        assert (
            classify_connection_failure(_cp(stdout="No route to host", returncode=255))
            == "no route to host"
        )

    def test_auth_and_command_failures_return_none(self):
        """Permission-denied and a non-zero remote command both PROVE the host
        accepted a connection — the opposite of ban-risk evidence."""
        auth = _cp(stderr="Permission denied (publickey).", returncode=255)
        cmd = _cp(stderr="No such file or directory", returncode=2)
        assert classify_connection_failure(auth) is None
        assert classify_connection_failure(cmd) is None

    def test_non_255_nonzero_exit_never_matches_regardless_of_stderr(self):
        """Kills a dropped ``cp.returncode != 255 -> None`` gate (the 2026-07-19
        false-trip): any non-255 non-zero exit is the REMOTE command's own
        status — it ran, the transport worked — so even verbatim marker text
        in its stderr is remote content and must return ``None``."""
        commlib = _cp(
            stderr="error: commlib error: got select error (Connection refused)",
            returncode=1,
        )
        assert classify_connection_failure(commlib) is None
        reset = _cp(stderr="Connection reset by peer", returncode=2)
        assert classify_connection_failure(reset) is None
        # ... in EITHER stream (the classifier scans both).
        stdout_side = _cp(stdout="No route to host", returncode=1)
        assert classify_connection_failure(stdout_side) is None

    def test_remote_exit_255_is_the_accepted_residual(self):
        """ssh collapses a REMOTE command's own ``exit 255`` onto the client's
        transport-failure code — indistinguishable at this seam. The marker
        match is the only remaining guard there: transport-shaped text still
        counts (accepted, documented), ordinary app text does not. Pins the
        boundary so a "helpful" tightening/loosening fails loudly."""
        remote_255_marker = _cp(
            stderr="error: commlib error: got select error (Connection refused)",
            returncode=255,
        )
        assert classify_connection_failure(remote_255_marker) is not None
        remote_255_plain = _cp(stderr="segmentation fault (core dumped)", returncode=255)
        assert classify_connection_failure(remote_255_plain) is None


# ===========================================================================
# guarded_call: gate order, slot release on EVERY path, slot-timeout isolation
# ===========================================================================


class TestGuardedCall:
    def test_breaker_gate_runs_before_fn(self):
        """Kills a reordering that would run ``fn`` before the breaker check: an
        open circuit must fail fast without ever invoking the attempt."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        called = {"n": 0}

        def fn():
            called["n"] += 1
            return _cp(returncode=0)

        with pytest.raises(SshCircuitOpen):
            guarded_call(TARGET, fn, clock=clock, sleep=lambda _s: None)
        assert called["n"] == 0  # fn never ran

    def test_slot_released_after_success_records_reset(self):
        from hpc_agent.infra import ssh_slots

        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)  # counter at N-1
        guarded_call(TARGET, lambda: _cp(returncode=0), clock=clock, sleep=lambda _s: None)
        # Success reset the counter AND released the slot for the next caller.
        assert _state()["consecutive_failures"] == 0
        assert not any(p.exists() for p in ssh_slots.slot_paths(HOST))

    def test_slot_released_on_connection_marked_completedprocess(self):
        """The marker path (a CompletedProcess with a connection-level marker,
        NOT an exception) must still release the slot and record the failure
        with the marker in the detail."""
        from hpc_agent.infra import ssh_slots

        clock = FakeClock()
        cp = _cp(stderr="ssh: connect to host x port 22: Connection refused", returncode=255)
        guarded_call(TARGET, lambda: cp, clock=clock, sleep=lambda _s: None)
        assert not any(p.exists() for p in ssh_slots.slot_paths(HOST))
        doc = _state()
        assert doc["consecutive_failures"] == 1
        assert "connection refused" in doc["last_failure"]["detail"]

    def test_slot_released_on_timeout_which_counts_toward_breaker(self):
        from hpc_agent.infra import ssh_slots

        clock = FakeClock()

        def boom():
            raise TimeoutError("ssh to host timed out after 60s")

        with pytest.raises(TimeoutError):
            guarded_call(TARGET, boom, clock=clock, sleep=lambda _s: None)
        assert not any(p.exists() for p in ssh_slots.slot_paths(HOST))
        assert _state()["consecutive_failures"] == 1  # timeout counted

    def test_slot_wait_timeout_does_not_count_toward_breaker(self):
        """A ``SshSlotWaitTimeout`` is LOCAL contention, not host evidence — it
        must never trip the breaker (kills a mutation that records it as a
        connection failure)."""
        from hpc_agent.infra import ssh_slots

        clock = FakeClock()
        # Saturate the host's slots with the REAL current pid so they read ALIVE
        # under guarded_call's own (real) liveness probe and cannot be reclaimed —
        # forcing guarded_call's slot acquire to wait out the bound and give up.
        toks = [
            ssh_slots.acquire_slot(TARGET, clock=clock, sleep=lambda _s: None)
            for _ in range(ssh_slots.DEFAULT_MAX_CONNECTIONS)
        ]
        assert all(t is not None for t in toks)

        def sleeper(seconds: float) -> None:
            clock.advance(seconds)

        with pytest.raises(SshSlotWaitTimeout):
            guarded_call(TARGET, lambda: _cp(returncode=0), clock=clock, sleep=sleeper)
        # No breaker state was written by the slot-wait give-up.
        assert not circuit_state_path(HOST).exists()


# ===========================================================================
# record_connection_success: hot-path no-op + incident-cycle carry-forward
# ===========================================================================


class TestSuccessSemantics:
    def test_success_on_missing_doc_writes_nothing(self):
        """Kills a mutation that creates a fresh doc on success: a never-failed
        host has no state file and a success must not mint one."""
        record_connection_success(TARGET)
        assert not circuit_state_path(HOST).exists()

    def test_success_on_clean_closed_doc_is_a_no_op_write(self):
        """A closed doc with a zero counter needs no rewrite — the steady healthy
        state costs one read and zero lock/write traffic (kills a mutation that
        always writes)."""
        clock = FakeClock()
        _fail_n(clock, 1)
        record_connection_success(TARGET)  # 1 -> 0 : writes
        before = circuit_state_path(HOST).read_bytes()
        mtime = circuit_state_path(HOST).stat().st_mtime_ns
        record_connection_success(TARGET)  # already clean : no write
        assert circuit_state_path(HOST).read_bytes() == before
        assert circuit_state_path(HOST).stat().st_mtime_ns == mtime

    def test_success_carries_reopen_cycle_counter_forward(self):
        """The run-13 livelock signal: a connection-level success (the deceptive
        cheap probe) must PRESERVE ``reopen_cycles`` / ``incident_started_at``,
        never wipe them — only INCIDENT_WINDOW expiry resets. Kills a mutation
        that ``_fresh_doc``s the whole thing away."""
        clock = FakeClock()
        detail = f"ssh to {TARGET} timed out after 60s: module load x && source /a/conda.sh"
        _fail_n(clock, CIRCUIT_THRESHOLD, detail=detail)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)
        started = _state()["incident_started_at"]
        record_connection_success(TARGET)
        doc = _state()
        assert doc["state"] == "closed"
        assert doc["reopen_cycles"] == 1  # incident cycle survived the close
        assert doc["incident_started_at"] == started


# ===========================================================================
# override + fail-open robustness
# ===========================================================================


class TestOverrideAndFailOpen:
    def test_override_is_per_host_and_comma_split_stripped(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        import os

        os.environ["HPC_SSH_CIRCUIT_OVERRIDE"] = f" unrelated , {HOST} "
        try:
            check_circuit(TARGET, clock=clock)  # named (whitespace-tolerant): no raise
            os.environ["HPC_SSH_CIRCUIT_OVERRIDE"] = "unrelated.only"
            with pytest.raises(SshCircuitOpen):
                check_circuit(TARGET, clock=clock)  # not named: still fails fast
        finally:
            os.environ.pop("HPC_SSH_CIRCUIT_OVERRIDE", None)

    def test_empty_override_env_does_not_bypass(self):
        """An empty / whitespace override string must read as 'no override' —
        kills a mutation where a blank env accidentally names every host."""
        assert ssh_circuit._overridden(HOST) is False
        import os

        os.environ["HPC_SSH_CIRCUIT_OVERRIDE"] = "   "
        try:
            assert ssh_circuit._overridden(HOST) is False
        finally:
            os.environ.pop("HPC_SSH_CIRCUIT_OVERRIDE", None)

    def test_override_still_records_failures(self):
        """Override only bypasses the fail-fast GATE; evidence is still recorded
        (so lifting the override doesn't reset the host's health)."""
        import os

        clock = FakeClock()
        os.environ["HPC_SSH_CIRCUIT_OVERRIDE"] = HOST
        try:
            _fail_n(clock, CIRCUIT_THRESHOLD)
            assert _state()["state"] == "open"  # recorded despite the bypass
        finally:
            os.environ.pop("HPC_SSH_CIRCUIT_OVERRIDE", None)

    def test_malformed_state_file_fails_open(self):
        clock = FakeClock()
        path = circuit_state_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        check_circuit(TARGET, clock=clock)  # unreadable -> breaker inactive, no raise

    def test_empty_host_target_is_a_noop(self):
        """A target that normalizes to an empty host neither gates nor records
        (kills a mutation that drops the ``if not host`` short-circuit)."""
        clock = FakeClock()
        check_circuit("", clock=clock)  # no raise
        record_connection_failure("", detail="x", clock=clock)  # no crash
        record_connection_success("")  # no crash
