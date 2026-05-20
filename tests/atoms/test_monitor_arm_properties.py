"""Property-based tests for ``hpc_agent.atoms.monitor_arm._seconds_to_cron``.

The function renders a cadence (in seconds) as a cron schedule string
for the ``decide-monitor-arm`` primitive. The implementation is a
straight branch on three regimes (≤60s, sub-hour, ≥hour) but the
output strings have to be valid 5-field cron, parseable by
``CronCreate``, and round-trip the cadence regime correctly. Examples
would have to enumerate boundary values by hand; properties cover the
input range without that bookkeeping.

Same rationale as ``test_cmd_sha_properties.py``: the function had no
direct test coverage, and a future "let me make these cron expressions
exact" or "switch to seconds-resolution scheduling" refactor could
silently break the format contract. Pinning here keeps the contract
machine-checked.
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from hpc_agent.atoms.monitor_arm import _seconds_to_cron

# ``_seconds_to_cron`` is only called with cadences from ``_DELAY_RULES``
# (60s..3600s today), but the function should handle arbitrary positive
# inputs without crashing — agents may pass through any cadence.
_cadence = st.integers(min_value=1, max_value=86400)  # 1s..24h

_FIVE_FIELD = re.compile(r"^[^\s]+ [^\s]+ [^\s]+ [^\s]+ [^\s]+$")
_SUB_HOUR = re.compile(r"^\*/(\d+) \* \* \* \*$")
_HOUR_OR_MORE = re.compile(r"^0 \*/(\d+) \* \* \*$")


@given(_cadence)
@settings(max_examples=75)
def test_seconds_to_cron_always_returns_five_fields(cadence_sec: int) -> None:
    out = _seconds_to_cron(cadence_sec)
    assert _FIVE_FIELD.fullmatch(out) is not None, out


@given(_cadence)
@settings(max_examples=50)
def test_seconds_to_cron_is_deterministic(cadence_sec: int) -> None:
    assert _seconds_to_cron(cadence_sec) == _seconds_to_cron(cadence_sec)


@given(st.integers(min_value=1, max_value=60))
@settings(max_examples=60)
def test_seconds_to_cron_at_or_below_60s_emits_every_minute(cadence_sec: int) -> None:
    """The framework's smallest cron resolution is 1 minute, so any
    cadence ≤60s is rounded to ``every minute``."""
    assert _seconds_to_cron(cadence_sec) == "* * * * *"


@given(st.integers(min_value=61, max_value=3599))
@settings(max_examples=75)
def test_seconds_to_cron_sub_hour_uses_minute_step(cadence_sec: int) -> None:
    """For 60 < cadence < 3600, the schedule is ``*/N * * * *`` where
    N is the minute step, 1..59. Pinning so a future refactor that
    accidentally switches to ``0 */N * * *`` for sub-hour cadences
    fails immediately. Boundary is ``cadence_sec=3600`` exactly →
    ``minutes=60`` → falls into the hour branch (covered by the next
    test); hypothesis caught this off-by-one in an earlier draft of
    this file."""
    out = _seconds_to_cron(cadence_sec)
    m = _SUB_HOUR.fullmatch(out)
    assert m is not None, out
    minute_step = int(m.group(1))
    assert 1 <= minute_step < 60, (cadence_sec, out)


@given(st.integers(min_value=3600, max_value=86400))
@settings(max_examples=75)
def test_seconds_to_cron_hour_or_more_uses_hour_step(cadence_sec: int) -> None:
    """For cadence ≥ 3600s, the schedule is ``0 */N * * *`` (run at
    the top of every Nth hour), where N is at least 1."""
    out = _seconds_to_cron(cadence_sec)
    m = _HOUR_OR_MORE.fullmatch(out)
    assert m is not None, out
    hour_step = int(m.group(1))
    assert hour_step >= 1, (cadence_sec, out)
