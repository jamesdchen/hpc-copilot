# Onboard-by-reproduction — rung 6 of the onboarding map (Phase 6.5)

**Status: PLANNED, USER-RULED (2026-07-07, rulings 6a/6b/6c).** Companion
to `docs/design/onboarding-map.md` (rung 6). Sequenced as a THIN PHASE 6.5
— after Phase 3 (the fingerprint exists to receive samples) and Phase 6
(evidence memory exists for the optional conclusion composition).

## The arrival

The most common real mid-career arrival: **repo + script + claimed
result** ("my paper says QLIKE 0.1203; here's the code"). The strongest
first interaction the copilot can offer: *"let's see if your result
reproduces under observation."* It converts the skeptic's "I don't need
workflow tooling" into "your result now has a claim-check receipt," and
the claim's evidence history begins at the front door.

## The flow (composition over existing machinery)

1. **Onboard the artifact** — the interview path. EXISTS.
2. **Run fresh under observation, twice** — existing submit machinery;
   the double-canary pattern mints honest n=2 OBSERVED fingerprint
   samples. EXISTS (post-Phase-3 for the sample admission).
3. **Compare fresh vs claim** — `verify-reproduction`'s caller-tolerance
   comparator gains an **external-baseline mode**: the baseline side is
   the human-authored claim instead of a recorded run. NEW (small).
4. **Brief the human** — match or FINDING, with drift dimensions
   surfaced. EXISTS (mismatch-is-a-finding posture).

A skill orchestrates 1–4; the only core additions are the comparator mode
and the receipt kind. No new verbs expected (the mode rides the
verify-reproduction spec); if implementation finds otherwise, that is a
registry-arithmetic drift note here, not a silent addition.

## The claim record (ruling 6a — the LEAN shape)

The claim lives **in the claim-check spec**: human-authored claimed values
+ tolerances, authorship-gated at append-decision like every human spec.
The receipt EMBEDS the claim verbatim. There is deliberately NO required
claim record elsewhere:

- The earlier draft (claim = a `source: external` conclusion attestation)
  was ruled OVERBUILT — and would have violated the evidence-memory
  ruling that conclusions are required NOWHERE at creation. Coherence
  held by subtraction.
- A human MAY later write a conclusion citing the claim-check receipt's
  sha — optional composition through Phase-6 machinery, zero new record
  types, and exactly how the receipt becomes queryable memory.

## The naming lock (ruling 6b — the anti-laundering line)

The comparison result is a distinct receipt kind: **`claim-check`** —
NEVER a reproduction receipt. "Reproduced" requires two OBSERVED runs; an
external claim was never observed, and calling a claim-match
"reproduction" would launder unattested history into the trust chain (the
F1 class, at the front door). The honest sentence the machinery may emit:

> "the claim is consistent with a fresh observed run (within caller
> tolerance)"

The fingerprint history starts from the observed runs ONLY. An
enforcement row pins the vocabulary: no code path writes a
reproduction-kind receipt with an external baseline (fires-and-passes
test at the receipt-write seam).

## Failure semantics

Fresh ≠ claim is a dated FINDING routed to the human — never an
accusation, never blocking. The brief surfaces which identity dimension
moved: code sha? env? **data** (the rung-0 coupling — with a manifest at
claim time the brief can say "the data changed since the claim"; without
one it says "cannot distinguish result decay from data drift — no
manifest", disclosed). The human concludes; core compares.

## What this rung deliberately does not do

- No verdict on the CLAIM's truth — only consistency with an observed run
  under the caller's tolerance.
- No fetch/import of external artifacts — the scientist brings the repo
  (the refusal list: we manifest what arrives, we never fetch).
- No forced memory record — see 6a.
- No standalone entry before Phase 3: building the front door before the
  rooms (fingerprint sample admission) inverts the dependency — the
  reason this is 6.5, not earlier (ruling 6c).

## Sequencing (ruling 6c)

Phase 6.5: after Phase 3 and Phase 6. Contents: the comparator's
external-baseline mode, the `claim-check` receipt kind + its enforcement
row, the orchestrating skill, and the onboarding-map rung-6 status flip.

## Drift log

- 2026-07-07: written (Fable, pre-deadline); rulings 6a (lean claim-in-
  spec shape, conclusion optional), 6b (claim-check naming lock), 6c
  (Phase 6.5) folded.
