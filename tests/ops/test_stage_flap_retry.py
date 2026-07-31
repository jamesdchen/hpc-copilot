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
    assert "exhausted 2 bounded attempt(s)" in message
    assert "delta-based" in message, "exhaustion must still say progress was not lost"


# ── the flap identity must SURVIVE exhaustion ────────────────────────────────
#
# The ladder is only ever REACHED on a flap (a non-flap propagates from attempt
# 1), so an exhausted ladder is by construction a flap outcome. The envelope
# used to be raised bare: ``__cause__`` was None and the stamp was never
# applied, so ``is_transport_flap`` — which walks the cause chain — answered
# False at exactly the moment the verdict matters most, and a downstream
# terminal-cause classifier read a known transport flap as a hard failure.


def test_exhausted_after_a_flap_still_reads_as_a_flap(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """The exhaustion envelope carries the flap verdict by IDENTITY.

    Uses a bare ``TimeoutError`` deliberately: it is a flap by TYPE and carries
    no stamp, so a ``__cause__`` walk alone would still answer False. Only the
    stamp on the envelope preserves the verdict here.
    """
    from hpc_agent.infra.ssh_options import is_transport_flap

    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 2)
    with pytest.raises(errors.SshUnreachable) as excinfo:
        _run_retry([TimeoutError("flap"), TimeoutError("flap")], slept=_no_sleep)

    assert is_transport_flap(excinfo.value) is True
    assert submit_flow._stage_failure_is_flap(excinfo.value) is True


def test_exhaustion_chains_the_last_failure_as_its_cause(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """``raise ... from last`` — the envelope must not orphan the failure that
    caused it, or a 2am traceback shows the summary and nothing underneath it."""
    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 2)
    last = TimeoutError("the second flap")
    with pytest.raises(errors.SshUnreachable) as excinfo:
        _run_retry([TimeoutError("the first flap"), last], slept=_no_sleep)
    assert excinfo.value.__cause__ is last


def test_a_stamped_last_failure_survives_exhaustion_through_the_chain(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """The other half: a STAMPED last failure (the L4 transport refusal) reaches
    ``is_transport_flap`` through the restored ``__cause__`` chain."""
    from hpc_agent.infra.ssh_options import is_transport_flap, mark_transport_flap

    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 1)
    stamped = mark_transport_flap(errors.SshUnreachable("probe severed"))
    with pytest.raises(errors.SshUnreachable) as excinfo:
        _run_retry([stamped], slept=_no_sleep)
    assert excinfo.value.__cause__ is stamped
    assert is_transport_flap(excinfo.value) is True


