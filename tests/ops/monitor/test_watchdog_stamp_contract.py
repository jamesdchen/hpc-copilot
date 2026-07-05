"""Pins the single driver-watchdog tick-stamp definition (``state.journal.stamp_watchdog_tick``).

Finding 12 (proving run #5): the canary poll loop stamped NO liveness while the
monitor poll loop stamped the §5 watchdog every poll — two loops disagreeing on
"what a tick means" (the #351-#4 class) false-flagged a discovery canary as a
stalled driver and left the sidecar frozen at its submit stamp. The stamp now
has ONE home; this test proves BOTH poll loops route through it, so a future
change to the tick semantics cannot land in one loop and miss the other.

Shape mirrors ``tests/state/test_code_drift.py::test_layers_share_one_drift_predicate``.
"""

from __future__ import annotations

import inspect


def test_both_poll_loops_share_one_watchdog_stamp_definition() -> None:
    from hpc_agent.ops import monitor_flow, verify_canary
    from hpc_agent.state import journal

    # THE one definition exists in the state layer.
    assert callable(journal.stamp_watchdog_tick)

    # The monitor poll loop's _stamp_watchdog is a THIN re-point, not a second
    # body: it routes through the shared helper and does NOT re-inline the
    # next_tick_due deadline computation the shared helper owns.
    monitor_src = inspect.getsource(monitor_flow._stamp_watchdog)
    assert "stamp_watchdog_tick" in monitor_src, (
        "monitor poll loop must route through the shared tick-stamp helper"
    )
    assert "next_tick_due=" not in monitor_src, (
        "monitor poll loop must not re-inline the stamp body (it belongs to the shared helper)"
    )

    # The canary poll loop stamps through the SAME shared helper (the call lives
    # inside the decorated verify_canary primitive, so assert against the module
    # source rather than the wrapped callable).
    canary_src = inspect.getsource(verify_canary)
    assert "stamp_watchdog_tick" in canary_src, (
        "canary poll loop must route through the shared tick-stamp helper"
    )
