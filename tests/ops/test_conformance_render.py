"""Tests for ``ops/conformance_render.py`` (live-conformance T5).

The deterministic ``conformance-status`` brief renderer — pure string work over
the comparator's own fields (the ``ops/relay_render.py`` posture). Pins:

* a GOLDEN render (the dual-labelled, range-phrased brief, byte-exact);
* BYTE-STABILITY (two renders of the same inputs are identical);
* the VOCABULARY token pin (no σ, no urgency / recommendation prose — the
  enforcement row: "the evidence brief stays range-phrased and dual-labelled").

TOY VOCABULARY ONLY (the plan's C6 fixture rule): a fake instrument's
calibration reading. Never trading words.
"""

from __future__ import annotations

from hpc_agent.ops.conformance_render import render_status_brief
from hpc_agent.state.conformance import (
    BaselineRef,
    ConformanceDeclaration,
    ConformanceReport,
    Envelope,
    KeyVerdict,
)


def _decl(**kw: object) -> ConformanceDeclaration:
    base = {
        "baseline": BaselineRef(path="cal.json", sha256="abc123"),
        "keys": ("reading",),
        "min_window_n": 3,
        "review_horizon": None,
    }
    base.update(kw)
    return ConformanceDeclaration(**base)  # type: ignore[arg-type]


def _nonconforming_report() -> ConformanceReport:
    kv = KeyVerdict(
        key="reading",
        tier_reason="outside_envelope",
        within=False,
        baseline=Envelope(lo=0.94, hi=0.97, rel_spread=0.03, n=5),
        window=Envelope(lo=0.95, hi=0.99, rel_spread=0.04, n=4),
        baseline_n=5,
        window_n=4,
        label_sets=(),
    )
    return ConformanceReport(
        tier="nonconforming",
        keys=(kv,),
        window_n=4,
        min_window_n=3,
        as_of="2026-05-05T00:00:00Z",
        keys_from_baseline=False,
    )


def test_golden_nonconforming_brief() -> None:
    """The dual-labelled, range-phrased brief renders byte-exactly (golden)."""
    brief = render_status_brief(
        registration_id="sensor-7-cal",
        report=_nonconforming_report(),
        baseline_n=5,
        sealed_at="2026-03-02T00:00:00Z",
        baseline_note=None,
        window_since="2026-05-01T00:00:00Z",
        window_until="2026-05-04T00:00:00Z",
        window_labels=["site=lab-a"],
        declaration=_decl(),
    )
    expected = (
        "# Conformance sensor-7-cal — NONCONFORMING\n"
        "\n"
        "NONCONFORMING — at least one well-evidenced key's window exits its "
        "well-evidenced envelope. A FINDING; it changes no registration status, "
        "revokes nothing, halts nothing.\n"
        "\n"
        "## Live window\n"
        "- n=4 spanning 2026-05-01T00:00:00Z to 2026-05-04T00:00:00Z (min required: 3)\n"
        "- label sets observed: site=lab-a\n"
        "\n"
        "## Sealed baseline\n"
        "- n=5 sealed 2026-03-02T00:00:00Z (point-in-time; live observations never widen it)\n"
        "\n"
        "## Keys (1)\n"
        "- reading [outside_envelope]: window [0.95, 0.99]; vs registered [0.94, 0.97]; "
        "window n=4; baseline n=5\n"
        "\n"
        "## Declaration\n"
        "- keys: reading\n"
        "- min_window_n: 3\n"
        "- review_horizon: none\n"
    )
    assert brief == expected


def test_byte_stability_two_renders_identical() -> None:
    """The renderer is a pure function: two renders of the same inputs match."""
    kwargs = dict(
        registration_id="sensor-7-cal",
        report=_nonconforming_report(),
        baseline_n=5,
        sealed_at="2026-03-02T00:00:00Z",
        baseline_note="the on-disk baseline artifact 'cal.json' sha does not match the sealed "
        "declaration (declared abc123..., on-disk def456...); the sealed evidence moved - "
        "treat the comparison below as provisional.",
        window_since="2026-05-01T00:00:00Z",
        window_until="2026-05-04T00:00:00Z",
        window_labels=["site=lab-a", "site=lab-b"],
        declaration=_decl(),
    )
    assert render_status_brief(**kwargs) == render_status_brief(**kwargs)  # type: ignore[arg-type]


def test_novel_key_omits_absent_ranges_but_states_ns() -> None:
    """A key-novelty verdict states BOTH ns but omits the absent side's range."""
    kv = KeyVerdict(
        key="reading",
        tier_reason="key_novelty",
        within=None,
        baseline=None,
        window=None,
        baseline_n=0,
        window_n=4,
        label_sets=(),
    )
    report = ConformanceReport(
        tier="needs_verdict",
        keys=(kv,),
        window_n=4,
        min_window_n=3,
        as_of="2026-05-05T00:00:00Z",
        keys_from_baseline=False,
    )
    brief = render_status_brief(
        registration_id="sensor-7-cal",
        report=report,
        baseline_n=0,
        sealed_at=None,
        baseline_note=None,
        window_since=None,
        window_until=None,
        window_labels=[],
        declaration=_decl(),
    )
    assert "- reading [key_novelty]: window n=4; baseline n=0" in brief
    assert "[0.9" not in brief  # no fabricated envelope range for the absent side
    assert "label sets observed: none" in brief


def test_vocabulary_pin_no_sigma_no_urgency() -> None:
    """The brief carries NO σ and NO urgency / recommendation vocabulary (token pin)."""
    briefs = [
        render_status_brief(
            registration_id="sensor-7-cal",
            report=report,
            baseline_n=5,
            sealed_at="2026-03-02T00:00:00Z",
            baseline_note="the sealed evidence moved - treat the comparison below as provisional.",
            window_since="2026-05-01T00:00:00Z",
            window_until="2026-05-04T00:00:00Z",
            window_labels=["site=lab-a"],
            declaration=_decl(),
        )
        for report in (_nonconforming_report(),)
    ]
    forbidden = [
        "σ",
        "sigma",
        "std",
        "p-value",
        "confidence interval",
        "urgent",
        "urgency",
        "recommend",
        "should",
        "you must",
        "halt the",
        "pause the",
        "act now",
    ]
    for brief in briefs:
        low = brief.lower()
        for token in forbidden:
            assert token.lower() not in low, f"forbidden token {token!r} in brief"
