"""Unknown-footprint honesty in the S2 relay line (run #6).

The kernel's defensive 0.0 (walltime unresolved) must never render as
"est. 0 core-hours" -- the brief stamps ``footprint_unknown`` and the
renderer honors it (with a falsy-est fallback for briefs from older
workers that predate the flag).
"""

from __future__ import annotations

from typing import Any

from hpc_agent.ops.relay_render import render_relay


def _brief(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r-deadbeef",
        "cluster": "discovery",
        "est_core_hours": 0.0,
    }
    base.update(overrides)
    return base


def test_footprint_unknown_flag_renders_unknown() -> None:
    line = render_relay("s2", "canary_verified", _brief(footprint_unknown=True))
    assert "unknown core-hours (walltime unresolved)" in line
    assert "0 core-hours" not in line


def test_falsy_estimate_fallback_renders_unknown() -> None:
    # Older workers' briefs carry no flag; the defensive 0.0 still reads unknown.
    line = render_relay("s2", "canary_verified", _brief())
    assert "unknown core-hours" in line


def test_real_estimate_renders_number() -> None:
    line = render_relay(
        "s2", "canary_verified", _brief(est_core_hours=12.5, footprint_unknown=False)
    )
    assert "12.5 core-hours" in line


def test_cache_skip_never_renders_canary_green() -> None:
    """A #249 TTL-cache skip carries its disclosure onto the VERBATIM relay
    line — 'canary green' for a canary that never ran would read as a fresh
    pass (the mandatory-disclosure ruling)."""
    reason = (
        "canary skipped: cmd_sha 82ba92e8 validated 37m ago on carc (HPC_NO_CANARY_SKIP=1 to force)"
    )
    line = render_relay("s2", "canary_verified", _brief(canary_skipped_reason=reason))
    assert "canary green" not in line
    assert "canary skipped: cmd_sha 82ba92e8" in line
    assert "HPC_NO_CANARY_SKIP=1" in line


def test_real_canary_pass_still_renders_green() -> None:
    """No skip reason on the brief → the ordinary 'canary green' line."""
    line = render_relay("s2", "canary_verified", _brief())
    assert "canary green" in line
