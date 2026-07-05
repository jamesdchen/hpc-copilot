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
