"""Tests for the persistent per-host SSH circuit breaker (infra.ssh_circuit).

Time is mocked via the injectable ``clock`` parameter — no real sleeps.
State isolation comes for free from the suite-wide autouse
``_isolated_journal_home`` fixture (the breaker resolves its state dir
through ``run_record._current_homedir``).
"""

from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from hpc_agent.errors import SshCircuitOpen
from hpc_agent.infra import ssh_circuit
from hpc_agent.infra.ssh_circuit import (
    BASE_COOLDOWN_SEC,
    CIRCUIT_THRESHOLD,
    CYCLE1_COOLDOWN_SEC,
    CYCLE2_COOLDOWN_SEC,
    CYCLE3_PLUS_COOLDOWN_SEC,
    MAX_COOLDOWN_SEC,
    PROBE_CLAIM_TTL_SEC,
    ConnectionOutcome,
    check_circuit,
    circuit_state_path,
    classify_connection_failure,
    classify_connection_outcome,
    guarded_call,
    record_connection_failure,
    record_connection_success,
)

HOST = "login.cluster.edu"
TARGET = f"user@{HOST}"

#: The 2026-07-19 scheduler-integration incident shape: a remote qsub that
#: EXECUTED (ssh transport healthy) and failed because the qmaster was dead
#: container-side — REMOTE stderr carrying a marker-shaped line ("Connection
#: refused") that is application content, never transport evidence.
COMMLIB_QMASTER_DOWN_STDERR = (
    "error: commlib error: got select error (Connection refused)\n"
    'Unable to run job: unable to send message to qmaster using port 6444 on host "sgeci"'
)


class FakeClock:
    """Injectable wall clock: no real time passes in any test.

    Starts at the REAL current epoch (state written with a fake clock is
    also read by production code paths that gate on ``time.time()`` —
    e.g. ``ssh_run``'s breaker check in the integration tests below — and
    a 1970-epoch ``opened_at`` would look long-expired to them).
    """

    def __init__(self, start: float | None = None) -> None:
        self.now = time.time() if start is None else start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _fail_n(clock: FakeClock, n: int, target: str = TARGET) -> None:
    for _ in range(n):
        record_connection_failure(target, detail="connect timeout", clock=clock)


