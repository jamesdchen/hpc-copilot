"""Worked example proving the shared ``tests/_ssh_fakes.py`` fake behaves.

This is the ADDITIVE demonstrator for the shared SSH fake — it does not migrate
any existing per-file closure. It pins the three properties a caller relies on:
substring dispatch + recording, ack-sentinel round-tripping against the *real*
``wrap_with_ack`` / ``split_ack`` transport helpers, and stateful rules.
"""

from __future__ import annotations

from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack
from tests._ssh_fakes import (
    SCHED_ACK,
    WAVE_ACK,
    FakeSSH,
    Reply,
    ack_line,
    completed,
    stateful_crontab,
)


def test_substring_rules_first_match_and_recording() -> None:
    fake = FakeSSH(
        rules=[
            ("scrontab -l", Reply(stdout="no scrontab\n", returncode=1)),
            ("crontab -l", Reply(stdout="deny\n", returncode=1)),
        ]
    )
    # Longer needle wins because it is ordered first — "crontab -l" is a
    # substring of "scrontab -l".
    r = fake("scrontab -l 2>&1 || true", ssh_target="u@h")
    assert r.returncode == 1 and "no scrontab" in r.stdout
    # Unmatched command → default rc-0 empty success.
    assert fake("mkdir -p /x", ssh_target="u@h").returncode == 0
    assert fake.dispatched("scrontab -l") == ["scrontab -l 2>&1 || true"]
    assert fake.sent_once("mkdir -p /x")


def test_auto_ack_round_trips_through_real_transport_helpers() -> None:
    """The fake echoes exactly the sentinel ``wrap_with_ack`` asked for, and the
    production ``split_ack`` parser recovers the rule's rc and clean stdout."""
    fake = FakeSSH(rules=[("squeue", Reply(stdout="jobline\n", returncode=1))])
    cmd = wrap_with_ack("squeue -u me", SCHED_ACK)  # …; echo "__HPC_SCHED_ACK__=$?"

    out = fake(cmd, ssh_target="u@h").stdout
    clean, rc = split_ack(out, SCHED_ACK)

    assert clean == "jobline\n"
    assert rc == 1  # the rule's rc rode the ack, proving the channel "ran"


def test_auto_ack_handles_bare_affirmation_token() -> None:
    fake = FakeSSH()  # default rule, auto-ack on
    cmd = f"cd /proj && printf '%s\\n' {WAVE_ACK}; ls waves"
    out = fake(cmd, ssh_target="u@h").stdout
    assert WAVE_ACK in out  # affirmation token present → the listing "ran"


def test_auto_ack_can_be_suppressed_for_silence_case() -> None:
    """A rc-0 but ack-LESS read is the deliberate UNKNOWN / truncated-channel
    signal; ``auto_ack=False`` models it without the fake helpfully adding one."""
    fake = FakeSSH(rules=[("squeue", Reply(returncode=0, auto_ack=False))])
    out = fake(wrap_with_ack("squeue", SCHED_ACK), ssh_target="u@h").stdout
    _clean, rc = split_ack(out, SCHED_ACK)
    assert rc is None  # no ack line → channel silence


def test_raw_completedprocess_passes_through_verbatim() -> None:
    fake = FakeSSH(rules=[("weird", completed("raw\n", returncode=7))])
    r = fake("weird cmd; echo __HPC_SCHED_ACK__=$?", ssh_target="u@h")
    assert r.returncode == 7 and r.stdout == "raw\n"  # no ack appended


def test_stateful_crontab_rule_reflects_prior_install() -> None:
    fake = FakeSSH(rules=[("crontab", stateful_crontab())])
    # No crontab yet → -l is a denial with rc 1.
    assert fake("crontab -l 2>&1 || true", ssh_target="u@h").returncode == 1
    # Install a line, then a subsequent -l reflects it (state persisted in store).
    fake("printf 'line\\n' | crontab -", ssh_target="u@h")
    reread = fake("crontab -l", ssh_target="u@h")
    assert reread.returncode == 0 and "crontab -" in reread.stdout


def test_ack_line_builder_shapes() -> None:
    assert ack_line(SCHED_ACK, 2) == "__HPC_SCHED_ACK__=2\n"
    assert ack_line(WAVE_ACK) == "__HPC_WAVE_ACK__\n"
