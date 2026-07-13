"""Pydantic models for the ``conformance-status`` query verb (live-conformance T2).

Wire surface over :mod:`hpc_agent.ops.conformance.status_op` — the read-only
comparator report (``docs/design/live-conformance.md`` C-verbs). Given a
registration and a caller-supplied window selection, the verb loads the ledger,
the registration, and the sealed baseline, calls the ONE comparator definition
(``state/conformance.py::judge_window``), and returns per-key verdicts + the
overall tier + a deterministic code-rendered brief. Verdicts are DERIVED on
every read — no verdict store, nothing marked seen (the attention-queue
recompute posture).

**The honest comparison (C-compare), reflected in the wire shape.** Point-in-time
registered evidence (an order-statistics envelope over a sealed, fixed window)
versus a ROLLING live window (different n, different regime, autocorrelated) is
apples-to-oranges. Core does ONLY comparison arithmetic and DISCLOSES both sides'
evidence verbatim — it never fabricates a σ, a p-value, or a confidence interval.
So every ``KeyVerdictLine`` carries BOTH sides range-phrased and n-labelled
(``window_lo/hi`` + ``window_n`` against ``baseline_lo/hi`` + ``baseline_n``), and
the result-level ``window`` / ``baseline`` blocks carry the spans, the sealed
date, and the label sets. A thin window or thin baseline never auto-verdicts:
insufficient / novel / incomparable route to ``needs_verdict`` in BOTH
directions, named by ``tier_reason``.

**No market vocabulary.** ``key``/``labels`` are opaque caller slugs; every field
name here is a mechanism noun (``window``/``baseline``/``envelope``-shaped —
statistical-process-control lineage), never a fill/order/position/pnl-shaped
name (the forbidden-vocabulary walk, mirrored).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hpc_agent._wire._shared import RunIdStrict

# The per-key ``tier_reason`` vocabulary (C-compare, the seven-member fold).
# Exported so the boundary test can equality-pin the closed set. ``within`` /
# ``outside`` are the only two DERIVED verdicts; the other five are all
# evidence-thinness / novelty / incomparability routes to ``needs_verdict``.
ConformanceTierReason = Literal[
    "within_envelope",
    "outside_envelope",
    "insufficient_window",
    "thin_baseline",
    "key_novelty",
    "label_novelty",
    "incomparable",
]

# The overall fold (C-compare "The fold"): any well-evidenced key outside a
# well-evidenced envelope -> nonconforming; else any thin/novel/incomparable
# key -> needs_verdict; else conforming.
ConformanceOverall = Literal["conforming", "needs_verdict", "nonconforming"]


class KeyVerdictLine(BaseModel):
    """One declared key's verdict, DUAL-labelled with both sides' evidence.

    The range-phrased, sigma-free evidence line (C-compare step 2): the window's
    observed order statistics ``[window_lo, window_hi]`` over ``window_n`` obs vs
    the registered envelope ``[baseline_lo, baseline_hi]`` over ``baseline_n``.
    The four range bounds are OPTIONAL: they are absent for an incomparable /
    novel key (a live key the baseline never carried, or vice versa) where one
    side has no order statistics to state. ``window_n`` / ``baseline_n`` are
    ALWAYS present — the counts are the mechanical evidence the classifier routes
    on (window_n < min_window_n, baseline_n < 3), disclosed even when a range is
    not.
    """

    model_config = ConfigDict(extra="forbid", title="conformance per-key verdict line")

    key: str = Field(
        description="The declared metric key (opaque caller slug; core never learns its meaning).",
    )
    tier_reason: ConformanceTierReason = Field(
        description=(
            "Why this key landed where it did: within/outside the well-evidenced "
            "envelope, or a thinness/novelty/incomparability route to needs_verdict. "
            "The closed seven-member vocabulary (C-compare)."
        ),
    )
    window_lo: float | None = Field(
        default=None,
        description="Live window observed minimum (order statistic). Absent when the window has no comparable value.",
    )
    window_hi: float | None = Field(
        default=None,
        description="Live window observed maximum (order statistic). Absent when the window has no comparable value.",
    )
    baseline_lo: float | None = Field(
        default=None,
        description="Registered envelope minimum (sealed order statistic). Absent when the baseline never carried this key.",
    )
    baseline_hi: float | None = Field(
        default=None,
        description="Registered envelope maximum (sealed order statistic). Absent when the baseline never carried this key.",
    )
    window_n: int = Field(
        ge=0,
        description=(
            "Count of observations in the selected live window for this key. Compared "
            "against the caller-declared min_window_n; below it routes "
            "insufficient_window in EITHER direction (never a fabricated verdict)."
        ),
    )
    baseline_n: int = Field(
        ge=0,
        description=(
            "Count of sealed baseline rows carrying this key. Below the reused "
            "well-evidenced bar (n>=3) routes thin_baseline; the one mechanized "
            "evidence threshold, never a new invention."
        ),
    )


class ConformanceWindow(BaseModel):
    """The LIVE side's evidence: the selected window's span and label sets.

    Disclosed verbatim beside every verdict (C-compare step 2) — never corrected
    for autocorrelation or regime shift, only stated. ``since``/``until`` are the
    observed span of the selected window (absent when the selection was purely
    count-based and carried no timestamp bound); ``labels`` is the distinct set of
    label strings observed across the window (novelty is disclosed, not judged).
    """

    model_config = ConfigDict(extra="forbid", title="conformance live-window evidence")

    n: int = Field(ge=0, description="Number of receipts in the selected window.")
    since: str | None = Field(
        default=None,
        description="Earliest observed_at in the window (ISO), when a timestamp bound applies.",
    )
    until: str | None = Field(
        default=None,
        description="Latest observed_at in the window (ISO), when a timestamp bound applies.",
    )
    labels: list[str] = Field(
        default_factory=list,
        description="Distinct opaque label strings observed across the window. Novelty disclosed, never interpreted.",
    )


class ConformanceBaseline(BaseModel):
    """The REGISTERED side's evidence: the sealed baseline's n and seal date.

    Point-in-time and FIXED (C-compare): read from the dossier-bound artifact,
    never grown by live observations. ``sealed_at`` is when the registration
    sealed it — the second half of the dual evidence label ("baseline n=126 sealed
    2026-03-02").
    """

    model_config = ConfigDict(extra="forbid", title="conformance sealed-baseline evidence")

    n: int = Field(ge=0, description="Number of sealed baseline rows (point-in-time; never grows).")
    sealed_at: str | None = Field(
        default=None,
        description="ISO timestamp the registration sealed this baseline (absent when the record carries none).",
    )


class ConformanceStatusSpec(BaseModel):
    """Input spec for ``conformance-status`` — a registration + a window selection.

    The window is an EXPLICIT caller selection over the ledger (C-compare, the
    live side); core never picks, defaults, or recommends a window. Two mutually
    exclusive selection modes:

    * a COUNT selection — ``last_n`` (the trailing N receipts), or
    * a TIMESTAMP selection — ``since`` (required anchor), with optional ``until``
      upper bound.

    The selection rule (pinned by the model validator):

    1. ``last_n`` and ``since``/``until`` are MUTUALLY EXCLUSIVE — a count
       selection and a timestamp selection cannot be mixed.
    2. AT LEAST ONE selection must be present — one of ``since`` / ``last_n``.
       ``until`` alone is refused: it only bounds a ``since``-anchored window and
       is not itself a selection (core never invents the missing anchor).
    """

    model_config = ConfigDict(extra="forbid", title="conformance-status input spec")

    registration_id: RunIdStrict = Field(
        description=(
            "The registration whose live conformance to report. A caller-authored "
            "filesystem-safe slug keying the ledger."
        ),
    )
    since: str | None = Field(
        default=None,
        description=(
            "Timestamp window anchor (ISO): select receipts with observed_at >= since. "
            "Mutually exclusive with last_n; required if last_n is absent."
        ),
    )
    until: str | None = Field(
        default=None,
        description=(
            "Optional timestamp upper bound (ISO): select receipts with observed_at "
            "<= until. Only bounds a since-anchored window; refused without since."
        ),
    )
    last_n: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Count selection: the trailing N receipts. Mutually exclusive with since/until."
        ),
    )

    @model_validator(mode="after")
    def _one_window_selection(self) -> ConformanceStatusSpec:
        timestamp_mode = self.since is not None or self.until is not None
        if self.last_n is not None and timestamp_mode:
            raise ValueError(
                "conformance-status takes EITHER last_n (a count selection) OR "
                "since/until (a timestamp selection), never both: a count and a "
                "timestamp window cannot be mixed."
            )
        if self.last_n is None and self.since is None:
            raise ValueError(
                "conformance-status requires a window selection: supply last_n (a "
                "count) or since (a timestamp anchor). `until` alone is not a "
                "selection — it only bounds a since-anchored window; core never "
                "invents the missing anchor."
            )
        return self


class ConformanceStatusResult(BaseModel):
    """The comparator report — per-key verdicts, the overall tier, dual evidence.

    DERIVED on every read (no verdict store). ``keys`` is one ``KeyVerdictLine``
    per declared key; ``overall`` is the fold. ``window`` / ``baseline`` carry the
    two sides' evidence labels verbatim; ``declaration_echo`` echoes the sealed
    conformance declaration (keys, min_window_n, review_horizon) so a reader sees
    what was judged against without a second call; ``render`` is the deterministic
    code-composed markdown brief (range-phrased, dual-labelled, no urgency or
    recommendation prose).
    """

    model_config = ConfigDict(extra="forbid", title="conformance-status output data")

    registration_id: str = Field(
        description="The registration reported on.",
    )
    overall: ConformanceOverall = Field(
        description=(
            "The fold over the per-key verdicts: nonconforming if any well-evidenced "
            "key exits a well-evidenced envelope; else needs_verdict if any key is "
            "thin/novel/incomparable; else conforming."
        ),
    )
    keys: list[KeyVerdictLine] = Field(
        default_factory=list,
        description="One verdict line per declared key, each dual-labelled with both sides' evidence.",
    )
    window: ConformanceWindow = Field(
        description="The live side's evidence: the selected window's n, span, and observed label sets.",
    )
    baseline: ConformanceBaseline = Field(
        description="The registered side's evidence: the sealed baseline's n and seal date.",
    )
    declaration_echo: dict[str, str | int | list[str] | None] | None = Field(
        default=None,
        description=(
            "Echo of the sealed conformance declaration (keys, min_window_n, "
            "review_horizon) the comparison judged against. Opaque; disclosed so a "
            "reader need not re-fetch the registration."
        ),
    )
    render: str = Field(
        description=(
            "Deterministic code-rendered markdown brief — wording composed from "
            "record fields, range-phrased and dual-labelled, with no urgency or "
            "recommendation vocabulary."
        ),
    )