def _state() -> dict:
    doc = json.loads(circuit_state_path(HOST).read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    return doc


# ---------------------------------------------------------------------------
# Trip condition
# ---------------------------------------------------------------------------


class TestTrip:
    def test_trips_after_threshold_consecutive_failures(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        check_circuit(TARGET, clock=clock)  # still closed: no raise
        _fail_n(clock, 1)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_open_emits_loud_stderr_line(self, capsys):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        err = capsys.readouterr().err
        assert "OPENED" in err
        assert HOST in err

    def test_auth_failure_does_not_count(self):
        """Permission-denied proves the connection reached the host."""
        clock = FakeClock()
        auth_cp = _cp(stderr="user@host: Permission denied (publickey).", returncode=255)
        for _ in range(CIRCUIT_THRESHOLD + 2):
            guarded_call(TARGET, lambda: auth_cp, clock=clock)
        check_circuit(TARGET, clock=clock)  # never opened

    def test_remote_command_nonzero_does_not_count(self):
        clock = FakeClock()
        cmd_cp = _cp(stderr="ls: cannot access '/nope': No such file or directory", returncode=2)
        for _ in range(CIRCUIT_THRESHOLD + 2):
            guarded_call(TARGET, lambda: cmd_cp, clock=clock)
        check_circuit(TARGET, clock=clock)

    def test_remote_command_nonzero_with_commlib_stderr_never_trips(self):
        """The 2026-07-19 incident, end-to-end through the breaker seam: three
        remote qsub rc=1 failures carrying the dead qmaster's commlib
        "Connection refused" in REMOTE stderr must leave the breaker CLOSED —
        the command RAN, so the transport is proven regardless of what its
        stderr says (before the rc==255 gate this opened the circuit)."""
        clock = FakeClock()
        commlib_cp = _cp(stderr=COMMLIB_QMASTER_DOWN_STDERR, returncode=1)
        for _ in range(CIRCUIT_THRESHOLD + 2):
            guarded_call(TARGET, lambda: commlib_cp, clock=clock)
        check_circuit(TARGET, clock=clock)  # never opened
        assert not circuit_state_path(HOST).exists()  # zero failures recorded

    def test_genuine_transport_failure_255_trips(self):
        """Behavior preserved: the ssh CLIENT's own failure (exit 255 with a
        transport marker) still counts — CIRCUIT_THRESHOLD consecutive open
        the circuit and the next attempt fails fast."""
        clock = FakeClock()
        down_cp = _cp(stderr="ssh: connect to host x port 22: Connection refused", returncode=255)
        for _ in range(CIRCUIT_THRESHOLD):
            guarded_call(TARGET, lambda: down_cp, clock=clock)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_guarded_call_counts_timeouts_and_trips(self):
        clock = FakeClock()

        def boom():
            raise TimeoutError("ssh to host timed out after 30s")

        for _ in range(CIRCUIT_THRESHOLD):
            with pytest.raises(TimeoutError):
                guarded_call(TARGET, boom, clock=clock)
        with pytest.raises(SshCircuitOpen):
            guarded_call(TARGET, boom, clock=clock)  # gate fires before fn

    def test_user_prefix_is_normalized_to_host(self):
        """user@host and a bare host share one circuit."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD, target=f"other_user@{HOST}")
        with pytest.raises(SshCircuitOpen):
            check_circuit(HOST, clock=clock)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassify:
    @pytest.mark.parametrize(
        "stderr",
        [
            "ssh: connect to host x port 22: Connection refused",
            "Connection reset by peer",
            "ssh: connect to host x port 22: Connection timed out",
            "Connection timed out during banner exchange",
            "ssh_exchange_identification: Connection closed by remote host",
            "kex_exchange_identification: read: Connection reset",
            "ssh: connect to host x port 22: No route to host",
        ],
    )
    def test_connection_failures_match(self, stderr):
        assert classify_connection_failure(_cp(stderr=stderr, returncode=255)) is not None

    @pytest.mark.parametrize(
        ("stderr", "returncode"),
        [
            ("", 0),
            ("Permission denied (publickey).", 255),
            ("Host key verification failed.", 255),
            ("bash: no-such-cmd: command not found", 127),
        ],
    )
    def test_non_connection_outcomes_do_not_match(self, stderr, returncode):
        assert classify_connection_failure(_cp(stderr=stderr, returncode=returncode)) is None

    def test_remote_executed_failure_with_marker_stderr_does_not_match(self):
        """The 2026-07-19 incident at the classifier: a remote command that
        RAN and exited non-zero (rc=1) with the dead qmaster's commlib
        "Connection refused" in REMOTE stderr is NOT connection-level —
        OpenSSH reserves 255 for the client's own failure; any other non-zero
        status is the remote command's own exit."""
        assert (
            classify_connection_failure(_cp(stderr=COMMLIB_QMASTER_DOWN_STDERR, returncode=1))
            is None
        )

    @pytest.mark.parametrize("returncode", [1, 2, 126, 127])
    def test_no_marker_matches_at_a_remote_exit_status(self, returncode):
        """Every marker in the set is remote content at a remote exit status —
        kills a gate that special-cases one marker or one exit code instead of
        keying on the client's own 255."""
        for marker in ssh_circuit._CONNECTION_FAILURE_MARKERS:
            cp = _cp(stderr=f"remote app said: {marker}", returncode=returncode)
            assert classify_connection_failure(cp) is None, (marker, returncode)


# ---------------------------------------------------------------------------
# Tri-state outcome (NIT 1): count / reset / IGNORE
# ---------------------------------------------------------------------------


class TestTriStateOutcome:
    """classify_connection_outcome: rc==255+marker → FAILURE; rc==0 (or the
    rc==255-no-marker residual) → SUCCESS; rc≠255 non-zero → INCONCLUSIVE —
    not transport evidence either way (a direct leg's remote-command status
    OR a wrapper leg's LOCAL wrapper status against a dead host)."""

    @pytest.mark.parametrize("returncode", [1, 2, 12, 127])
    def test_non_255_nonzero_is_inconclusive_even_with_marker_stderr(self, returncode):
        cp = _cp(stderr="ssh: connect to host x port 22: Connection refused", returncode=returncode)
        assert classify_connection_outcome(cp) is ConnectionOutcome.INCONCLUSIVE

    def test_rc_255_with_marker_is_failure(self):
        cp = _cp(stderr="ssh: connect to host x port 22: Connection refused", returncode=255)
        assert classify_connection_outcome(cp) is ConnectionOutcome.FAILURE

    @pytest.mark.parametrize(
        ("stderr", "returncode"),
        [("", 0), ("Permission denied (publickey).", 255), ("segmentation fault", 255)],
    )
    def test_reached_the_host_is_success(self, stderr, returncode):
        assert (
            classify_connection_outcome(_cp(stderr=stderr, returncode=returncode))
            is ConnectionOutcome.SUCCESS
        )


class TestTriStateAccounting:
    """The tri-state at the breaker-accounting seam (the record_* functions):
    an rc≠255 non-zero attempt leaves the breaker UNCHANGED — no failure
    counted AND no success recorded (the false "reached the host" claim a
    wrapper leg against a dead host used to make by driving the classifier's
    None into record_connection_success)."""

    @pytest.mark.parametrize("returncode", [1, 12])
    def test_rc_non_255_nonzero_neither_counts_nor_resets(self, returncode):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)  # genuine failures put the counter at N-1
        seeded_ring = len(_state()["recent_establishments"])
        # rc=1 (remote command failure) / rc=12 (rsync-style wrapper status),
        # even with marker-shaped stderr, record nothing.
        guarded_call(
            TARGET,
            lambda rc=returncode: _cp(stderr="Connection refused", returncode=rc),
            clock=clock,
        )
        doc = _state()
        assert doc["consecutive_failures"] == CIRCUIT_THRESHOLD - 1  # NOT reset
        assert len(doc["recent_establishments"]) == seeded_ring  # no ring append
        check_circuit(TARGET, clock=clock)  # still closed

    def test_ignored_legs_do_not_reset_so_genuine_failures_still_accumulate(self):
        """The end-to-end payoff: wrapper legs sandwiched between genuine
        failures do NOT reset the count, so the circuit still opens at
        threshold (before the tri-state the rc≠255 legs drove
        record_connection_success and wiped the counter)."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        guarded_call(TARGET, lambda: _cp(stderr="Connection refused", returncode=12), clock=clock)
        _fail_n(clock, 1)  # the Nth genuine failure — the ignored leg did not reset
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_rc_0_and_rc_255_no_marker_still_reset(self):
        """Lane-A path (b) byte-identical: rc==0 and the rc==255-no-marker
        residual (auth failure) still record success and reset the counter."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        auth = _cp(stderr="Permission denied (publickey).", returncode=255)
        guarded_call(TARGET, lambda: auth, clock=clock)
        assert _state()["consecutive_failures"] == 0
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        guarded_call(TARGET, lambda: _cp(returncode=0), clock=clock)
        assert _state()["consecutive_failures"] == 0


# ---------------------------------------------------------------------------
# Open behavior / envelope
# ---------------------------------------------------------------------------


class TestOpenEnvelope:
    def test_fail_fast_envelope_fields_and_message(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        with pytest.raises(SshCircuitOpen) as excinfo:
            check_circuit(TARGET, clock=clock)
        exc = excinfo.value
        assert exc.error_code == "ssh_circuit_open"
        assert exc.category == "network"
        assert exc.retry_safe is False
        msg = str(exc)
        assert HOST in msg
        assert "ban-risk" in msg
        assert "failing fast until" in msg  # when the cooldown ends
        assert f"HPC_SSH_CIRCUIT_OVERRIDE={HOST}" in msg  # how to override
        assert exc.remediation and "HPC_SSH_CIRCUIT_OVERRIDE" in exc.remediation

    def test_override_bypasses_open_circuit_per_host(self, monkeypatch):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", f"unrelated.host,{HOST}")
        check_circuit(TARGET, clock=clock)  # no raise
        monkeypatch.setenv("HPC_SSH_CIRCUIT_OVERRIDE", "some.other.host")
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)


# ---------------------------------------------------------------------------
# Cooldown / half-open
# ---------------------------------------------------------------------------


class TestHalfOpen:
    def test_single_probe_slot_under_concurrent_claimants(self, capsys):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # first claimant wins the slot
        assert "half-open" in capsys.readouterr().err
        for _ in range(5):  # the rest of the fleet keeps failing fast
            with pytest.raises(SshCircuitOpen) as excinfo:
                check_circuit(TARGET, clock=clock)
            assert "probe" in str(excinfo.value)

    def test_probe_success_closes_and_resets(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_success(TARGET)
        doc = _state()
        assert doc["state"] == "closed"
        assert doc["consecutive_failures"] == 0
        assert doc["cooldown_sec"] == BASE_COOLDOWN_SEC
        check_circuit(TARGET, clock=clock)  # closed: no raise

    def test_probe_failure_reopens_at_next_cycle_cooldown(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open: cycle 1 → 15s
        assert _state()["cooldown_sec"] == CYCLE1_COOLDOWN_SEC
        clock.advance(CYCLE1_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_failure(TARGET, detail="still down", clock=clock)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["cooldown_sec"] == CYCLE2_COOLDOWN_SEC  # cycle 2 → 60s
        assert doc["probe_claimed_at"] is None
        # Not yet past the NEW (cycle-2) deadline: fail fast.
        clock.advance(CYCLE2_COOLDOWN_SEC - 1)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_graduated_cooldown_progression_caps_at_cycle3(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1
        # 15 → 60 → 300 → 300 … (schedule replaces the old exponential doubling)
        schedule = [
            CYCLE1_COOLDOWN_SEC,
            CYCLE2_COOLDOWN_SEC,
            CYCLE3_PLUS_COOLDOWN_SEC,
            CYCLE3_PLUS_COOLDOWN_SEC,
            CYCLE3_PLUS_COOLDOWN_SEC,
        ]
        for expected in schedule:
            assert _state()["cooldown_sec"] == expected
            clock.advance(expected + 1)
            check_circuit(TARGET, clock=clock)  # half-open probe
            record_connection_failure(TARGET, detail="still down", clock=clock)
        assert _state()["cooldown_sec"] == CYCLE3_PLUS_COOLDOWN_SEC
        # The schedule ceiling is cycle-3, well under the legacy MAX anchor.
        assert CYCLE3_PLUS_COOLDOWN_SEC < MAX_COOLDOWN_SEC

    def test_stale_probe_claim_is_reclaimable(self):
        """A claimant that died must not wedge the circuit open forever."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim, then "crash"
        clock.advance(PROBE_CLAIM_TTL_SEC + 1)
        check_circuit(TARGET, clock=clock)  # reclaimed: no raise

    def test_straggler_failure_while_open_does_not_escalate_cooldown(self):
        """A concurrent in-flight failure (no probe claimed) is evidence only."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 → 15s
        record_connection_failure(TARGET, detail="straggler", clock=clock)
        assert _state()["cooldown_sec"] == CYCLE1_COOLDOWN_SEC  # unchanged, not escalated


# ---------------------------------------------------------------------------
# Success reset
# ---------------------------------------------------------------------------


class TestSuccessReset:
    def test_success_resets_consecutive_count(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        record_connection_success(TARGET)
        assert _state()["consecutive_failures"] == 0
        # The reset means another THRESHOLD-1 failures still don't trip.
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        check_circuit(TARGET, clock=clock)

    def test_success_with_no_state_file_writes_nothing(self):
        record_connection_success(TARGET)
        assert not circuit_state_path(HOST).exists()

    def test_success_with_clean_state_does_not_rewrite(self):
        clock = FakeClock()
        _fail_n(clock, 1)
        record_connection_success(TARGET)  # 1 → 0: writes
        mtime = circuit_state_path(HOST).stat().st_mtime_ns
        before = circuit_state_path(HOST).read_bytes()
        record_connection_success(TARGET)  # already clean: no write
        assert circuit_state_path(HOST).stat().st_mtime_ns == mtime
        assert circuit_state_path(HOST).read_bytes() == before


# ---------------------------------------------------------------------------
# Cross-process sharing (file-based state)
# ---------------------------------------------------------------------------


class TestCrossProcessSharing:
    def test_failures_recorded_by_one_caller_ban_another(self):
        """The point of file-based state: worker A's failure storm makes a
        FRESH ssh_run (worker B / another CLI invocation) fail fast before
        it ever spawns a subprocess."""
        from unittest.mock import patch

        from hpc_agent.infra import remote

        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # "worker A" tripped the breaker
        with (
            patch("hpc_agent.infra.remote.capture_via_select") as seam,
            pytest.raises(SshCircuitOpen),
        ):
            remote.ssh_run("true", ssh_target=TARGET)
        seam.assert_not_called()  # refused BEFORE opening a connection

    def test_two_runner_instances_share_one_state_file(self, monkeypatch, tmp_path):
        """Same HPC_JOURNAL_DIR ⇒ same circuit, regardless of process."""
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "shared_home"))
        clock_a = FakeClock()
        clock_b = FakeClock()
        _fail_n(clock_a, CIRCUIT_THRESHOLD - 1)
        record_connection_failure(TARGET, detail="from runner B", clock=clock_b)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock_b)
        assert (tmp_path / "shared_home" / "_ssh_circuit").is_dir()


# ---------------------------------------------------------------------------
# Ladder integration (remote._with_ssh_backoff consults the breaker
# BETWEEN attempts — see also tests/infra/test_remote.py::TestSshBackoff)
# ---------------------------------------------------------------------------


class TestLadderIntegration:
    def test_no_backoff_single_shot_still_consults_breaker(self, monkeypatch):
        from unittest.mock import patch

        from hpc_agent.infra import remote

        monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        with (
            patch("hpc_agent.infra.remote.capture_via_select") as seam,
            pytest.raises(SshCircuitOpen),
        ):
            remote.ssh_run("true", ssh_target=TARGET)
        seam.assert_not_called()

    def test_successful_ssh_run_resets_counter(self, monkeypatch):
        from unittest.mock import patch

        from hpc_agent.infra import remote

        monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD - 1)
        with patch("hpc_agent.infra.remote.capture_via_select") as seam:
            seam.return_value = subprocess.CompletedProcess(["ssh"], 0, "ok", "")
            remote.ssh_run("true", ssh_target=TARGET)
        assert _state()["consecutive_failures"] == 0

    def test_ssh_target_is_required_so_breaker_cannot_be_bypassed(self):
        """Contract pin: _with_ssh_backoff has no ssh_target default.

        The breaker only guards attempts when it knows the host; an optional
        ``ssh_target=None`` default would let a future call site silently
        bypass the circuit. Pin both the signature (no default) and the
        runtime consequence (omitting it is a TypeError, i.e. the guard
        actually fires).
        """
        import inspect

        from hpc_agent.infra import remote

        param = inspect.signature(remote._with_ssh_backoff).parameters["ssh_target"]
        assert param.default is inspect.Parameter.empty, (
            "ssh_target grew a default again — that silently bypasses the "
            "SSH circuit breaker for any caller that omits it (see 2026-07-04 "
            "probe storm). Keep it required."
        )
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

        with pytest.raises(TypeError, match="ssh_target"):
            remote._with_ssh_backoff(  # type: ignore[call-arg]
                lambda: subprocess.CompletedProcess(["ssh"], 0, "", ""),
                label="pin",
            )


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_malformed_state_file_fails_open(self):
        clock = FakeClock()
        path = circuit_state_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        check_circuit(TARGET, clock=clock)  # unreadable ⇒ breaker inactive

    def test_host_with_unsafe_chars_gets_safe_filename(self):
        clock = FakeClock()
        target = "user@host:with/odd*chars"
        record_connection_failure(target, detail="x", clock=clock)
        expected = ssh_circuit.circuit_state_path("host:with/odd*chars")
        assert expected.name == "host_with_odd_chars.json"
        assert expected.exists()


# ---------------------------------------------------------------------------
# effective_state — the read-seam honesty definition (2026-07-05: stale OPEN)
# ---------------------------------------------------------------------------


class TestEffectiveState:
    """The single READ-side definition every renderer (doctor, snapshot,
    net-triage) routes through: the state FILE says "open" until traffic runs
    the half-open probe, so an expired cooldown must read as
    half_open_eligible, never a stale open."""

    def test_missing_or_closed_doc_reads_closed(self):
        assert ssh_circuit.effective_state(None, now=0.0) == "closed"
        assert ssh_circuit.effective_state({"state": "closed"}, now=0.0) == "closed"
        # Fail-open on a malformed doc without a state key.
        assert ssh_circuit.effective_state({}, now=0.0) == "closed"

    def test_open_within_cooldown_reads_open(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 → 15s
        doc = _state()
        assert doc["state"] == "open"
        assert ssh_circuit.effective_state(doc, now=clock.now) == "open"
        clock.advance(CYCLE1_COOLDOWN_SEC - 1)  # still within the cycle-1 cooldown
        assert ssh_circuit.effective_state(doc, now=clock.now) == "open"

    def test_expired_cooldown_reads_half_open_eligible_while_file_says_open(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        doc = _state()
        assert doc["state"] == "open"  # write semantics untouched: file is stale
        assert ssh_circuit.effective_state(doc, now=clock.now) == "half_open_eligible"

    def test_effective_state_never_writes(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        before = circuit_state_path(HOST).read_text(encoding="utf-8")
        ssh_circuit.effective_state(_state(), now=clock.now)
        assert circuit_state_path(HOST).read_text(encoding="utf-8") == before

    def test_check_circuit_agrees_with_effective_state(self):
        """The gate and the renderer share one deadline definition: while
        effective_state says open, check_circuit refuses; once it says
        half_open_eligible, check_circuit grants the probe slot."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        assert ssh_circuit.effective_state(_state(), now=clock.now) == "open"
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        assert ssh_circuit.effective_state(_state(), now=clock.now) == "half_open_eligible"
        check_circuit(TARGET, clock=clock)  # claims the probe slot: no raise

    def test_open_deadline_matches_opened_at_plus_cooldown(self):
        doc = {"state": "open", "opened_at": 1000.0, "cooldown_sec": 300.0}
        assert ssh_circuit.open_deadline(doc, now=0.0) == 1300.0
        # Malformed opened_at falls back to now (fail-open: looks fresh).
        assert ssh_circuit.open_deadline({"state": "open"}, now=50.0) == 50.0 + BASE_COOLDOWN_SEC


HOST_CD = "cd /home/u/exp && module load conda && source /apps/conda/conda.sh && python run.py"


def _preamble_timeout_detail() -> str:
    """The shape guarded_call records for a wrapper TimeoutError: the ssh
    message embeds the (truncated) command that hung — here the preamble."""
    return f"ssh to {TARGET} timed out after 60s: {HOST_CD}"


def _drive_one_livelock_cycle(clock: FakeClock, *, detail: str) -> None:
    """Open the circuit on `detail` failures, let a cheap probe close it — one
    full run-13 livelock cycle (open → deceptive connection-level close)."""
    for _ in range(CIRCUIT_THRESHOLD):
        record_connection_failure(TARGET, detail=detail, clock=clock)
    clock.advance(BASE_COOLDOWN_SEC + 1)
    check_circuit(TARGET, clock=clock)  # claim the half-open probe slot
    record_connection_success(TARGET)  # the cheap probe closes the circuit


class TestPreambleDegradation:
    """run-13 finding 10/10-addendum: a cheap connection probe keeps CLOSING the
    circuit while the module/conda preamble times out — the breaker livelocks.
    The persisted cycle counter (which a connection-level success must NOT wipe)
    is the signal that names the degradation and suggests host-retarget."""

    def test_connection_success_preserves_the_reopen_cycle_counter(self):
        """The deliverable pin: record_connection_success no longer wipes the
        cycle counter (it used to `_fresh_doc` the whole thing away)."""
        clock = FakeClock()
        _drive_one_livelock_cycle(clock, detail=_preamble_timeout_detail())
        doc = _state()
        assert doc["state"] == "closed"  # the probe closed it
        assert doc["consecutive_failures"] == 0  # health reset …
        assert doc["reopen_cycles"] == 1  # … but the incident cycle survives

    def test_two_cycles_classify_degradation_naming_the_preamble(self):
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)  # cycle 1 → closed
        for _ in range(CIRCUIT_THRESHOLD):  # cycle 2: preamble times out again
            record_connection_failure(TARGET, detail=detail, clock=clock)
        doc = _state()
        assert doc["reopen_cycles"] == 2
        assert ssh_circuit.is_preamble_degraded(doc, now=clock.now) is True
        # The hanging stage is recovered from the recorded command.
        assert ssh_circuit.hanging_stage(doc) == "the conda activation (`source …/conda.sh`)"
        advice = ssh_circuit.degradation_advice(HOST, doc, now=clock.now)
        assert advice is not None
        assert "conda activation" in advice
        assert "2 cycles" in advice
        assert "node-local degradation" in advice

    def test_module_load_stage_named_when_conda_marker_absent(self):
        clock = FakeClock()
        detail = f"ssh to {TARGET} timed out after 60s: cd /x && module load gcc && ./a.out"
        _drive_one_livelock_cycle(clock, detail=detail)
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail=detail, clock=clock)
        assert ssh_circuit.hanging_stage(_state()) == "the module subsystem (`module load`)"

    def test_single_open_is_not_degraded(self):
        """One open (a genuine transient blip that recovers) is below the
        threshold — no false degradation classification."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        doc = _state()
        assert doc["reopen_cycles"] == 1
        assert ssh_circuit.is_preamble_degraded(doc, now=clock.now) is False
        assert ssh_circuit.degradation_advice(HOST, doc, now=clock.now) is None

    def test_stale_incident_window_reads_not_degraded(self):
        """A cycle counter from an OLD incident (window lapsed) is not a live
        degradation — the host may have recovered with no traffic to reset the
        file, the same read-seam honesty as effective_state."""
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail=detail, clock=clock)
        doc = _state()
        assert ssh_circuit.is_preamble_degraded(doc, now=clock.now) is True
        # Far beyond the incident window: reads as not-degraded.
        assert (
            ssh_circuit.is_preamble_degraded(
                doc, now=clock.now + ssh_circuit.INCIDENT_WINDOW_SEC + 1
            )
            is False
        )

    def test_open_error_message_carries_degradation_when_degraded(self):
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)
        for _ in range(CIRCUIT_THRESHOLD):  # cycle 2 → degraded + circuit open
            record_connection_failure(TARGET, detail=detail, clock=clock)
        with pytest.raises(SshCircuitOpen) as ei:
            check_circuit(TARGET, clock=clock)
        msg = str(ei.value)
        assert "DEGRADATION" in msg
        assert "conda activation" in msg

    def test_fresh_incident_after_window_resets_cycle_count(self):
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)  # cycle 1
        for _ in range(CIRCUIT_THRESHOLD):  # cycle 2
            record_connection_failure(TARGET, detail=detail, clock=clock)
        assert _state()["reopen_cycles"] == 2
        # Close, then a NEW open long after the window → fresh incident count.
        clock.advance(BASE_COOLDOWN_SEC * 2 + 1)
        check_circuit(TARGET, clock=clock)
        record_connection_success(TARGET)
        clock.advance(ssh_circuit.INCIDENT_WINDOW_SEC + 1)
        _fail_n(clock, CIRCUIT_THRESHOLD)
        assert _state()["reopen_cycles"] == 1

    def test_sibling_clusters_named_when_config_has_a_match(self, monkeypatch):
        from hpc_agent.infra import clusters as clusters_mod

        cfg = {
            "carc_disc2": {"host": HOST, "scheduler": "slurm", "scratch": "/scratch"},
            "carc_disc1": {
                "host": "discovery1.usc.edu",
                "scheduler": "slurm",
                "scratch": "/scratch",
            },
            "hoffman2": {"host": "h2.example", "scheduler": "sge", "scratch": "/u2"},
        }
        monkeypatch.setattr(clusters_mod, "load_clusters_config", lambda *a, **k: cfg)
        assert ssh_circuit.sibling_clusters(HOST) == ["carc_disc1"]
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail=detail, clock=clock)
        advice = ssh_circuit.degradation_advice(HOST, _state(), now=clock.now)
        assert advice is not None and "host-retarget carc_disc1" in advice

    def test_no_sibling_falls_back_to_settle_run(self, monkeypatch):
        from hpc_agent.infra import clusters as clusters_mod

        monkeypatch.setattr(clusters_mod, "load_clusters_config", lambda *a, **k: {})
        clock = FakeClock()
        detail = _preamble_timeout_detail()
        _drive_one_livelock_cycle(clock, detail=detail)
        for _ in range(CIRCUIT_THRESHOLD):
            record_connection_failure(TARGET, detail=detail, clock=clock)
        advice = ssh_circuit.degradation_advice(HOST, _state(), now=clock.now)
        assert advice is not None and "settle-run" in advice


def test_open_error_carries_structured_host_and_deadline():
    """The fail-fast error attaches ``host`` + ``deadline`` as structured
    attributes — consumers (``harvest_on_terminal``'s bounded wait-and-retry)
    must never parse the message for the cooldown deadline."""
    clock = FakeClock()
    _fail_n(clock, CIRCUIT_THRESHOLD)
    with pytest.raises(SshCircuitOpen) as ei:
        check_circuit(TARGET, clock=clock)
    assert ei.value.host == HOST
    assert ei.value.deadline == pytest.approx(clock.now + CYCLE1_COOLDOWN_SEC)
    # The class-level defaults keep bare construction (older sites) valid.
    bare = SshCircuitOpen("bare")
    assert bare.host == ""
    assert bare.deadline is None


def test_cluster_scheduler_scratch_lockstep_with_host_retarget():
    """The breaker's failover-equivalence signature stays in lock-step with
    ``ops.host_retarget._cluster_scheduler_scratch`` (the MIRROR pin): both
    must derive the identical ``(scheduler, scratch)`` pair from a raw
    ``clusters.yaml`` entry, or sibling suggestions and the retarget gate
    would disagree about what counts as a failover."""
    from hpc_agent.ops.host_retarget import (
        _cluster_scheduler_scratch as retarget_signature,
    )

    cases = [
        {"scheduler": "slurm", "scratch": "/scratch1"},
        {"scheduler": " sge ", "scratch": " /u/scratch "},
        {"scheduler": None, "scratch": None},
        {},
        {"scheduler": "slurm"},
        {"scratch": "/scratch1", "host": "x.usc.edu"},
    ]
    for cfg in cases:
        assert ssh_circuit._cluster_scheduler_scratch(cfg) == retarget_signature(cfg)


# ---------------------------------------------------------------------------
# Byte-compat: a pre-schedule (flat-300) state file must stay readable
# ---------------------------------------------------------------------------


def test_legacy_state_file_reads_with_flat_300_default():
    """A pre-schedule open state file (flat ``cooldown_sec=300``, and none of the
    new additive fields — ``reopen_cycles`` / ``suspected_cause`` /
    ``recent_establishments``) stays readable: its stored 300 cooldown is honored
    (deadline = opened_at + 300), a doc missing ``cooldown_sec`` falls back to the
    legacy :data:`BASE_COOLDOWN_SEC` default, and claiming the probe on it does
    not crash on the absent fields."""
    clock = FakeClock()
    path = circuit_state_path(HOST)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema_version": 1,
        "host": HOST,
        "state": "open",
        "consecutive_failures": 3,
        "cooldown_sec": 300.0,
        "opened_at": clock.now,
        "probe_claimed_at": None,
        "last_failure": None,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    # Within the stored 300s cooldown → fail fast at the legacy deadline.
    with pytest.raises(SshCircuitOpen):
        check_circuit(TARGET, clock=clock)
    assert ssh_circuit.open_deadline(legacy, now=clock.now) == clock.now + 300.0
    assert (
        ssh_circuit.open_deadline({"state": "open", "opened_at": clock.now}, now=clock.now)
        == clock.now + BASE_COOLDOWN_SEC
    )
    # Past the stored cooldown → half-open eligible; claims the probe, no crash on
    # the missing additive fields.
    clock.advance(301)
    check_circuit(TARGET, clock=clock)
    assert _state()["probe_claimed_at"] == pytest.approx(clock.now)


# ---------------------------------------------------------------------------
# Demand-driven half-open probe (B1): a cheap inline liveness check
# ---------------------------------------------------------------------------


class TestDemandProbe:
    """A caller hitting a half-open-eligible circuit may hand check_circuit a
    cheap ``ssh true``-class ``probe_fn`` — a passing probe closes the circuit and
    lets the caller proceed; a failing probe re-opens at the next cycle and fails
    the caller fast, without gambling an expensive real command as the probe."""

    def _open_and_lapse(self, clock: FakeClock) -> None:
        _fail_n(clock, CIRCUIT_THRESHOLD)  # fresh open → cycle 1 (15s)
        clock.advance(CYCLE1_COOLDOWN_SEC + 1)  # min-wait elapsed

    def test_successful_demand_probe_closes_and_caller_proceeds(self):
        clock = FakeClock()
        self._open_and_lapse(clock)
        check_circuit(TARGET, clock=clock, probe_fn=lambda: _cp(returncode=0))
        assert _state()["state"] == "closed"  # probe closed it; no raise → proceed
        check_circuit(TARGET, clock=clock)  # closed now: no raise

    def test_failed_demand_probe_reopens_at_next_cycle_and_fails_fast(self):
        clock = FakeClock()
        self._open_and_lapse(clock)
        down = _cp(stderr="ssh: connect to host x port 22: Connection timed out", returncode=255)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock, probe_fn=lambda: down)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["cooldown_sec"] == CYCLE2_COOLDOWN_SEC  # escalated to cycle 2
        assert doc["probe_claimed_at"] is None  # slot cleared for the next cycle

    def test_timed_out_demand_probe_reopens_at_next_cycle(self):
        clock = FakeClock()
        self._open_and_lapse(clock)

        def boom():
            raise TimeoutError("ssh to host timed out after 5s")

        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock, probe_fn=boom)
        assert _state()["cooldown_sec"] == CYCLE2_COOLDOWN_SEC

    def test_inconclusive_demand_probe_records_nothing_and_fails_fast(self):
        """An rc≠255 non-zero probe (a wrapper-style status) proved NOTHING:
        it neither closes the circuit (no success record — the old None-path
        false "reached the host" claim) nor escalates (no failure count, no
        cycle bump, cooldown unchanged); the caller fails fast and the
        claimed slot lapses at PROBE_CLAIM_TTL_SEC."""
        clock = FakeClock()
        self._open_and_lapse(clock)
        before = _state()
        wrapper_rc = _cp(stderr="rsync: connection unexpectedly closed", returncode=12)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock, probe_fn=lambda: wrapper_rc)
        doc = _state()
        assert doc["state"] == "open"  # NOT closed on an unproven host
        assert doc["consecutive_failures"] == before["consecutive_failures"]  # no count
        assert doc["reopen_cycles"] == before["reopen_cycles"]  # no escalation
        assert doc["cooldown_sec"] == CYCLE1_COOLDOWN_SEC  # cycle-1 lane unchanged

    def test_concurrent_caller_fails_fast_while_probe_claimed(self):
        """Single-flight: with the slot already claimed, a concurrent caller fails
        fast (in-flight message) and its probe_fn never runs — no thundering herd."""
        clock = FakeClock()
        self._open_and_lapse(clock)
        check_circuit(TARGET, clock=clock)  # first claimant holds the slot
        ran = {"n": 0}

        def probe():
            ran["n"] += 1
            return _cp(returncode=0)

        with pytest.raises(SshCircuitOpen) as ei:
            check_circuit(TARGET, clock=clock, probe_fn=probe)
        assert "probe" in str(ei.value)
        assert ran["n"] == 0  # the concurrent caller never opened a connection

    def test_pre_min_wait_caller_fails_fast_without_probing(self):
        """Before the cooldown lapses the caller fails fast and the probe_fn is
        never invoked — the demand probe is DEMAND-driven, gated on min-wait."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # open, cooldown 15s, NOT yet lapsed
        ran = {"n": 0}

        def probe():
            ran["n"] += 1
            return _cp(returncode=0)

        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock, probe_fn=probe)
        assert ran["n"] == 0


# ---------------------------------------------------------------------------
# liveness_probe: the production demand probe (docket #5) — a cheap
# ``ssh <target> true`` through the bounded capture runner DIRECTLY
# ---------------------------------------------------------------------------


class TestLivenessProbe:
    """The concrete probe factory guarded_call callers wire in. Hard boundaries
    pinned: it invokes the bounded runner directly (NEVER re-entering
    check_circuit / guarded_call — a nested check raises probe-in-flight off
    the caller's own claim stamp) and takes no ssh slot."""

    def test_probe_invokes_bounded_runner_directly_with_expected_argv(self, monkeypatch):
        from hpc_agent.infra import remote

        calls = []

        def fake_capture(argv, *, timeout):
            calls.append((argv, timeout))
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(remote, "capture_via_select", fake_capture)

        def _boom(*_a, **_k):  # the probe must never re-enter the breaker
            raise AssertionError("liveness_probe re-entered the breaker")

        monkeypatch.setattr(ssh_circuit, "check_circuit", _boom)
        monkeypatch.setattr(ssh_circuit, "guarded_call", _boom)

        cp = ssh_circuit.liveness_probe(TARGET)()
        assert cp.returncode == 0
        (argv, timeout), *rest = calls
        assert not rest  # exactly one bounded dial
        assert timeout == 15.0  # the bounded default
        assert argv[-2:] == [TARGET, "true"]  # the cheap liveness command
        assert "BatchMode=yes" in argv  # fails fast, never hangs on a prompt

    def test_probe_timeout_surfaces_as_builtin_timeout_error(self, monkeypatch):
        """The demand-probe contract: _run_demand_probe records a raised
        built-in TimeoutError as a connection failure — the probe translates
        the runner's subprocess.TimeoutExpired, never leaks it."""
        from hpc_agent.infra import remote

        def fake_capture(argv, *, timeout):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

        monkeypatch.setattr(remote, "capture_via_select", fake_capture)
        with pytest.raises(TimeoutError):
            ssh_circuit.liveness_probe(TARGET, timeout_sec=3)()

    def test_probe_failure_cp_is_classified_as_connection_failure(self, monkeypatch):
        """A refused dial returns its CompletedProcess (rc 255 + marker) so
        _run_demand_probe classifies it as a connection failure and re-opens —
        the probe never raises on an ordinary non-zero exit."""
        from hpc_agent.infra import remote

        monkeypatch.setattr(
            remote,
            "capture_via_select",
            lambda argv, *, timeout: subprocess.CompletedProcess(
                argv, 255, "", "ssh: connect to host x port 22: Connection refused"
            ),
        )
        cp = ssh_circuit.liveness_probe(TARGET)()
        assert cp.returncode == 255
        assert classify_connection_failure(cp) is not None


class TestDemandProbeWiring:
    """The production guarded_call callers pass a probe_fn, so a half-open
    circuit probes cheaply instead of gambling the real (possibly expensive)
    command as the probe (docket #5). Zero fast-path cost: the probe only runs
    on a claimed half-open slot, which TestDemandProbe already pins."""

    def test_remote_guarded_path_passes_liveness_probe_fn(self, monkeypatch):
        from hpc_agent.infra import remote

        monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
        captured = {}

        def fake_guarded(ssh_target, fn, **kwargs):
            captured.update(kwargs)
            return fn()

        monkeypatch.setattr(ssh_circuit, "guarded_call", fake_guarded)
        dialed = []
        monkeypatch.setattr(
            remote,
            "capture_via_select",
            lambda argv, *, timeout: (
                dialed.append(argv) or subprocess.CompletedProcess(argv, 0, "ok", "")
            ),
        )
        remote.ssh_run("qstat", ssh_target=TARGET)

        probe_fn = captured.get("probe_fn")
        assert callable(probe_fn)  # a probe is handed in, never None
        probe_fn()  # invoking it dials the cheap liveness argv for THIS target
        assert dialed[-1][-2:] == [TARGET, "true"]


# ---------------------------------------------------------------------------
# Storm-aware attribution (B3): disclosure + short-lane selection
# ---------------------------------------------------------------------------


class TestSelfStormAttribution:
    """A self-inflicted connection burst (≥ STORM_ESTABLISHMENT_THRESHOLD local
    establishment failures in the trailing STORM_WINDOW_SEC at the OPEN
    transition) is stamped ``suspected_cause="self-storm"``, disclosed in the
    refusal message, and holds the short cycle-1 cooldown while it correlates.
    Disclosure only — the evidence/verdict rules are untouched."""

    def _storm_trip(self, clock: FakeClock) -> None:
        # Seed the ring with establishment failures WITHOUT opening (successes
        # keep the consecutive counter low and CARRY the ring forward), then a
        # final consecutive burst that opens the circuit — the storm signature.
        for _ in range(2):
            _fail_n(clock, CIRCUIT_THRESHOLD - 1)  # 2 fails: counter < threshold
            record_connection_success(TARGET)  # counter → 0, ring carried
        _fail_n(clock, CIRCUIT_THRESHOLD)  # final 3 → opens (ring ≥ 6)

    def test_storm_stamps_cause_and_holds_short_lane_on_reopen(self):
        clock = FakeClock()
        self._storm_trip(clock)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["suspected_cause"] == ssh_circuit.SELF_STORM_CAUSE
        assert doc["cooldown_sec"] == CYCLE1_COOLDOWN_SEC
        # Re-open while the burst still correlates (probe fails within the window).
        clock.advance(CYCLE1_COOLDOWN_SEC + 0.1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_failure(TARGET, detail="still storming", clock=clock)
        doc = _state()
        assert doc["reopen_cycles"] == 2  # cycle advanced …
        assert doc["suspected_cause"] == ssh_circuit.SELF_STORM_CAUSE
        assert doc["cooldown_sec"] == CYCLE1_COOLDOWN_SEC  # … but the SHORT lane held

    def test_storm_disclosure_rides_the_refusal_message(self):
        clock = FakeClock()
        self._storm_trip(clock)
        with pytest.raises(SshCircuitOpen) as ei:
            check_circuit(TARGET, clock=clock)  # still within the cycle-1 cooldown
        msg = str(ei.value)
        assert "self-inflicted connection burst" in msg
        assert "retrying sooner" in msg

    def test_lone_trip_is_not_a_storm_and_escalates_normally(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)  # 3 consecutive, ring = 3 < threshold
        assert _state()["suspected_cause"] is None
        clock.advance(CYCLE1_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)
        record_connection_failure(TARGET, detail="still down", clock=clock)
        assert _state()["cooldown_sec"] == CYCLE2_COOLDOWN_SEC  # escalated, no hold

    def test_storm_decays_and_escalation_resumes_once_window_lapses(self):
        clock = FakeClock()
        self._storm_trip(clock)
        assert _state()["suspected_cause"] == ssh_circuit.SELF_STORM_CAUSE
        # Age the whole burst out of the window before the re-open.
        clock.advance(ssh_circuit.STORM_WINDOW_SEC + CYCLE1_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_failure(TARGET, detail="probe failed post-storm", clock=clock)
        doc = _state()
        assert doc["suspected_cause"] is None  # correlation lapsed
        assert doc["cooldown_sec"] == CYCLE2_COOLDOWN_SEC  # normal escalation resumed
