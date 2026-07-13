"""rc=124 (server-side ``timeout`` expiry) is classified SAFELY, never as broken-env.

run-12 finding 20 LAYER 1 wraps every remote command in ``timeout <deadline>s``,
which exits **124** when it fires. The poll-failure classifiers key the
deterministic broken-env fast-escalation on rc **126/127** ("command not
found"/"not executable"); a 124 must fall through to the transient bucket so a
timed-out poll rides the wait budget (the connection breaker's domain) instead
of being mistaken for an unhealable env fault. These pin that 124 is transient
and that 126/127 stay deterministic (regression guard).
"""

from __future__ import annotations

from hpc_agent import errors
from hpc_agent.ops.monitor_flow import _is_deterministic_env_failure
from hpc_agent.ops.verify_canary import _classify_poll_failure


def _rcf(rc: int) -> errors.RemoteCommandFailed:
    return errors.RemoteCommandFailed("remote command failed", returncode=rc)


def test_canary_rc124_is_transient_not_deterministic_env() -> None:
    assert _classify_poll_failure(_rcf(124)) == "transient"


def test_canary_rc126_127_stay_deterministic_env() -> None:
    assert _classify_poll_failure(_rcf(126)) == "deterministic_env"
    assert _classify_poll_failure(_rcf(127)) == "deterministic_env"


def test_canary_other_rc_is_transient() -> None:
    assert _classify_poll_failure(_rcf(1)) == "transient"
    assert _classify_poll_failure(_rcf(125)) == "transient"  # timeout's own failure


def test_monitor_rc124_is_not_deterministic_env() -> None:
    assert _is_deterministic_env_failure(_rcf(124)) is False


def test_monitor_rc126_127_are_deterministic_env() -> None:
    assert _is_deterministic_env_failure(_rcf(126)) is True
    assert _is_deterministic_env_failure(_rcf(127)) is True
