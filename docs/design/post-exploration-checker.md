---
status: shipped
origin: docs/plans/expost-trust-2026-07-30.md
audience: public — frames the README body, CONTRACT's checker workflow, the runbook's checker walkthrough
---
# The post-exploration checker — trust relocated to after the fact

**Status: DOCTRINE (2026-08-14).** Promotes the expost-trust plan
([`docs/plans/expost-trust-2026-07-30.md`](../plans/expost-trust-2026-07-30.md)
— the origin document) into design doctrine: hpc-agent repositions as the
**post-exploration fidelity checker** for agent-driven research. The series
has LANDED — the checker chain's verbs and skill ship in the tree (§7).
Where this document and the code disagree, the code wins — the plan is
downstream of the tree.

> **Gate by irreversibility and attestation, never by step.**

## 1. The thesis

Agent-driven research explores fastest when agents hand-roll their scripts and
submit raw — a night of freestyle outpaces the gated pipeline, and the plan's
observation was that the freestyle night was fast **and still trustworthy**.
Trust was RELOCATED, not skipped: no act asked permission; every act was
adversarially verified AFTER; the hard boundaries were exactly attestation
(human sign-off), outward/irreversible acts, and human intent. Reversibility
made everything else free.

Step-gating exploration buys no trust — it buys throughput loss. Ex-post +
evidence-based beats ex-ante + consent-based on throughput at equal (arguably
higher) assurance, WHEN reversibility and verification are real. So the
value-add moves: hpc-agent is not the toll booth every submission passes
through; it is the instrument that **guarantees the fidelity of results AFTER
the fact** — adopts the run, aggregates in code, claim-checks against
human-claimed numbers, and attests. The loops that gate and observe execution
remain, demoted (§4).

## 2. The chain: adopt → aggregate → claim-check → attest

Four links, each a verb (or a composition of verbs), each honoring the
amplification posture — accept with disclosure, refuse nothing, offer the
upgrade path.

**Adopt.** `adopt-run` mints the run record for a
run that already happened, observed by nothing. Adoption facts are ELICITED —
from the human's utterance or from observed scheduler state, never invented; a
field that is unknown stays unknown. The `cmd_sha` is DERIVED from the exact
command the run executed, never free-typed (parameter identity is computed or
it is nothing — `state/run_sha.py` has always minted it that way). The adopted
run enters the trust chain with exactly the evidence it has — unaudited,
unobserved, fingerprint n=0 — stated plainly, never laundered.

