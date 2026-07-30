"""The flap-riding stage retry: resume the delta, respect the breaker, disclose.

2026-07-30: a flapping VPN severed the staging push and KILLED the detached
worker. The push is delta-based, so the bytes that landed were banked and a
second attempt would have shipped only the remainder — but nothing retried, so
the human re-fired by hand and paid a full re-ship each time.

Under test: a flap retries and resumes; a non-flap does NOT (byte-identical
first-attempt behaviour); and the retry never opens a connection the circuit
breaker would refuse.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.ops import submit_flow

TARGET = "someone@hoffman2.idre.ucla.edu"


@pytest.fixture()
def _no_sleep() -> list[float]:
    return []


def _run_retry(attempts_script, *, slept: list[float]):  # type: ignore[no-untyped-def]
    calls = {"n": 0}

    def _run() -> str | None:
        calls["n"] += 1
        outcome = attempts_script[calls["n"] - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    result = submit_flow._stage_with_flap_retry(
        _run, ssh_target=TARGET, sleep=lambda s: slept.append(s)
    )
    return result, calls["n"]


# ── the flap fixture: fail attempt 1, succeed attempt 2, ONE invocation ──────


def test_flap_retries_within_one_invocation_and_resumes(_no_sleep: list[float]) -> None:
    """Attempt 1 dies on a severed link; attempt 2 succeeds. One worker, no re-fire."""
    script = [TimeoutError("ssh to host timed out after 60s: rsync"), "code-tree-sha"]
    result, n = _run_retry(script, slept=_no_sleep)
    assert result == "code-tree-sha"
    assert n == 2, "the flap must be retried inside the SAME invocation"
    assert _no_sleep, "a retry must pace itself, not hammer"


def test_retry_discloses_attempt_count_and_breaker_state(
    caplog: pytest.LogCaptureFixture, _no_sleep: list[float]
) -> None:
    """Every retry is disclosed with its attempt count, the breaker, and the delta."""
    caplog.set_level("WARNING", logger="hpc_agent.ops.submit_flow")
    _run_retry([TimeoutError("link severed"), "ok"], slept=_no_sleep)
    text = caplog.text
    assert "attempt 1/3" in text, "the attempt count must be legible in the worker log"
    assert "breaker[" in text, "the breaker state read at retry time must be disclosed"
    assert "delta-based" in text and "banked" in text, "the resume semantics must be stated"


def test_connect_marked_failure_is_treated_as_a_flap(_no_sleep: list[float]) -> None:
    """A transport-marked staging failure retries; the marker is the discriminator."""
    exc = errors.RemoteCommandFailed("rsync push failed (exit 255): connection reset by peer")
    result, n = _run_retry([exc, "sha"], slept=_no_sleep)
    assert (result, n) == ("sha", 2)


# ── a non-flap must behave EXACTLY as it did before the retry existed ────────


def test_non_flap_failure_raises_on_attempt_one_unchanged(_no_sleep: list[float]) -> None:
    """A broken executor / bad reducer must not be retried — same type, same message."""
    exc = errors.RemoteCommandFailed("rsync push failed (exit 23): No such file or directory")
    with pytest.raises(errors.RemoteCommandFailed) as excinfo:
        _run_retry([exc, "never-reached"], slept=_no_sleep)
    assert excinfo.value is exc, "a non-flap must propagate the ORIGINAL exception object"
    assert _no_sleep == [], "a non-flap must not pace, because it must not retry"


def test_rsync_protocol_error_is_not_retried(_no_sleep: list[float]) -> None:
    """rsync exit 12 is a protocol error — re-dialling cannot fix it (ssh_options)."""
    exc = errors.RemoteCommandFailed("rsync push failed (exit 12): connection reset by peer")
    with pytest.raises(errors.RemoteCommandFailed):
        _run_retry([exc, "never"], slept=_no_sleep)
    assert _no_sleep == []


def test_healthy_stage_runs_exactly_once(_no_sleep: list[float]) -> None:
    """The healthy path is byte-identical: one call, no pacing, no disclosure."""
    result, n = _run_retry(["sha"], slept=_no_sleep)
    assert (result, n, _no_sleep) == ("sha", 1, [])


# ── breaker respect: never open more connections than the circuit allows ─────


def test_exhaustion_raises_the_discriminated_cause_shape(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """Exhausted retries fail with the L2 cause vocabulary, not a bare timeout."""
    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 2)
    with pytest.raises(errors.SshUnreachable) as excinfo:
        _run_retry([TimeoutError("flap"), TimeoutError("flap")], slept=_no_sleep)
    message = str(excinfo.value)
    assert "after 2 bounded attempt(s)" in message
    assert "delta-based" in message, "exhaustion must still say progress was not lost"


def test_open_circuit_beyond_patience_stops_retrying(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """A long cooldown STOPS the ladder — never sleep through a ban fence."""
    monkeypatch.setattr(submit_flow, "_stage_retry_wait_sec", lambda _t, *, attempt: None)
    attempted = {"n": 0}

    def _run() -> str | None:
        attempted["n"] += 1
        raise TimeoutError("flap")

    with pytest.raises(errors.SshUnreachable):
        submit_flow._stage_with_flap_retry(
            _run, ssh_target=TARGET, sleep=lambda s: _no_sleep.append(s)
        )
    assert attempted["n"] == 1, "an over-long cooldown must stop the ladder, not sleep it out"
    assert _no_sleep == []


def test_wait_comes_from_the_circuits_own_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry NEVER picks its own wait while the circuit is open — it reads it.

    Bypassing the cooldown is precisely the ban-risk the breaker exists to stop,
    so the ladder's wait must be the circuit's remaining cooldown verbatim.
    """
    monkeypatch.setattr("hpc_agent.infra.ssh_circuit.cooldown_remaining_sec", lambda _h, **_k: 7.5)
    assert submit_flow._stage_retry_wait_sec(TARGET, attempt=1) == 7.5


def test_closed_circuit_uses_the_short_local_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hpc_agent.infra.ssh_circuit.cooldown_remaining_sec", lambda _h, **_k: 0.0)
    wait = submit_flow._stage_retry_wait_sec(TARGET, attempt=1)
    assert wait in submit_flow._STAGE_RETRY_DELAYS_SEC


def test_cooldown_past_the_cap_refuses_to_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hpc_agent.infra.ssh_circuit.cooldown_remaining_sec",
        lambda _h, **_k: submit_flow._STAGE_RETRY_MAX_BREAKER_WAIT_SEC + 1.0,
    )
    assert submit_flow._stage_retry_wait_sec(TARGET, attempt=1) is None
