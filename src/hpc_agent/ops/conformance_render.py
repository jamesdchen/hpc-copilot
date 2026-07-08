"""``render_status_brief`` — CODE renders the ``conformance-status`` brief from
the comparator's OWN structured fields (live-conformance T5). The agent relays
the returned string VERBATIM.

Design origin: ``docs/design/live-conformance.md`` C-verbs / C-compare — "a
deterministic code-rendered markdown brief (the ``ops/relay_render.py`` posture:
pure string work, wording composed from record fields, no urgency or
recommendation prose)". This is the disclosure surface for the honest
apples-to-oranges comparison (C5): point-in-time SEALED registered evidence (an
order-statistics envelope over a fixed baseline) versus a ROLLING live window
(different n, different regime, autocorrelated). Core does ONLY comparison
arithmetic and DISCLOSES both sides' evidence verbatim.

The two load-bearing token pins (enforcement rows; pinned by the render's
vocabulary test):

* **Range-phrased, never sigma-phrased.** Every line states observed
  ``[min, max]`` order statistics and the ``n`` on each side — never a σ, a
  p-value, or a confidence interval. A fitted parameter is a number core refuses
  to fabricate (C-compare step 2 / the D-envelope posture).
* **No urgency, no recommendation.** The brief STATES the verdict and both
  sides' evidence; it never says "act", "halt", "pause", "recommend", or scores
  leverage. Drift routes ATTENTION, never action — the agency boundary, at the
  render (the ``ops/relay_render.py`` "core interprets nothing" posture).

Pure string work — no SSH, no journal reads, no ``_wire`` import, no I/O: the
caller (``ops/conformance/status_op.py``) hands in the already-computed
:class:`~hpc_agent.state.conformance.ConformanceReport` plus the disclosed
baseline/window evidence, and receives the brief. Deterministic: the same inputs
render byte-identically every time (golden + byte-stability pinned in the tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hpc_agent.state.conformance import (
        ConformanceDeclaration,
        ConformanceReport,
        Envelope,
        KeyVerdict,
    )

__all__ = ["render_status_brief"]

# The overall-tier headline sentences. STATE the verdict and what it means (the
# no-recommendation posture): a needs_verdict routes a human read; a
# nonconforming is a FINDING that changes NO registration status (drift routes,
# never acts — the agency boundary at the render). No urgency vocabulary.
_TIER_HEADLINE: dict[str, str] = {
    "conforming": (
        "CONFORMING — every declared key's live window sits inside a well-evidenced "
        "registered envelope over a sufficient window. A derived read, recomputed now."
    ),
    "needs_verdict": (
        "NEEDS_VERDICT — at least one declared key is thin, novel, or incomparable; "
        "the range-phrased evidence below is for a human verdict, never an auto-verdict "
        "fabricated from evidence disclosed as insufficient."
    ),
    "nonconforming": (
        "NONCONFORMING — at least one well-evidenced key's window exits its "
        "well-evidenced envelope. A FINDING; it changes no registration status, "
        "revokes nothing, halts nothing."
    ),
}


def _num(value: float) -> str:
    """Format an order-statistic bound deterministically (compact, sigma-free)."""
    return f"{value:g}"


def _range(env: Envelope | None) -> str | None:
    """Render an envelope as ``[lo, hi]``, or ``None`` when a side has no statistics."""
    if env is None:
        return None
    return f"[{_num(env.lo)}, {_num(env.hi)}]"


def _key_line(kv: KeyVerdict) -> str:
    """One declared key's line: the tier_reason + BOTH sides range-phrased + ns.

    The dual evidence label (C-compare step 2), verbatim: the window's observed
    range vs the registered envelope, each with its ``n``. A range is stated only
    when that side carried comparable statistics (absent for a novel/incomparable
    key); the ns are ALWAYS stated — the counts are the mechanical evidence the
    classifier routed on. No σ, no fitted parameter.
    """
    window_range = _range(kv.window)
    baseline_range = _range(kv.baseline)
    evidence: list[str] = []
    if window_range is not None:
        evidence.append(f"window {window_range}")
    if baseline_range is not None:
        evidence.append(f"vs registered {baseline_range}")
    evidence.append(f"window n={kv.window_n}")
    evidence.append(f"baseline n={kv.baseline_n}")
    return f"- {kv.key} [{kv.tier_reason}]: {'; '.join(evidence)}"


def render_status_brief(
    *,
    registration_id: str,
    report: ConformanceReport,
    baseline_n: int,
    sealed_at: str | None,
    baseline_note: str | None,
    window_since: str | None,
    window_until: str | None,
    window_labels: Sequence[str],
    declaration: ConformanceDeclaration,
) -> str:
    """Render the ``conformance-status`` markdown brief — a PURE function.

    Composed from the comparator's OWN fields (C-verbs): the overall fold
    headline, the LIVE side's evidence (window n + span + observed label sets),
    the REGISTERED side's evidence (baseline n + seal date), any DISCLOSED
    baseline-integrity note (a drifted/absent on-disk artifact — disclosed, never
    a refusal; the membership gate is the append-time job), the per-key
    dual-labelled range-phrased lines, and the declaration echo. Deterministic
    and byte-stable; no urgency or recommendation vocabulary, no σ.
    """
    lines: list[str] = []
    overall = report.tier
    lines.append(f"# Conformance {registration_id} — {overall.upper()}")
    lines.append("")
    lines.append(_TIER_HEADLINE.get(overall, overall))

    # ── the live side (C-compare step 2: window n + span, disclosed verbatim) ──
    lines.append("")
    lines.append("## Live window")
    span = ""
    if window_since is not None and window_until is not None:
        span = f" spanning {window_since} to {window_until}"
    elif window_since is not None:
        span = f" since {window_since}"
    lines.append(
        f"- n={report.window_n}{span} (min required: {report.min_window_n})"
    )
    if window_labels:
        lines.append(f"- label sets observed: {', '.join(window_labels)}")
    else:
        lines.append("- label sets observed: none")

    # ── the registered side (point-in-time, SEALED, never grows) ──────────────
    lines.append("")
    lines.append("## Sealed baseline")
    sealed_seg = f" sealed {sealed_at}" if sealed_at else ""
    lines.append(f"- n={baseline_n}{sealed_seg} (point-in-time; live observations never widen it)")
    if baseline_note is not None:
        lines.append(f"- integrity: {baseline_note}")

    # ── per-key verdicts (dual-labelled, range-phrased) ───────────────────────
    lines.append("")
    lines.append(f"## Keys ({len(report.keys)})")
    if report.keys_from_baseline:
        lines.append("- (key set disclosed from the baseline — the declaration named none)")
    if not report.keys:
        lines.append("- none judged")
    else:
        lines.extend(_key_line(kv) for kv in report.keys)

    # ── the declaration echo (what was judged against, disclosed) ─────────────
    lines.append("")
    lines.append("## Declaration")
    declared_keys = ", ".join(declaration.keys) if declaration.keys else "(every baseline key)"
    lines.append(f"- keys: {declared_keys}")
    lines.append(f"- min_window_n: {declaration.min_window_n}")
    horizon = declaration.review_horizon if declaration.review_horizon else "none"
    lines.append(f"- review_horizon: {horizon}")

    return "\n".join(lines) + "\n"