**Aggregate.** `aggregate-check` (readiness + integrity gate) then
`aggregate-run` (deterministic combine + reduce) — the aggregate family's
blocks, reused. **The reducer — never the LLM — computes every aggregate
number** (`ops/aggregate_flow.py`); the reduced table stamps its own
`contributing_run_ids`. A harness or agent relays the reducer's output; it
computes, averages, and interpolates nothing. `aggregate-run` keeps its
greenlight gate inside the verb body (`ops/block_gate.py::assert_greenlit_target`
refuses a call the run's journal does not greenlight) — consent binds exactly
where it always did.

**Claim-check.** `verify-reproduction` in external-baseline mode
(`ops/verify_reproduction.py::_run_claim_check`) compares the reduced numbers
against a human-authored claim — `claimed_values` + tolerances, free text the
human wrote and the machinery transcribes verbatim. The consistency
determination is the comparator's (trusted code, caller tolerance as data);
the verdict sentence is a code-rendered module constant —
`CLAIM_CONSISTENT_SENTENCE_ADOPTED` when the run's sidecar carries adopt-run's
`extra.adopted` marker, `CLAIM_CONSISTENT_SENTENCE` for a fresh observed run —
relayed verbatim; the LLM points the human at
the render and never characterizes match or mismatch in its own words. Fresh ≠
claim is a dated FINDING that names the moved dimension, never an accusation,
never a block.

**Attest.** `evidence-brief` projects the run's durable records — sidecar,
journal, receipts, the reduced table's provenance — into the evidence digest
the human attests against; the decision journal records the outcome
(append-only, `append-decision` always human-prompted). Attestation is the
human's act; the machinery's job is to make the evidence cheap to accrue and
impossible to fake.

## 3. What an unobserved run can earn

An adopted run was never observed: no double canary fired for it, no
fingerprint sample accreted, no bind-lock closed over its payloads. So the
strongest verdict the checker may emit over it is:

> "the claim is consistent with the adopted run's records (within caller tolerance)"

— and that sentence is emitted by CODE (the module constant
`CLAIM_CONSISTENT_SENTENCE_ADOPTED`; a fresh observed run earns the
fresh-observed variant instead). **An unobserved run earns "consistent
with", nothing more.** The fingerprint history starts from OBSERVED runs only;
adoption mints no samples, and no code path writes a reproduction-kind receipt
over an external or adopted baseline
(`ops/verify_reproduction.py::_assert_receipt_kind_matches_baseline` raises —
the fires-and-passes seam). Claim-check receipts land in their own ledger
(`claim_check_receipts.jsonl`), so the naming lock holds at the storage layer
too.

## 4. The loops, demoted — an opt-in observation instrument

The submit / status / aggregate / campaign block-drive loops REMAIN — nothing
is amputated. What changes is their station: from the default posture every
submission passes through, to an **opt-in instrument for runs where the caller
wants evidence minted DURING execution**. The loops are the only way to accrue
the observed-tier evidence — the double canary, the bind-locked determinism
fingerprint samples, the per-block journaling, the consent lineage — and runs
that want that tier drive through them exactly as today (the harness runbook
projects the procedure). Runs that already happened, or that exploration
produced raw, enter through the checker instead and carry their honest
unobserved label. Two front doors, one trust chain; the ladder and the trust
model are still the same object (`docs/design/onboarding-map.md`).

## 5. What the checker deliberately does NOT do

Named so the boundary stays real:

- **Never calls itself a reproduction.** A claim-check is a distinct receipt
  kind — "reproduced" requires two OBSERVED runs; an external claim or an
  adopted run was never observed, and calling a match "reproduction" would
  launder unattested history into the trust chain (the naming lock, ruling 6b,
  `docs/design/onboard-by-reproduction.md`).
- **Never fetches what wasn't manifested.** The refusal list holds: we
  manifest what arrives; we never fetch. Adoption takes the record the caller
  brings and the scheduler's observed state; nothing else.
- **Never computes an aggregate number in the LLM.** The reducer computes; the
  LLM points. Every number a model relays is audited (`verify-relay`).
- **Never gates the freestyle path.** Disclose-not-gate: an unadopted run is
  simply unchecked, disclosed as such where asked; the checker makes the next
  rung of evidence cheap to accrue, it does not hold entry hostage. Nothing
  refuses a bare `y`.
- **Never concludes.** Code compares; the human concludes. The checker's
  verdicts are consistency findings and named drift dimensions — stage-level
  judgment stays caller-side.

## 6. The trust floor (invariants that never move)

From the plan, unchanged by the repositioning — these ARE the product;
everything else is throughput: typed attestation authorship + the utterance
log; the append-only journal; `append-decision` always human-prompted (the
consent-forward hook hard-codes it); caps/expiry/spec-binding on all standing
authority; every automated act disclosed in the morning brief; reviewer≠author;
the determinism doctrine.

## 7. Build status, honestly

- **Landed — reused by the checker:** `aggregate-check` /
  `aggregate-run`, `verify-reproduction` external-baseline + the `claim-check`
  receipt kind, `evidence-brief`, the decision journal, `verify-relay`.
- **Landed — this series:** the `adopt-run` verb; the `check`
  catalog tier; the `hpc-check` skill driving the chain end to end; the
  runbook's checker conformance walkthrough (generator-homed prose — the
  checker path is a plain verb chain, not a block-drive workflow, so it has
  no `DECISION_POINTS` / `block_chain` home to project from).

## Drift log

- **2026-08-14 — series landed; verdict sentence corrected.** The adopt-run
  verb, `check` catalog tier, `hpc-check` skill, and runbook walkthrough are
  in the tree — IN FLIGHT flags flipped. §3's verdict now quotes the
  code-rendered constant `CLAIM_CONSISTENT_SENTENCE_ADOPTED` exactly (the
  earlier "manifested record" phrasing was an invented variant no code
  renders).
- **2026-08-14 — created (doctrine).** Promotes the expost-trust plan
  (2026-07-30, banked from the user question: "when claude code is able to
  freestyle, shit gets done within a reasonable amount of time — how can we
  alter our pipeline without giving up trustworthiness?") into design
  doctrine. Rulings R-a..R-e remain the plan's docket; this document promotes
  the PRINCIPLE and the checker chain, not the rulings. Series companions:
  `pyproject.toml` description, README body reorder, the CONTRACT checker
  workflow, the harness runbook walkthrough, and the `adopt-run` / `check`
  catalog / `hpc-check` skill implementations landing in parallel.