def test_stop_on_cooldown_exhaustion_also_keeps_the_flap_verdict(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """The ladder that STOPS on an over-long cooldown reached that stop because
    of a flap too — the verdict must not depend on which exit was taken."""
    from hpc_agent.infra.ssh_options import is_transport_flap

    monkeypatch.setattr(submit_flow, "_stage_retry_wait_sec", lambda _t, *, attempt: None)
    with pytest.raises(errors.SshUnreachable) as excinfo:
        _run_retry([TimeoutError("flap")], slept=_no_sleep)
    assert is_transport_flap(excinfo.value) is True


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


# ── L4 -> L3 COMPOSITION: the seam that shipped broken ───────────────────────
#
# The gap this closes: L4 raises a composed SshUnreachable promising "the bounded
# staging retry will re-attempt", but L3 classified via substring markers the
# composed message does not contain — so the promise was false and a severed
# manifest probe HARD-KILLED the worker on attempt 1, strictly worse than the
# full copy it replaced. Each leg was tested alone; nothing tested the seam.


def test_l4_transport_refusal_is_classified_a_flap() -> None:
    """The stamp — not the prose — is what L3 classifies on."""
    from hpc_agent.infra.ssh_options import mark_transport_flap

    exc = mark_transport_flap(
        errors.SshUnreachable(
            "the remote hash-manifest probe for /r failed on the transport "
            "(TimeoutError: severed); refusing to degrade a flap into a whole-tree re-ship."
        )
    )
    assert submit_flow._stage_failure_is_flap(exc) is True


def test_unstamped_lookalike_message_is_not_a_flap() -> None:
    """The stamp is load-bearing: the same prose WITHOUT it must not be retried.

    This is what makes the previous test a real assertion rather than one that
    would pass off the message text alone.
    """
    exc = errors.SshUnreachable(
        "the remote hash-manifest probe for /r failed on the transport "
        "(TimeoutError: severed); refusing to degrade a flap into a whole-tree re-ship."
    )
    assert submit_flow._stage_failure_is_flap(exc) is False


def test_a_stamped_cause_survives_rewrapping() -> None:
    """A wrapper that re-raises a stamped error keeps the verdict (``__cause__`` walk)."""
    from hpc_agent.infra.ssh_options import mark_transport_flap

    inner = mark_transport_flap(errors.SshUnreachable("probe severed"))
    try:
        raise errors.RemoteCommandFailed("staging leg failed") from inner
    except errors.RemoteCommandFailed as outer:
        assert submit_flow._stage_failure_is_flap(outer) is True


def test_l4_refusal_retries_and_attempt_two_ships_the_remainder(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """END-TO-END: severed manifest probe -> L3 retry -> attempt 2 ships the DELTA.

    Asserts the actual ship set, not the log text: attempt 2 must ship only the
    file that changed, proving the retry resumed against the delta rather than
    re-shipping the tree.
    """
    from hpc_agent.infra.ssh_options import mark_transport_flap

    shipped: list[list[str]] = []
    attempt = {"n": 0}

    def _stage() -> str | None:
        attempt["n"] += 1
        if attempt["n"] == 1:
            # Leg A (the remote hash walk) severed by the flap — L4's refusal.
            raise mark_transport_flap(
                errors.SshUnreachable(
                    "the remote hash-manifest probe for /r failed on the transport "
                    "(TimeoutError: read severed mid-manifest); refusing to degrade a "
                    "flap into a whole-tree re-ship."
                )
            ) from TimeoutError("read severed mid-manifest")
        # Link steadied: the manifest round-trip succeeds and the delta ships
        # only what actually differs.
        shipped.append(["changed.py"])
        return "code-tree-sha"

    result = submit_flow._stage_with_flap_retry(
        _stage, ssh_target=TARGET, sleep=lambda s: _no_sleep.append(s)
    )

    assert result == "code-tree-sha", "the worker must SURVIVE the flap, not die on it"
    assert attempt["n"] == 2, "L4's refusal must be retried by L3 — the promise it makes"
    assert shipped == [["changed.py"]], "attempt 2 ships only the remainder, not the tree"


def test_stop_on_cooldown_message_states_attempts_made_and_the_cause(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """The STOP message must not claim attempts it never made, nor hide the breaker.

    It used to say "after 3 bounded attempt(s)" when exactly ONE ran, and never
    mentioned the cooldown that stopped it — the log asserted the opposite of
    what happened, twice.
    """
    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 3)
    monkeypatch.setattr(submit_flow, "_stage_retry_wait_sec", lambda _t, *, attempt: None)

    def _run() -> str | None:
        raise TimeoutError("flap")

    with pytest.raises(errors.SshUnreachable) as excinfo:
        submit_flow._stage_with_flap_retry(
            _run, ssh_target=TARGET, sleep=lambda s: _no_sleep.append(s)
        )
    message = str(excinfo.value)
    assert "STOPPED after 1 of 3 allowed attempt(s)" in message
    assert "circuit for" in message and "cooldown longer than" in message
    assert "exhausted 3" not in message, "must never claim attempts it did not make"


def test_exhausted_path_never_dials(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
) -> None:
    """The naming pass is CONSULT-ONLY: it must not open connections the breaker just refused."""
    from hpc_agent.infra import readiness_sensors as rs

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("the exhausted path must not sense/dial the fenced host")

    monkeypatch.setattr(rs, "read_path_readiness", _boom)
    monkeypatch.setattr(rs, "_run_route_resolution", _boom)
    monkeypatch.setattr(rs, "tcp_connect", _boom)
    monkeypatch.setattr(submit_flow, "_stage_retry_attempts", lambda: 1)

    def _run() -> str | None:
        raise TimeoutError("flap")

    with pytest.raises(errors.SshUnreachable):
        submit_flow._stage_with_flap_retry(
            _run, ssh_target=TARGET, sleep=lambda s: _no_sleep.append(s)
        )
