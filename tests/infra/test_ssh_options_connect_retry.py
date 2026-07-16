"""Connect-failure retry schedule + retry-safe classification (latency rank 25)."""

from __future__ import annotations

import pytest

from hpc_agent.infra import ssh_options


def test_connect_retry_schedule_is_tight():
    # Two attempts (one initial + one retry), each dial bounded by ConnectTimeout
    # (15s) — ~2x15s dead-host detection, not the command ladder's 3-5x60s.
    delays = ssh_options.connect_failure_retry_delays()
    assert delays == (2.0,)
    assert len(delays) == 1  # one RETRY => two attempts


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
def test_is_connect_failure_true_for_dial_errors(stderr):
    assert ssh_options.is_connect_failure(255, stderr) is True


def test_is_connect_failure_false_on_success():
    assert ssh_options.is_connect_failure(0, "Connection refused") is False


def test_is_connect_failure_false_for_remote_command_error():
    # An authenticated command that failed (remote tar/find error) is NOT a
    # connect failure and must not be re-dialed.
    assert ssh_options.is_connect_failure(2, "tar: cannot open: No such file") is False


def test_retry_safe_false_on_spawn_error():
    # ENOENT launching ssh/tar — deterministic, never retry-safe.
    assert ssh_options.is_retry_safe(127, "", spawn_error=True) is False


def test_retry_safe_false_on_rsync_exit_12():
    # rsync protocol/stream error: a re-dial does not fix a broken negotiation.
    assert ssh_options.is_retry_safe(12, "rsync error: files could not be transferred") is False


def test_retry_safe_true_on_connect_failure():
    assert ssh_options.is_retry_safe(255, "Connection refused") is True


def test_retry_safe_false_on_plain_command_failure():
    assert ssh_options.is_retry_safe(2, "remote command failed") is False
