"""Connect-failure retry schedule + retry-safe classification (latency rank 25).

The classifier is LEG-AWARE (the rc==255 doctrine shared with
``ssh_circuit.classify_connection_failure`` and ``remote._is_throttle_failure``):
on a ``remote-command`` leg a remote process rides the ssh session, so its rc
and stderr are remote-controlled content and the connect markers are believed
only at ssh's reserved client exit 255; on a ``pure-connect`` leg the ssh
process IS the connection, so any non-zero exit is transport evidence.
"""

from __future__ import annotations

import inspect

import pytest

from hpc_agent.infra import ssh_options
from hpc_agent.infra.ssh_options import ConnectLegKind

LEGS: tuple[ConnectLegKind, ...] = ("pure-connect", "remote-command")


def test_connect_retry_schedule_is_tight():
    # Two attempts (one initial + one retry), each dial bounded by ConnectTimeout
    # (15s) — ~2x15s dead-host detection, not the command ladder's 3-5x60s.
    delays = ssh_options.connect_failure_retry_delays()
    assert delays == (2.0,)
    assert len(delays) == 1  # one RETRY => two attempts


@pytest.mark.parametrize("leg", LEGS)
@pytest.mark.parametrize(
    "stderr",
    [
        "ssh: connect to host h port 22: Connection refused",
        "ssh: connect to host h port 22: Connection timed out",
        "Connection reset by peer",
        "ssh: Could not resolve hostname h: Name or service not known",
        "No route to host",
        "kex_exchange_identification: Connection closed by remote host",
    ],
)
def test_is_connect_failure_true_for_dial_errors(leg, stderr):
    # At ssh's reserved client exit 255 the dial itself failed before any
    # remote command ran — the markers are transport evidence on BOTH leg kinds.
    assert ssh_options.is_connect_failure(255, stderr, leg=leg) is True


@pytest.mark.parametrize("leg", LEGS)
def test_is_connect_failure_false_on_success(leg):
    assert ssh_options.is_connect_failure(0, "Connection refused", leg=leg) is False


def test_is_connect_failure_false_for_remote_command_error():
    # An authenticated command that failed (remote tar/find error) is NOT a
    # connect failure and must not be re-dialed.
    assert (
        ssh_options.is_connect_failure(2, "tar: cannot open: No such file", leg="remote-command")
        is False
    )


# --- leg-aware gate (rc==255 doctrine; sibling: classify_connection_failure) ---


@pytest.mark.parametrize("returncode", [1, 2, 126])
def test_remote_command_leg_marker_at_non_255_is_not_connect_failure(returncode):
    # THE DEFECT: on the tar|ssh pull leg the remote tar/find RIDES the session,
    # so rc=1 with marker-shaped stderr is remote content over a HEALTHY channel
    # (2026-07-19 scheduler-integration incident: a dead qmaster made every qsub
    # leg exit 1 with commlib "Connection refused" in REMOTE stderr) — never
    # dial evidence, so no tight redial.
    assert (
        ssh_options.is_connect_failure(returncode, "Connection refused", leg="remote-command")
        is False
    )
    assert (
        ssh_options.is_retry_safe(returncode, "Connection refused", leg="remote-command") is False
    )


def test_remote_command_leg_marker_at_255_is_connect_failure():
    assert ssh_options.is_connect_failure(255, "Connection refused", leg="remote-command") is True
    assert ssh_options.is_retry_safe(255, "Connection refused", leg="remote-command") is True


@pytest.mark.parametrize("returncode", [1, 2, 126])
def test_pure_connect_leg_marker_at_any_nonzero_rc_stays_connect_failure(returncode):
    # On a bare `ssh host true`-class probe the ssh process IS the connection:
    # any non-zero exit is transport evidence, so the pre-gate behavior stands.
    assert (
        ssh_options.is_connect_failure(returncode, "Connection refused", leg="pure-connect") is True
    )
    assert ssh_options.is_retry_safe(returncode, "Connection refused", leg="pure-connect") is True


def test_unknown_leg_kind_refused():
    # No silent default: an undeclared leg kind is a loud error, never a guess.
    with pytest.raises(ValueError, match="unknown connect leg kind"):
        ssh_options.is_connect_failure(
            255,
            "Connection refused",
            leg="carrier-pigeon",  # type: ignore[arg-type]
        )


def test_leg_has_no_silent_default():
    # Mutation-2 closure (verifier F): the leg must be DECLARED at every call
    # site. If a default sneaks back in, an undeclared leg gets a GUESSED
    # classification — exactly the hole the leg-aware gate closes — and the
    # rest of this suite would stay green.
    for fn in (ssh_options.is_connect_failure, ssh_options.is_retry_safe):
        assert inspect.signature(fn).parameters["leg"].default is inspect.Parameter.empty


def test_retry_safe_false_on_spawn_error():
    # ENOENT launching ssh/tar — deterministic, never retry-safe.
    assert ssh_options.is_retry_safe(127, "", leg="remote-command", spawn_error=True) is False


def test_retry_safe_false_on_rsync_exit_12():
    # rsync protocol/stream error: a re-dial does not fix a broken negotiation.
    assert (
        ssh_options.is_retry_safe(
            12, "rsync error: files could not be transferred", leg="remote-command"
        )
        is False
    )


@pytest.mark.parametrize("leg", LEGS)
def test_retry_safe_true_on_connect_failure(leg):
    assert ssh_options.is_retry_safe(255, "Connection refused", leg=leg) is True


def test_retry_safe_false_on_plain_command_failure():
    assert ssh_options.is_retry_safe(2, "remote command failed", leg="remote-command") is False
