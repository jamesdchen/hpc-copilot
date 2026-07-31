# Operational actor — the LLM decides operations, never attestations

**Status: DESIGN ONLY (2026-07-30). No code lands from this document.** The
block-drive wiring is explicitly **PENDING** (§7) because it collides with Wave
P2.c, which is in flight in the same seam; this doc names the seam so the build
is ONE unit later rather than a merge fight now.

**Parent:** [`docs/plans/expost-trust-2026-07-30.md`](../plans/expost-trust-2026-07-30.md)
— the two-lane model and ruling **R-e** ("judgment legs ride the agent-actor
park (P2.a, landed): code sequences, the LLM judges at named non-consent parks,
disclosure journaled; the human holds attestation + spend only"). This document
is R-e's specification.

Cite `path::symbol`, never line numbers. Record implementation drift in a drift
log at the foot of this document.

Siblings written concurrently: `docs/design/s2-readiness.md`,
`docs/plans/prelude-chain-2026-07-30.md`,
`docs/plans/attended-latency-2026-07-30.md` — referenced, never edited here.

---

## 1. The two lanes, stated as a boundary

The parent plan's principle is **gate by irreversibility and attestation, never
by step.** That partitions every decision the chain reaches into exactly two
lanes:

| | **Attestation lane** | **Operational lane** |
|---|---|---|
| Question | "Do you, the human, assert this?" | "Which mechanical path should this run take?" |
| Answerable by | a typed human sentence, only | evidence + judgment |
| What it produces | a claim someone can be held to | a route through machinery |
| Examples | sign-off, scope-unlock, conclusion, registration, challenge resolution, consent grants | transient-or-not, retry-or-failover, which-cluster, which-node, is-this-signature-the-known-one |
| Today | human, typed, gated | **heuristic code that abstains into prose and hands the ambiguity to whoever is watching** |

The whole content of this design is that the **second column has no actor**. The
chain already knows the question is open — `ops/recover/resolve.resolve` returns
`Resolution(decided_by="judgement")` with an `Escalation` and `CandidateAction`s
and then *nothing journals the judgement arm's answer*;
`infra/ssh_circuit.degradation_advice` under a ProxyJump correctly reports that
the classification is AMBIGUOUS and prints a discriminator for a human to run.
Both are honest. Both stop.

An LLM is a competent operational judge and an illegitimate attestor. The
operational actor gives the first column nothing and the second column an actor.

## 2. The settled decisions

### OA1 — The question is composed IN CODE, bounded, with evidence attached

The chain never hands the LLM "the situation". It hands a **question object**
composed by code from state the code already holds:

- the parked position (verb, stage, reason) and the run's identity;
- the closed answer vocabulary for THIS question (never free text — see OA2);
- evidence excerpts, bounded, by path + line range;
- the mechanical candidates the code itself would have chosen among, each with
  the rule that would have selected it.

`ops/recover/diagnosis.compose_diagnosis_request` is this seat, already built
and already correctly shaped: a pure code-composed read of the parked verb/stage/
reason, failure-signature matches routed through the ONE
`infra/failure_signatures.classify`, local read paths, a closed category
vocabulary, and hard bounds (`_EVIDENCE_SCAN_DEPTH`, `_MAX_SIGNATURE_MATCHES`).
The operational actor **generalizes this composer across question kinds**; it
does not write a second one.

The answer shape likewise already exists:
`_wire/actions/attach_diagnosis.AttachDiagnosisSpec{classification,
evidence_excerpts[], proposed_actions[]}`, including the deliberate distinction
between the matcher's `"unknown"` and the agent's `UNMATCHED_CLASSIFICATION`
("an agent's *I looked and it matches nothing* is a claim, not a matcher
output"). That distinction is the model for every operational answer: the LLM's
verdict and the code's verdict must never be storable in the same field.

### OA2 — An operational answer is a CHOICE FROM A CODE-OWNED SET, never prose

Every operational question ships its candidate set from code. The LLM selects a
candidate and states a rationale; the rationale is disclosure, never input. No
code path parses the rationale — the same "the code never reads a nudge string"
invariant `_kernel/lifecycle/block_drive` holds at the human rendezvous, held at
the agent one.

Consequences that make this checkable:
- an answer naming a candidate not in the set is REFUSED (structurally — it is
  not a missing-authorship refusal, see OA6);
- an empty candidate set means the question was not composable, and the chain
  parks for the HUMAN instead of inventing an actor for it;
- a question with exactly one candidate is not a question — code takes it and
  journals it as a code act, unchanged from today.

### OA3 — The act is journaled in its OWN append-only ledger, never the decision journal

**The record type is new and it does not live in `*.decisions.jsonl`.**

The tempting shape is the existing one: `ops/host_retarget._journal_and_patch_failover`
already writes a code-authored, non-consent act into the decision journal via the
state-layer `state/decision_journal.append_decision`, with `block="host-retarget"`,
`provenance={"kind": …, "directed": False}` and the doctrine comment *automatic ≠
silent*. Copying it is **REJECTED here**: that record carries `response="y"`, and
a `"y"` in the decisions file is exactly what `state/decision_journal.is_latest_committed_greenlight`
and the block-gate scan read. Minting operational acts in that shape would grow a
population of code-authored `"y"` records that a boundary scan must then learn to
ignore — the failure mode is a permission the human never granted, discovered by
archaeology.

The correct precedent is the **overnight consumption ledger**
(`ops/overnight.overnight_ledger_path` / `record_consumption` /
`read_consumption_ledger`, `<scope>.overnight.jsonl`), which is a separate file
*precisely* so a code-authored audit event can never flip a greenlight read or
shadow a real one. The operational act takes the same shape:

- a per-scope append-only `<scope>.operational.jsonl`, written through the same
  `infra/io.append_jsonl_line` (advisory flock + fsync, append-only, never RMW);
- fields (proposed): `schema_version`, `ts`, `scope_kind`, `scope_id`,
  `question_kind`, `at_block`, `at_stage`, `candidates[]` (the code-owned set as
  offered), `chosen`, `rationale`, `evidence_digest`, `authored_by` (server-set,
  never caller-supplied — the `state/diagnosis` precedent), `grant_ref`
  (`null` when the act spends nothing), `spend` (`null` or the metered amount);
- **no `response` field at all.** Not `"y"`, not `"n"`. The absence is the
  structural guarantee: there is no value an operational record can hold that a
  greenlight scan could read as consent, because the key a greenlight scan reads
  does not exist in this schema.

### OA4 — An operational act is NEVER consent and NEVER a scientific claim

Two separate refusals, because they fail differently.

**Never consent.** An operational act authorizes nothing. Where an operational
choice would cross a boundary that requires authorization, the authorization
comes from the standing grant (OA5) or the chain parks for a human — the act
records *which route was chosen*, never *that it was permitted*. The
consent-forwarding hook's rule holds unchanged and is the model: `append-decision`
is ALWAYS ask, because authorizing a consent-commit from consent on file is
laundering. An operational actor that could mint consent would be the same
laundering with a longer path.

**Never a scientific claim.** No operational act may enter a result, a
conclusion, a reproduction verdict, a notebook section, or any citable artifact.
The operational lane answers *how the computation was routed*; it can never
answer *what the computation showed*. `ops/recover/diagnosis`'s existing wall —
the diagnosis "NEVER enters the decision-brief provenance journal, never becomes
an answer-menu option … and no gate reads it" — is relaxed by exactly one notch
here (the chain may ACT on the choice) and by no more: the operational ledger is
still not a provenance source, still not an answer-menu option, and still not
readable by any attestation gate.

### OA5 — Caps are INHERITED from the standing grant; nothing new is minted

An operational act that spends is admitted by the same machinery that admits an
overnight auto-advance, with no second envelope:

- `ops/overnight.standing_consent_status` is the caps predicate. Its ordered
  refusal reasons (`no-consent`, `heal-exhausted`, `expired`, `spec-changed`,
  `placement-changed`, `over-budget-cap`, `over-walltime-cap`,
  `over-cluster-budget-cap`, `over-cluster-walltime-cap`) apply verbatim: an
  operational act that would exceed any of them does not happen, and the chain
  parks for the human with the reason named.
- `ops/overnight.consent_heal_classes` / `consent_authorizes_class` is the
  existing "declared class of act this grant authorizes" and is the template for
  **which operational question kinds a grant covers**. A grant that names no
  operational classes authorizes no spending operational act — silence is not
  permission.
- `ops/overnight.consume_boundary_under_consent` remains the ONE
  consult-and-ledger seat; a spending operational act consumes through it and
  appears in the consumption ledger exactly as any other consumed boundary.
- `ops/overnight.is_consumable_boundary` is the admission table and must
  explicitly admit or exclude each operational question kind — a new kind is a
  deliberate one-line edit, never derived from a naming convention (the
  `block_chain.AGENT_PARKS` discipline).

A **non-spending** operational act (classify this signature; is this transient)
needs no grant. It is still journaled and still disclosed — *automatic ≠ silent*
applies to the free acts too, and they are the ones most likely to accumulate
unwatched.

### OA6 — Refusals here are STRUCTURAL, never authorship-marked

`ops/decision/journal/_shared._refuse_missing_authorship` and its
`{"authorship_evidence": "missing"}` marker exist for refusals a freshly typed
human sentence would fix. No operational refusal is of that kind: a candidate
outside the set, an expired grant, an uncomposable question, a missing evidence
path — none is repaired by the human typing. All raise structurally, unmarked.
A marked refusal on the operational lane would tell the human to author a
sentence that authorizes nothing, which is the contract-taught-by-refusal defect
in a new place.

### OA7 — The park actor becomes three-valued

`infra/block_chain.park_actor` today answers `"agent"` or `"human"`, keyed on the
`AGENT_PARKS` frozenset of `(verb, stage)` pairs whose single member is the audit
loop's draft rendezvous. The registry widens from a set to a **map** and the
answer widens to three values:

| Actor | Meaning | What the park composes | How the resume reads the answer |
|---|---|---|---|
| `human` | the boundary needs a typed human | greenlight target, `approve_hint`, standing-consent offer, answer menu | the decision journal, boundary-scoped |
| `agent-draft` | the LLM must AUTHOR something (today's `"agent"`) | a draft ask; **none** of the consent affordances | re-runs the parked block; the draft is EVIDENCE ON DISK |
| `agent-operational` | the LLM must CHOOSE among code-owned candidates | the question object of OA1; **none** of the consent affordances | reads the operational ledger for an act at THIS question |

`agent-draft` is a rename of the existing `"agent"` value and must stay
wire-compatible: the marker's `actor` key is written only for a non-human actor
(`block_drive.park` / `_repark_marker`), so the widening must not change a single
human marker's bytes, and an old `actor="agent"` marker must continue to resolve
as `agent-draft`.

The resume asymmetry in the last column is the load-bearing part.
`block_drive._resume_agent_park` deliberately never touches the decision journal,
because "a greenlight committed at some other boundary must never be able to
satisfy this one". `agent-operational` needs the same isolation for the same
reason and one more: its answer lives in the operational ledger, so its resume
must read THAT and must scope the read to the question — an operational act
recorded for a different question at a different block satisfies nothing.

### OA8 — Disclosure is not optional and outlives the act

Every operational act appears in `ops/overnight.overnight_morning_brief` in its
own section, beside `consumed[]`, carrying question kind, chosen candidate,
rationale, and the grant reference (or the explicit "spent nothing"). The
morning brief's existing property — disclosure deliberately OUTLIVES the consent
that authorized the act — carries over unchanged; an expired grant must never
erase the record of what was done under it. `ops/recover/notify.compose_park_notice`
and the `ops/status_blocks` snapshot fold surface the same acts as
pointers-and-counts, never content (the S13 discipline).

## 3. Invariants the attestation lane keeps — stated, and untouched

None of the following is modified, extended, or read by anything in this design.
They are listed because a design that adds an actor must say, explicitly, which
seals it is not breaking.

| Seal | Home | Why it is untouched |
|---|---|---|
| Attestor vocabulary | `state/attestation.ATTESTORS = {"human", "code"}` | NOT extended. An operational act is not an attestation, so it never reaches `validate` / `bind` / `reduce` and mints no third attestor. |
| Recompute lock | `state/attestation.bind(record, recompute=…)` | No operational act carries a recompute sha; nothing here binds or reduces. |
| Trusted-display / `view_sha` binding | `ops/decision/journal/signoff._assert_signoff_render_current` | Sign-off still requires the render artifact to exist, agree on `view_sha` + `section`, and match a freshly recomputed section sha. |
| reviewer ≠ author | `ops/decision/journal/signoff._assert_signoff_reviewer_not_author` (MH6), `challenge` MH7 | Unchanged. An operational actor is neither reviewer nor author of anything attested. |
| Typed human authorship | `ops/decision/journal/human_authorship._assert_human_authorship` | Unchanged; the operational ledger never carries `REQUIRED_CALLER_FIELDS`. |
| Code-derived fields are unauthorable | `ops/decision/journal/code_derived._assert_no_code_derived_fields` | Unchanged, and note it applies to EVERY append — the operational ledger is a different file precisely so this gate's population is not diluted. |
| Brief provenance (rule 9) | `ops/decision/journal/brief_provenance._assert_brief_provenance` | A greenlight's resolved values must be NAMED in the persisted brief. Operational acts are not provenance sources and cannot supply a named value. |
| `append-decision` is always human-prompted | the consent-forwarding hook (`_kernel/hooks/consent_forward`) hard-codes ALWAYS-ask for it | Unchanged. Nothing in this design calls `append-decision`. |
| Append-only journal | `state/decision_journal`, `infra/io.append_jsonl_line` | The new ledger uses the same append-only writer; no path reads-modifies-writes. |
| Server-resolved identity | `authored_by` set by the state layer, never caller-supplied (`state/diagnosis` precedent) | The operational record's `authored_by` follows the same rule. |
| Determinism doctrine | repo-wide | An operational choice is an input to the run's ROUTE, and is recorded; it is never an input to a computed number. |

## 4. The first three questions to promote

Each already has a mechanical seat that abstains or tie-breaks arbitrarily. The
build promotes these three and no others; the fourth column is what the code
keeps doing when no actor answers.

| Question | Today's seat | The candidate set the code already owns | Fallback when unanswered |
|---|---|---|---|
| **transient-or-not** | `ops/recover/resolve.resolve` returns `decided_by="judgement"` for `segv`, `queue_stall`, `code_bug`, `unknown`; `_DETERMINISTIC` classes are already code-decided and stay so | the `Escalation`'s `CandidateAction`s | park for the human, exactly as today |
| **retry-or-failover** | `infra/ssh_circuit.degradation_advice` states honestly that a ProxyJump path makes node-local-vs-tunnel-drop AMBIGUOUS and offers the alternate-route preamble probe as discriminator (landed 2026-07-30, `1e89053a`) | {run the discriminator, retry in place, failover} — and the discriminator is itself a candidate, which is the point: the operational actor may choose to GATHER rather than decide | the advice string, printed for a human |
| **which-cluster** | `ops/queue/advance._placement` eliminates on hard constraints then picks least-loaded with an **alphabetical tie-break**; `ops/host_retarget.pool_failover` takes the first healthy untried pool member, `directed=False, # mechanism: no human judgment` | the surviving candidates after hard-constraint elimination | the alphabetical / first-healthy pick, unchanged |

Note what the third row concedes: the alphabetical tie-break is not a decision,
it is the absence of one, and it is already being made — silently, today, by
sort order. Promoting it does not add an actor to a human decision; it adds an
actor to a coin flip.

## 5. Enforcement-row candidates

House format (`| Rule | Enforced by | Fires when |`), for
`docs/internals/principles/determinism-boundary.md` (rows 1–4, beside the
existing "An AGENT park never consumes nor mints consent" row) and
`docs/internals/principles/multi-human.md` (row 5). Written as candidates: the
build lands them with the real test names.

| Rule | Enforced by | Fires when |
|---|---|---|
| **An operational act is not a greenlight, structurally.** The operational record schema has NO `response` key and is written to its own `<scope>.operational.jsonl` through `infra/io.append_jsonl_line`, never to `*.decisions.jsonl`. The guarantee is the absent key, not a convention: `state/decision_journal.is_latest_committed_greenlight` and the block-gate boundary scan read a field this schema cannot hold. The rejected alternative is on the record — `ops/host_retarget._journal_and_patch_failover` writes `response="y"` into the decisions file, and copying that shape would grow a population of code-authored `"y"` records every boundary scan must learn to ignore. | `tests/…::test_operational_record_has_no_response_key`, `::test_operational_act_never_lands_in_the_decisions_journal`, `::test_greenlight_scan_is_byte_identical_across_an_operational_act` | an operational act is written through the decision-journal writer, the schema grows a `response`/consent-shaped field, or a greenlight scan's verdict moves when an operational act is appended |
| **An operational act inherits caps; it never mints them.** A SPENDING operational act is admitted only by `ops/overnight.standing_consent_status` (all nine refusal reasons apply verbatim) and consumes through the ONE `consume_boundary_under_consent` seat; `is_consumable_boundary` must name each operational question kind explicitly. A grant that declares no operational classes authorizes no spending operational act — `consent_authorizes_class` silence is refusal, not permission. | `tests/…::test_spending_operational_act_refused_without_a_covering_grant`, `::test_operational_spend_appears_in_the_consumption_ledger`, `::test_ungranted_class_is_refused_not_defaulted` | an operational act spends outside the grant's caps/expiry/`cmd_sha` binding, a second envelope is introduced, or a question kind is admitted by naming convention instead of the table |
| **The LLM chooses from a code-owned set; its prose is never parsed.** The candidate set is composed in code (`ops/recover/diagnosis.compose_diagnosis_request` generalized), an answer naming a non-member is refused STRUCTURALLY (never `_refuse_missing_authorship`-marked), and the `rationale` is disclosure only — no code path reads it. Mirrors the driver's standing "THE CODE NEVER READS A NUDGE STRING". | `tests/…::test_answer_outside_the_candidate_set_is_refused`, `::test_rationale_is_never_read_by_any_code_path`, `::test_operational_refusals_carry_no_authorship_marker` | a candidate is accepted because the rationale mentioned it, the refusal is authorship-marked, or a question is composed with a free-text answer field |
| **An operational act is never a scientific claim.** No operational record may be cited by a result, conclusion, reproduction verdict, notebook section, or any citable artifact; the operational ledger is not a provenance source, not an answer-menu option, and not readable by any attestation gate. | `tests/…::test_operational_ledger_is_not_a_provenance_source`, `::test_no_citable_artifact_reads_the_operational_ledger` | any render, brief, or attestation gate grows a read of the operational ledger |
| **`park_actor` is three-valued and the widening is byte-neutral.** `infra/block_chain.park_actor` answers `human` / `agent-draft` / `agent-operational` from an explicit `(verb, stage)` map — never derived from a stage-name spelling. Every human park's marker bytes are unchanged (the `actor` key is still written only for a non-human actor), a pre-widening `actor="agent"` marker still resolves as `agent-draft`, and neither agent actor composes a greenlight target, `approve_hint`, standing-consent offer, or answer menu. | `tests/…::test_park_actor_map_is_explicit_not_derived`, `::test_human_park_marker_bytes_unchanged_by_the_widening`, `::test_legacy_agent_marker_resolves_as_agent_draft`, `::test_neither_agent_actor_composes_a_consent_affordance` | a human marker's bytes move, a legacy marker demotes to `human` (the park would then wait forever for a `y` nobody will type), an actor is inferred from a stage name, or an agent park composes any consent affordance |

## 6. The fire tests the build must ship

Beyond the enforcement rows' pins, four tests that must be able to FAIL for the
right reason — each written so the reviewer can see the mutation that trips it.

1. **`test_operational_act_cannot_satisfy_a_human_park`** — record a valid
   operational act for a run, then tick the driver at a `human` park for that
   same run. The tick must still return `awaiting_decision`. *Mutation that must
   trip it:* pointing the human-park resume at the operational ledger.
2. **`test_operational_park_resume_ignores_a_committed_greenlight`** — the twin,
   in the other direction and the one that actually matters: park at an
   `agent-operational` boundary, commit a real human `y` at a DIFFERENT boundary,
   tick. The operational park must not advance. *Mutation:* routing the
   operational resume through the ordinary greenlight read.
3. **`test_spending_operational_act_refused_when_the_grant_expired`** — a live
   question, a valid candidate, an EXPIRED standing grant. Refusal names
   `expired` (the `standing_consent_status` reason, verbatim), the act is not
   ledgered, and the chain parks for the human. *Mutation:* checking caps at
   compose time instead of at act time, so a grant that expires between the two
   is not caught.
4. **`test_every_operational_act_reaches_the_morning_brief`** — n acts across
   both spending and non-spending kinds appear in
   `overnight_morning_brief`, including after the authorizing grant has expired
   (disclosure outlives consent). *Mutation:* filtering the brief's operational
   section by live-grant, which would erase exactly the acts most worth reading.

A fifth, if the build widens `AGENT_PARKS` before the wiring lands:
**`test_park_actor_widening_is_byte_neutral`** — assert a human park's marker
JSON is byte-identical before and after the map widening, on a fixture marker.

## 7. PENDING — the block_drive wiring (collides with Wave P2.c)

**Nothing in `_kernel/lifecycle/block_drive.py` or `infra/block_chain.py` is
touched by this document.** Wave P2.c is building the onboard chain in the same
seam (`block_chain.ORDER`'s `audit-handoff` single-member family exists exactly
so P2.c can re-home it into an onboard family, and the audit chain's
`block_index` positions must not move under it). A widening of `AGENT_PARKS` and
a third `park_actor` value landing concurrently would fight P2.c over the same
two files for no gain.

The seam, named so the build is ONE unit later:

| Symbol | Change owed |
|---|---|
| `infra/block_chain.AGENT_PARKS` | frozenset of `(verb, stage)` → map `(verb, stage) → actor`, values `agent-draft` \| `agent-operational`; membership stays a deliberate one-line edit |
| `infra/block_chain.park_actor` | three-valued return; `"agent"` becomes `"agent-draft"` with legacy-value tolerance |
| `_kernel/lifecycle/block_drive._pending_park_actor` | reads the widened value; absent ⇒ `human` unchanged; legacy `"agent"` ⇒ `agent-draft` |
| `_kernel/lifecycle/block_drive._resume_agent_park` | splits: the draft leg re-runs the parked block (unchanged); a new operational leg reads the operational ledger scoped to THIS question, and — like the draft leg — never touches the decision journal |
| `_kernel/lifecycle/block_drive.park` | composes the OA1 question object for `agent-operational`, beside the existing `_attach_draft_ask`; composes no consent affordance for either agent actor |
| `_kernel/lifecycle/block_drive._repark_marker` | carries the widened actor VERBATIM (the existing hazard — a dropped actor silently demotes an agent park to a human one — now has three values to preserve) |
| `ops/overnight.is_consumable_boundary` | admits or excludes each operational question kind explicitly |
| `ops/overnight.overnight_morning_brief` | the operational-acts section (OA8) |

**Ordering:** P2.c lands and merges first. Then this build, as one unit: ledger +
question composer + the seam edits above + the §5 rows + the §6 tests. Splitting
it is worse than delaying it — a half-wired actor is a park with no answerer.

## 8. Open questions for the human (not assumed here)

1. **Which of §4's three questions is in scope for the first build?** The
   which-cluster tie-break is the safest (it replaces a coin flip) and the
   transient-or-not question is the most valuable (it is the one that costs
   attended minutes). Recommend which-cluster first, transient-or-not second,
   retry-or-failover last — it is the one whose wrong answer costs the most.
2. **Does a NON-spending operational act need any grant at all?** This document
   says no (journaled + disclosed is sufficient). The counter-argument is that
   an unbounded stream of free acts is its own surface.
3. **Does the standing grant need a new declared field for operational classes,
   or does `consent_heal_classes` widen?** Widening reuses a proven vocabulary;
   a new field keeps "what may heal" and "what may be judged" separable.

## Implementation drift log

- **2026-07-30 — banked as design only.** No code. Written from
  `docs/plans/expost-trust-2026-07-30.md` ruling R-e and the landed P2.a
  agent-actor park; the block_drive wiring is deferred behind Wave P2.c (§7).
  OA1–OA8 are proposals, not rulings: the parent docket records R-a…R-e as
  awaiting explicit rulings, and this document inherits that state.
