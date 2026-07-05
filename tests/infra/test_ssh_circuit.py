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
    MAX_COOLDOWN_SEC,
    PROBE_CLAIM_TTL_SEC,
    check_circuit,
    circuit_state_path,
    classify_connection_failure,
    guarded_call,
    record_connection_failure,
    record_connection_success,
)

HOST = "login.cluster.edu"
TARGET = f"user@{HOST}"


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

    def test_probe_failure_reopens_with_doubled_cooldown(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim the probe
        record_connection_failure(TARGET, detail="still down", clock=clock)
        doc = _state()
        assert doc["state"] == "open"
        assert doc["cooldown_sec"] == BASE_COOLDOWN_SEC * 2
        assert doc["probe_claimed_at"] is None
        # Not yet past the NEW deadline: fail fast.
        clock.advance(BASE_COOLDOWN_SEC + 1)
        with pytest.raises(SshCircuitOpen):
            check_circuit(TARGET, clock=clock)

    def test_exponential_cooldown_progression_capped(self):
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        expected = BASE_COOLDOWN_SEC
        for _ in range(6):  # 300 → 600 → 1200 → 2400 → 3600 → 3600 ...
            assert _state()["cooldown_sec"] == expected
            clock.advance(expected + 1)
            check_circuit(TARGET, clock=clock)  # half-open probe
            record_connection_failure(TARGET, detail="still down", clock=clock)
            expected = min(expected * 2, MAX_COOLDOWN_SEC)
        assert _state()["cooldown_sec"] == MAX_COOLDOWN_SEC

    def test_stale_probe_claim_is_reclaimable(self):
        """A claimant that died must not wedge the circuit open forever."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        clock.advance(BASE_COOLDOWN_SEC + 1)
        check_circuit(TARGET, clock=clock)  # claim, then "crash"
        clock.advance(PROBE_CLAIM_TTL_SEC + 1)
        check_circuit(TARGET, clock=clock)  # reclaimed: no raise

    def test_straggler_failure_while_open_does_not_double_cooldown(self):
        """A concurrent in-flight failure (no probe claimed) is evidence only."""
        clock = FakeClock()
        _fail_n(clock, CIRCUIT_THRESHOLD)
        record_connection_failure(TARGET, detail="straggler", clock=clock)
        assert _state()["cooldown_sec"] == BASE_COOLDOWN_SEC


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
            patch("hpc_agent.infra.remote._capture_via_select") as seam,
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
            patch("hpc_agent.infra.remote._capture_via_select") as seam,
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
        with patch("hpc_agent.infra.remote._capture_via_select") as seam:
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
        _fail_n(clock, CIRCUIT_THRESHOLD)
        doc = _state()
        assert doc["state"] == "open"
        assert ssh_circuit.effective_state(doc, now=clock.now) == "open"
        clock.advance(BASE_COOLDOWN_SEC - 1)
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
