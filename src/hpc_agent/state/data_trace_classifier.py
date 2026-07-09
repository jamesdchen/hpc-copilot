"""The digest classifier — NO KNOB, the classifier decides (data-trace T3).

Digests (full-frame content hashes) are the trace's one expensive atom, and
they have exactly one consumer class: IDENTITY questions (reproduction
verification, canary-vs-local, fingerprint admission). Whether a run IS one of
those is recorded BEFORE it starts — the canary flag, the sidecar's
``reproduces`` field, local-gauntlet context, ``task_count`` — so code sets the
digest flag and the human never sees a decision point (``docs/design/
data-trace.md`` §"Digest policy: NO KNOB — the classifier decides").

This module is the PURE mapping (sidecar/context → on|off) plus the degradation
helper the verify/render side reads. It is submit-side POLICY: deliberately
NOT in the T2 emission contract (``execution/mapreduce/data_trace_contract``),
which is the cluster-import-safe, stdlib-only NAMES module the dispatcher
imports — the doc pins "the digest CLASSIFIER lives elsewhere (T1/T3)". It
homes here, beside the T1 record model (``state/data_trace``), because both
are the local, package-side substrate; the classifier stays stdlib-only and
pure so it is trivially testable and the caller (``build_submit_spec``) maps
its decision onto :data:`~hpc_agent.execution.mapreduce.data_trace_contract.
TRACE_DIGEST_ENV_VAR`.

FAILURE POSTURE (what makes knob-removal safe): on-when-unneeded = bounded
seconds wasted; off-when-needed = verification DEGRADES to whole-run
comparison and DISCLOSES "stage digests unrecorded" (:func:`digest_availability`)
— the status quo plus honesty, never a block, never a fabricated match. The
spec-level override (``force_on``/``force_off``) is an OVERRIDE, never a prompt,
and its exercise is disclosed on the sidecar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent.state.data_trace import atom_schema

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "SMALL_ARRAY_DIGEST_THRESHOLD",
    "DIGEST_OVERRIDE_VALUES",
    "DigestOverride",
    "DigestContext",
    "DigestDecision",
    "classify_digests",
    "DigestAvailability",
    "digest_availability",
]

#: The atom name the classifier gates (the closed-set membership is asserted at
#: import via :func:`atom_schema`, so a renamed atom fails loudly here rather
#: than silently un-gating every run).
_DIGEST_ATOM = atom_schema("digest").name

#: task_count threshold: an array of at most this many tasks digests ON.
#:
#: PINNED to 4 (drift-log 2026-07-08, T3): the doc says "task_count <= threshold
#: → digests on" without a number, so this is pinned here. Rationale anchored on
#: an EXISTING house number rather than invented: ``SubmitFlowSpec.
#: canary_skip_threshold`` defaults to 4 — an array of <=4 tasks AUTO-SKIPS the
#: canary (#263: the main run's own tasks execute as fast as a canary would), so
#: it loses the canary-vs-local identity check that larger arrays get for free.
#: Digesting exactly those small arrays restores per-stage identity coverage
#: where the canary won't, and is cheap precisely because the array is small.
#: Changing this CLASS is a reviewed edit (the doc's "human-owned frozen code");
#: instances never ask.
SMALL_ARRAY_DIGEST_THRESHOLD = 4

DigestOverride = Literal["force_on", "force_off"]

#: The closed set of override tokens (pinned by test so a third value can never
#: be added without a reviewed edit here). ``None`` = no override (the classifier
#: decides); the two members are the ONLY caller levers.
DIGEST_OVERRIDE_VALUES: tuple[DigestOverride, ...] = ("force_on", "force_off")


@dataclass(frozen=True)
class DigestContext:
    """The recorded-before-it-starts context the classifier reads.

    Every field is a fact known at submit time from the sidecar / submit
    context — NEVER a human decision:

    * ``is_canary`` — this is a canary run (canary-vs-local is an identity
      question; the doc's "canary flag").
    * ``reproduces`` — the sidecar's ``reproduces`` field is set (a deliberate
      reproduction; verify-reproduction compares stage digests).
    * ``is_local`` — local-gauntlet context ("did my cheap-kill see what I
      think?"; the local runner ingests immediately and may diff).
    * ``task_count`` — the array size; ``<= SMALL_ARRAY_DIGEST_THRESHOLD``
      digests on (see the threshold rationale).
    * ``override`` — the spec-level ``trace_digests`` lever. WINS over every
      classifier signal; ``None`` = the classifier decides.
    """

    is_canary: bool = False
    reproduces: bool = False
    is_local: bool = False
    task_count: int = 0
    override: DigestOverride | None = None


@dataclass(frozen=True)
class DigestDecision:
    """The classifier's verdict.

    * ``digests_on`` — the resolved on/off the caller maps onto the env var.
    * ``triggers`` — the context signals that fired (empty when off by
      default); rendered for disclosure, never interpreted.
    * ``override`` — the override token exercised, or ``None``.
    * ``override_exercised`` — the override was provided (``override is not
      None``); the sidecar records ``trace_digests_override`` when true.
    * ``reason`` — a one-line human-readable summary for briefs / disclosure.
    """

    digests_on: bool
    triggers: tuple[str, ...] = field(default_factory=tuple)
    override: DigestOverride | None = None
    override_exercised: bool = False
    reason: str = ""


def classify_digests(ctx: DigestContext) -> DigestDecision:
    """Map a :class:`DigestContext` to a :class:`DigestDecision`. PURE.

    ON when ANY identity signal holds: ``is_canary`` | ``reproduces`` |
    ``is_local`` | ``task_count <= SMALL_ARRAY_DIGEST_THRESHOLD``. The
    spec-level ``override`` (``force_on``/``force_off``) WINS over the signals
    and its exercise is recorded (``override_exercised``) so the caller can
    disclose it on the sidecar. Nothing here prompts; nothing adapts.
    """
    triggers: list[str] = []
    if ctx.is_canary:
        triggers.append("canary")
    if ctx.reproduces:
        triggers.append("reproduces")
    if ctx.is_local:
        triggers.append("local")
    if ctx.task_count <= SMALL_ARRAY_DIGEST_THRESHOLD:
        triggers.append(f"task_count<={SMALL_ARRAY_DIGEST_THRESHOLD}")

    natural_on = bool(triggers)
    override = ctx.override
    if override == "force_on":
        return DigestDecision(
            digests_on=True,
            triggers=tuple(triggers),
            override=override,
            override_exercised=True,
            reason=(
                "digests ON — override force_on (would otherwise be "
                f"{'on' if natural_on else 'off'}: "
                f"{', '.join(triggers) if triggers else 'no identity signal'})"
            ),
        )
    if override == "force_off":
        return DigestDecision(
            digests_on=False,
            triggers=tuple(triggers),
            override=override,
            override_exercised=True,
            reason=(
                "digests OFF — override force_off (would otherwise be "
                f"{'on' if natural_on else 'off'}: "
                f"{', '.join(triggers) if triggers else 'no identity signal'})"
            ),
        )

    return DigestDecision(
        digests_on=natural_on,
        triggers=tuple(triggers),
        override=None,
        override_exercised=False,
        reason=(
            f"digests {'ON' if natural_on else 'OFF'} — "
            + (", ".join(triggers) if triggers else "no identity signal (counts/sketches only)")
        ),
    )


# --- the degradation path (off-when-needed → DISCLOSED, never fabricated) -----


@dataclass(frozen=True)
class DigestAvailability:
    """Whether a trace's records carry stage digests — the degradation flag.

    A digest-wanting consumer (verify-reproduction, canary-vs-local, fingerprint
    admission) reads this BEFORE comparing. When ``present`` is false it DEGRADES
    to whole-run comparison and surfaces :meth:`disclosure` — "stage digests
    unrecorded" — never a block, never a fabricated per-stage match (the doc's
    degradation sentence; the pointing doctrine applied to a missing atom).
    """

    present: bool
    stages_total: int
    stages_with_digest: int

    def disclosure(self) -> str | None:
        """A one-line disclosure string when digests are absent/partial, else None.

        ``None`` when every stage carries a digest (nothing to disclose). A
        string otherwise — the exact text a brief/verdict surfaces so the human
        reads "compared without stage digests", never a silent same-looking
        result.
        """
        if self.present:
            return None
        if self.stages_total == 0:
            return "stage digests unrecorded: the trace has no stages to compare"
        if self.stages_with_digest == 0:
            return (
                "stage digests unrecorded: this run's context classified digests OFF "
                f"({self.stages_total} stages, none digested) — verification degrades "
                "to whole-run comparison"
            )
        return (
            "stage digests PARTIAL: "
            f"{self.stages_with_digest}/{self.stages_total} stages digested — "
            "per-stage comparison is only possible where both sides carry a digest"
        )


def digest_availability(records: Sequence[dict[str, Any]]) -> DigestAvailability:
    """Count how many of *records* carry a ``digest`` atom. PURE.

    ``present`` is true only when EVERY record digests (a partial trace cannot
    answer a per-stage identity question end-to-end). Reads the T1 record shape
    (``record["atoms"]["digest"]``) tolerantly — a malformed record simply does
    not count toward ``stages_with_digest``.
    """
    total = 0
    with_digest = 0
    for rec in records:
        total += 1
        atoms = rec.get("atoms") if isinstance(rec, dict) else None
        if isinstance(atoms, dict) and _DIGEST_ATOM in atoms:
            with_digest += 1
    return DigestAvailability(
        present=total > 0 and with_digest == total,
        stages_total=total,
        stages_with_digest=with_digest,
    )
