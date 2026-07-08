# Multi-human — the trust substrate for a research group

**Status: PLANNED (2026-07-07), not yet implemented.** The durable hand-off
for the multi-actor substrate (the `docs/design/notebook-audit.md` pattern):
settled decisions with recorded rationale, file-disjoint Opus task waves,
enforcement rows, boundary-drift flags. Cite `path::symbol`, never line
numbers. Record implementation drift in a drift log at the foot of this
document. Siblings written concurrently: `docs/design/live-conformance.md`
and `docs/design/challenge-attestation.md` — referenced, never edited here.

## Product intent — team amplification

Every mechanism in the tree today assumes ONE human. The utterance log is
identity-less ("text a human typed" — WHICH human is unrecorded because there
is only one); `state/attestation.py::ATTESTORS` is `{"human", "code"}` with
no identity; "the human signed" is unambiguous by census. The moment a second
researcher shares the repo, three things the substrate cannot currently say
become load-bearing:

1. **Reviewer ≠ author.** A sign-off by the person whose session drafted the
   section is self-review wearing a review's clothes. Nothing today can even
   *state* the distinction, let alone gate on it.
2. **Delegated authority.** "Actor X may greenlight canary-scale; a
   registration requires actor Y" — the lab's real division of labor — has no
   declarative home; it lives in unenforceable social convention.
3. **Attribution in the archive.** The dossier can prove *that* a human
   signed at a sha; it cannot say *who*. For a group, an unattributed
   attestation is a diluted one.

**The settled design center (user-approved 2026-07-07): begin at the trust
anchor, not at auth.** No accounts, no passwords, no PKI, no identity system
in core. Identity today is IMPLICIT; multi-human makes it EXPLICIT — an
opaque `actor` slug stamped by the harness and compared by gates — without
core ever VERIFYING who anyone is. The honest trust claim throughout is
**HARNESS-ASSERTED attribution**: the same honesty tier the whole utterance
log already lives at (`docs/internals/harness-contract.md`, "The honest trust
limit"). Attributed ≠ verified, stated everywhere.

**Scope: a single shared machine FIRST** — one lab workstation, two
researchers, one repo, one journal home. Federation across machines is a
SEPARABLE later problem (short reserved section at the end; nothing designed
here depends on its answers).

## The four moves, in order

The feature is four small extensions to existing objects, each additive,
each byte-identical to today when no actors are declared:

- **(a) Attributed capture** — the harness stamps an opaque `actor` id on
  captured utterances. A one-line extension to capability 1 of
  `docs/internals/harness-contract.md` ("attributed utterance log"): the
  harness knows whose session it is; core records the assertion.
- **(b) `attestor_id` on attestation records** — an opaque caller-authored
  slug, additive to `state/attestation.py`'s record shape. Old records lack
  it → single-actor semantics, byte-compatible.
- **(c) Identity COMPARISONS in gates** — pure `!=` / set-membership over
  opaque ids, Q1-clean (IDENTITY over opaque caller content,
  `docs/internals/engineering-principles.md`). First instance: reviewer ≠
  author on notebook sign-offs. Second (reserved to this doc by the
  challenge sibling): resolver ≠ challenger.
- **(d) Policy as pack data** — who may sign / greenlight / register what,
  as declarative caller-side lists and mappings core compares, never
  evaluates (the `docs/design/domain-packs.md` pattern applied to people).

## Architecture decisions (settled)

### MH1 — the actor-id model: an `actors` block on interview.json

The declaration seam is the `_wire/actions/interview.py::InterviewSpec`
opt-in precedent (`audited_source`, D7): a sibling optional field

```json
"actors": {"ids": ["alice", "bob"],
           "policy": {"registration": ["alice"],
                      "campaign-greenlight": ["alice", "bob"]}}
```

- `ids` — the declared actor slugs. Slug rules: the shared filesystem-safe
  tag class (`state/scopes.py::validate_tag` — the slug becomes a PATH
  SEGMENT in MH2, so this is load-bearing, not stylistic). Opaque to core:
  never a role vocabulary — core has no idea what a "PI", "postdoc", or
  "reviewer" is, and no field may ever carry those words
  (the caller-vocabulary rule; enforcement row below).
- `policy` — optional; see MH6. Absent → no policy gating.
- Persisted verbatim in interview.json (`ops/memory/interview.py`),
  `exclude_none` so an absent block keeps interview.json byte-identical
  (the v1.6 `_AuditedSource` precedent).
- **Default-single-actor semantics (the D7 posture, exactly):** no `actors`
  block, or `ids` with fewer than two entries → every identity comparison
  and policy consultation in every gate returns silently, byte-identical to
  today. Zero declared actors is not an error, not a warning, not a log
  line — it is today's system. Attribution CAPTURE (MH2) may still occur
  when a session is actor-configured (stamping is harmless and additive);
  only COMPARISONS require the >1 declaration.
- A policy entry or any record naming an actor NOT in `ids` is a LOUD
  `errors.SpecInvalid` at validation time (the dangling-reference posture,
  `docs/design/domain-packs.md` "The bind event" — an opted-in reference
  core cannot resolve must never silently pass).

**How a session knows its actor: harness configuration.** The reference
binding is an environment variable, `HPC_ACTOR`, set per-session by the
harness/shell profile (each researcher's login on the shared workstation
exports their own slug). The capture hooks
(`_kernel/hooks/utterance_capture.py`, `answer_capture.py`) and the gate-side
session-actor resolver (MH4) read it. This is deliberately NOT a CLI flag or
a spec field: an agent-suppliable field would let the model choose its actor
— the actor must arrive from OUTSIDE the model's tool surface, exactly like
the utterance text itself. **The trust limit, extended verbatim:** a human
exporting someone else's `HPC_ACTOR` is a harness-config-level attack, out
of scope exactly as disabling the capture hook is today
(`docs/internals/harness-contract.md`, "The honest trust limit" — extend
that paragraph, don't fork it).

### MH2 — attributed capture: PER-ACTOR LOG FILES, never a fourth record field

The second-hardest call, settled honestly. The utterance-log record schema
is FROZEN — `docs/internals/harness-contract.md` §2: "Exactly three fields
… No other fields; the reader tolerates unknown keys but the writer MUST NOT
add them" — and the planned conformance kit
(`docs/design/conformance-kit.md` D-K3) asserts that byte-rule against every
conforming harness. Three candidate mechanics, weighed:

- **(rejected) an `actor` field on the record.** Breaks the frozen write
  API for every conforming writer at once; under the kit's deprecation
  posture a previously-conforming harness failing the schema assertion is
  the definition of a breaking change → harness-contract v2 for a feature
  most installs (single-actor) never use. Disproportionate.
- **(rejected) a `schema_version`-gated record extension** (the
  `canon_version` escape-hatch style). Honest but heavy: every reader,
  every kit fixture, and both reference adapters grow version branches, and
  the single-actor path is no longer byte-identical (new writers emit a
  version field). The escape hatch exists for when there is no additive
  alternative; here there is one.
- **(CHOSEN) per-actor log files — attribution rides the STORAGE LOCATOR,
  not the record.** An actor-configured session appends to
  `<journal home>/<repo_hash>/utterances.<actor>.jsonl`; an unconfigured
  session appends to `utterances.jsonl` exactly as today. Each file carries
  the SAME frozen 3-field records — §2 is untouched byte-for-byte, every
  existing conforming writer stays conforming, and the single-actor world
  is byte-identical by construction (no actor configured → no suffixed file
  ever exists). The locator bullet of §2 gains one additive sentence; the
  record-schema bullet gains none.

Mechanics in `state/utterances.py`:

- `utterances_path(experiment_dir, actor=None)` — `actor=None` → the
  existing unsuffixed path; an actor slug (validated by the shared tag
  class — it is a path segment) → the suffixed path. Same non-creating
  no-scaffold rule: files may be created, the NAMESPACE DIRECTORY never is.
- `append_utterance(experiment_dir, text, actor=None)` — the hooks pass the
  session's `HPC_ACTOR` when set. Same frozen record, same provenance
  filter (`is_harness_injected`), same fail-open. An INVALID actor slug
  fails open to the unsuffixed log (a broken config degrades to today's
  tier, never wedges capture).
- `read_utterances(experiment_dir, actor=None)` — `actor=None` → the UNION
  of the unsuffixed log and every `utterances.<actor>.jsonl`, merged
  oldest-first by `ts` (so every existing identity-less consumer sees all
  human text, as today); `actor=<slug>` → that actor's file ONLY. The
  unsuffixed log is deliberately EXCLUDED from an actor-scoped read:
  anonymous text must never satisfy an actor-specific evidence check, or
  cross-actor laundering re-opens (agent under actor A quoting text typed
  in an unattributed session).
- A concurrency bonus this choice buys for free: two researchers' sessions
  appending simultaneously on the shared machine write DISJOINT files — no
  interleaving hazard on the append path.

**The harness-contract extension (one capability, one added line).**
Capability 1 becomes the "attributed utterance log": a conforming harness
MAY write through the actor-suffixed locator when it knows whose session it
is; the record schema, provenance contract, no-scaffold rule, and fail-open
semantics are unchanged. Core never verifies the attribution — the claim is
harness-asserted, the tier is named. The LLM-never-writes lock is unchanged
and covers the suffixed files identically (no verb writes ANY utterance
file). The second harness (`notebook-ingest-signoffs`,
human-invoked-only) gains an optional actor parameter under the same
documented-human-invoked-only contract as its `write_utterance_log` flag.

**Two consequences named honestly (pre-implementation verification
2026-07-07):**

1. **Capability-1 DETECTION must learn the suffixed locator.**
   `ops/harness_capabilities.py` probes log presence via
   `state/utterances.py::utterances_path(...).exists()` — under an
   actor-only capture regime the unsuffixed file never exists and the verb
   would report capability 1 absent while attributed logs sit beside it.
   The presence probe becomes "unsuffixed OR any `utterances.<actor>.jsonl`"
   (non-creating glob) — task MT4b. Tiers key off detection (the contract's
   "detected == behaved" rule), so missing this breaks the negotiation
   surface, not just a report.
2. **A v1-conforming (unattributed) harness DEGRADES under >1 declared
   actors.** Its writes land in the unsuffixed log, which MH4's
   actor-scoped evidence pool deliberately excludes — so a harness that
   fully honors §2 v1 no longer earns the full-strength tier in a
   declared-multi-actor experiment; the gate falls to the friction tier.
   This is disclosed, not accidental (anonymous text satisfying an
   actor-specific check is the laundering channel), but it is a
   capability-1 promise becoming actor-conditional and MT8's contract
   extension MUST state it in the degrades-when-absent form the contract
   uses for every tier.

### MH3 — `attestor_id`: the additive identity field on the ONE kernel

`state/attestation.py::Attestation` gains `attestor_id: str | None = None`,
validated like `view_sha` (when present, non-empty string; opaque — core
compares, never interprets). `attestor` stays the closed `{"human","code"}`
literal — WHAT kind of attestor; `attestor_id` is WHICH one. Weighed
against a parallel store or a wrapper record: the field is the minimal
additive change; `validate`/`bind`/`reduce` are untouched in behavior (an
absent `attestor_id` validates exactly as today, so every existing record —
greenlight, sign-off, receipt, look, bind — remains valid byte-for-byte:
**old records lack it → single-actor semantics**). `reduce` never keys on
it (drift-revocation is identity-of-subject, not identity-of-attestor);
gates that want identity comparisons read the field per-instance, the same
thin-gates-call-the-kernel posture as today. Code attestations may carry it
too (which actor's session emitted a receipt) — same opaque echo, no lock
change.

### MH4 — how gates resolve and verify an actor at authorship-check time

Two distinct questions, kept separate on purpose:

1. **Attribution — whose act is this?** The gate resolves the SESSION ACTOR
   server-side: a small helper in `ops/decision/journal.py`
   (`_session_actor()` — reads `HPC_ACTOR`, validates the slug against the
   declared `actors.ids`, returns `None` when unset or when no `actors`
   block exists). The resolved actor is stamped as `attestor_id` on the
   record's attestation projection. It is NEVER a caller-suppliable spec
   field — the append-decision wire model gains no actor field (the same
   reasoning as MH1: the model must not choose its identity; enforcement
   row below). Under >1 declared actors, a gated human block
   (sign-off, greenlight, unlock, registration when it lands) with NO
   resolvable session actor is REFUSED loudly naming the missing
   configuration — an anonymous act in a declared-multi-actor experiment is
   the laundering channel (sign as nobody, be everybody), so the
   dangling-reference posture applies, not D7 silence. Zero/one declared →
   the helper is never consulted; byte-identical.
2. **Evidence — does this actor's own typed text support it?** The
   utterance tier becomes actor-scoped: `_harness_human_texts(experiment_dir,
   actor=None)` passes the actor through to
   `state/utterances.py::read_utterances`. Under >1 declared actors, every
   authorship gate (`_assert_human_authorship`, `_assert_unlock_authorship`,
   `_assert_signoff_authorship`) draws its evidence pool from the SESSION
   ACTOR'S log only — the response tokens must derive from text THIS actor
   typed. This closes cross-actor laundering: actor A's agent cannot commit
   a value only actor B ever typed. When the actor's log is absent/empty
   the gate falls back to the journal-response friction tier exactly as
   today (`_harness_human_texts` returning `None`) — the tier ladder gains
   an actor dimension but no new rungs, and each tier keeps its honest
   name.

### MH5 — the drafter-attribution seam: a journaled DRAFT attestation

The hardest judgment call in this doc. Reviewer ≠ author needs the section's
AUTHOR, and the author of an LLM-drafted section is settled honestly as:
**the actor whose session drove the drafting — the LLM is transport, the
session-owner is the author.** (The drafting prompt, the nudges, the
acceptance of the draft all came from that human's session; the model has no
standing of its own, exactly as it has none in the utterance log.)

Where is that recorded? Three candidate seams, weighed:

- **(rejected) the audit config** (`audited_source` gaining a per-section
  author map): static data about a moving target — sections are redrafted
  across sessions, and interview.json is not an append-only trail; an
  author map there would be overwritten state, not evidence.
- **(rejected) the sign-off record itself** (the signer asserts who drafted
  it): the reviewer's agent authoring the author claim at review time is
  self-satisfying — a guard the LLM itself satisfies is not a guard.
  Authorship must be recorded at DRAFT time by the drafting session, not
  reconstructed at review time by the reviewing one.
- **(CHOSEN) a draft attestation** — a new lightweight mutate verb,
  `notebook-draft` (`ops/notebook/draft_op.py`, the
  `notebook-record-receipt` template): given `{audit_id, section}`, it
  recomputes `section_sha` from the `.py` ON DISK server-side (the parse IS
  the recompute — never caller-asserted), resolves the session actor
  server-side (MH4's helper — no wire field), and appends
  `block="notebook-draft"`, `response="drafted"` (an honest mechanical
  string, the `record_auto_clear` naming discipline),
  `resolved={audit_id, section, section_sha, actor}` to the notebook
  journal, projected to a CODE attestation
  (`attestor="code"`, `attestor_id=<actor>`,
  `subject_kind="notebook-draft"`, `subject_id=<section>`,
  `content_sha=<section_sha>`) routed through `state/attestation.py::bind`.
  The audit skill (`skills/hpc-notebook-audit`) records a draft after each
  (re)draft lands, as part of its existing prelude choreography.

Properties by construction: a redraft moves the sha → the old draft record
reads STALE via the ONE reducer → authorship follows the CURRENT content,
with no state machine (the D8 property). The record is fabrication-resistant
in the dimension that matters: the sha is server-recomputed, and the actor
is harness-asserted from the same out-of-model channel as everything else —
an agent cannot stamp a section onto a different actor without a config
attack that is already out of scope. Stated at its honest tier
(pre-implementation verification 2026-07-07): what the record proves is
"the actor whose SESSION RECORDED the draft at this sha" — the skill
choreography (MT6) is what keeps the recording session the drafting
session; a session declining to record can only forfeit its own actor's
authorship claim (MH6 then refuses the sign-off for a missing
attribution), never mint one for someone else.

### MH6 — the reviewer≠author gate (first identity comparison) + its D7 silence

Extends `ops/decision/journal.py::_assert_signoff_authorship` — after the
existing three locks, under EXACTLY these conditions:

- **Active only when interview.json declares >1 actor** (MH1). Otherwise
  the gate body is byte-identical to today — no draft lookup, no actor
  resolution, no new refusal, zero additional filesystem reads beyond the
  interview.json read the gate already performs
  (`_resolve_signoff_audit_config` reads it today; the actors check rides
  the same read).
- The signer = the session actor (MH4), recorded as `attestor_id` on the
  sign-off record.
- The author = the `actor` of the newest `notebook-draft` attestation for
  this section whose `content_sha` equals the FRESHLY RECOMPUTED
  `section_sha` (the reduction routes through `state/attestation.py::reduce`
  with the draft records — one kernel, never a re-inlined newest-first).
- **The comparison: `signer != author`, pure identity over opaque slugs.**
  Equal → refused, naming both the section and the shared actor ("the
  drafter's actor cannot sign their own section"). Core never knows WHY the
  lab wants this — it compares ids, full stop (Q1-clean).
- **A missing draft attribution at the current sha is REFUSED, not
  skipped**, when >1 actor is declared: an unattributed section would make
  self-review undetectable by omission (draft, skip `notebook-draft`,
  self-sign). The refusal names the remedy (record the draft). This is the
  dangling-reference/loud posture, deliberately NOT D7 silence — D7 silence
  belongs to the un-opted-in world (zero/one actor), where the gate does not
  exist.
- The redundant-sign-off path (auto-cleared + voluntary human sign-off,
  the T8 accept-and-mark ruling) faces the SAME comparison — a redundant
  self-review is still recorded self-review.

### MH7 — the challenge sibling hook: resolver ≠ challenger (owned here)

`docs/design/challenge-attestation.md` (concurrent sibling) defines the
challenge and resolution records and RESERVES the identity comparison to
this doc. The ruling, in the same shape as MH6: the challenge record carries
`attestor_id=<challenger>` (the session actor at challenge time); the
resolution record carries `attestor_id=<resolver>`; when >1 actor is
declared, the resolution gate refuses `resolver == challenger` — you may
not adjudicate your own objection — and refuses an unattributed resolution
(the MH6 loud posture); the WITHDRAWAL gate refuses
`withdrawer != challenger` (a second actor silencing another's standing
dissent is the suppression channel — added at cross-doc verification
2026-07-07, since neither doc covered withdrawal identity). Zero/one actor
declared → silent, byte-identical (a solo researcher legitimately resolves
their own past challenge; the comparison only means something in a group).
Ownership, made unambiguous (cross-doc verification 2026-07-07 — the two
docs previously each pointed at the other and NEITHER task list carried
the check): this plan supplies the actor plumbing (MH1–MH4); the gate code
lands in the challenge plan's verdict/withdraw gate
(`_assert_challenge_verdict_authorship`, its T5) **as a follow-up task
executed by whichever plan lands second**, citing this section. Neither
plan's Wave C is complete without that follow-up once both have landed.

### MH8 — policy as pack data: delegation, declaratively

The `docs/design/domain-packs.md` pattern applied to people. Policy is
**lists and mappings core compares — membership tests, never evaluation**:

- **v1 home: the `actors.policy` mapping on interview.json** (MH1) —
  `{<gated block name>: [<actor slug>, ...]}`. Keys are EXISTING gated
  block names (mechanism nouns core already owns: `"notebook-sign-off"`,
  `"campaign-greenlight"`, `"scope-unlock"`, `"registration"` when the
  sibling kernel lands); values are subsets of `actors.ids` (a dangling
  slug is loud at validation, MH1). The consultation, one helper used by
  every gate (`_assert_actor_policy(block, actor)` beside the authorship
  gates): when >1 actor is declared AND the block has a policy entry, the
  session actor must be a member — otherwise refused naming the block and
  the allowed list. No entry for a block → no restriction (policy is
  opt-in per block). Pure `in` over opaque slugs: IDENTITY + COUNTING,
  nothing core evaluates.
- **The pack seam, to-be-reserved:** when `docs/design/domain-packs.md`
  lands, a pack MAY declare the same mapping shape as a seam
  (`actor_policy` — NOTE, pre-implementation verification 2026-07-07:
  `state/pack.py::SEAM_NAMES` is a CLOSED, equality-pinned set and the
  packs plan does NOT currently reserve this entry; adding it is a
  reviewed vocabulary change to that plan, to be coordinated there —
  shape-only loading like every seam) so a
  group can version its delegation rules with its domain standards and get
  drift-revocation on policy edits for free (a re-bound policy revokes
  nothing retroactively — records keep their `attestor_id`; the gate simply
  consults the CURRENT policy at append time). Reserved exactly as S6 was:
  named in the schema, no consumer built here.
- **Where delegation matters most: the registration kernel.**
  `docs/design/registration-kernel.md` R6 is the maximally-human gate; its
  `_assert_registration_authorship` (sibling plan, T7) inherits the policy
  consultation and the `attestor_id` stamp when both plans have landed —
  "registration requires actor Y" is one policy line, and the registration
  record then archives WHO deployed capital, at the strongest tier in the
  system. This plan adds no registration code; the wiring note lives here
  so neither plan re-derives it.
- **What policy can NEVER be:** a predicate ("actor X may greenlight IF
  n_tasks < 100"), a role vocabulary ("PI may…"), a priority/quorum scheme
  (two-of-three signing is a FUTURE instance — it decomposes into COUNTING
  distinct `attestor_id`s over one subject and can ride the same substrate
  later, but it is not designed here). Lists and mappings only.

## What this deliberately does NOT build

- **No identity verification in core** — no login, no password, no keypair,
  no signature check, no OS-user probe. The actor is a harness-asserted
  opaque slug; the tier is named, never overclaimed.
- **No role vocabulary** — core never learns "PI"/"student"/"reviewer";
  every id is caller-authored and compared by identity only.
- **No auth UI / account management** — declaring actors is editing
  interview.json (via the interview verb), configuring a session is one
  env var.
- **No per-actor secrets, quotas, or cluster credentials** — clusters.yaml
  and SSH identity are machine-level concerns, untouched.
- **Federation deferred** (below).

## Federation (reserved — a separable later problem)

The substrate merges naturally by construction: utterance logs, decision
journals, and attestation records are append-only, immutable, and
content-addressed (full-text shas on utterances; content/view shas on
attestations) — union-merge of two machines' logs is CRDT-like, and
`reduce`'s newest-wins reads survive a merge because records carry their
own `ts`. The rulings deliberately DEFERRED, not designed:

- cross-machine ordering (wall-clock `ts` vs a causal order) and dedup of
  byte-identical records;
- actor identity ACROSS machines (is `alice` here `alice` there? — the
  first place verified identity might genuinely earn its way in, and
  exactly why it is not built now);
- the namespace key itself: `state/run_record.py::repo_hash` hashes the
  RESOLVED LOCAL PATH, so two clones of one repo hash to different
  namespaces on different machines — federation needs a portable repo
  identity before any merge question is even well-posed.

Nothing in MH1–MH8 forecloses any answer here; that is the design's
federation obligation, fully discharged.

## Task waves (file-disjoint, for parallel Opus dispatch)

Every task: fires+passes test pair required (each refusal demonstrates its
fire path on a synthetic violation). The new verb (`notebook-draft`) ⇒ run
ALL SIX regen scripts (`scripts/bake_operations_json.py --write`,
`scripts/build_verb_module_map.py`, `scripts/build_operations_index.py`,
`scripts/build_schemas.py`, `scripts/build_primitive_index.py`,
`scripts/build_primitive_frontmatter.py`) — the 0.8.0 lesson; registry
count +1 (re-check the baseline at implementation: the slate plans move it
to 146 first, evidence-memory to 148, and the concurrent siblings add
live-conformance +2 / challenge +1 in whatever post-slate order lands —
verify against `hpc-agent capabilities`, never a doc's frozen number).
**Sequencing vs the slate:** `ops/decision/journal.py`,
`state/attestation.py`, and `_wire/actions/interview.py` are HOT files the
slate phases also touch (`docs/design/slate-sequencing.md`); this plan
lands as a post-slate phase, its Wave C strictly serialized behind
registration T7 and elicitation E2 on `journal.py` — AND around the
concurrent siblings on the same file (`docs/design/challenge-attestation.md`
T5, `docs/design/live-conformance.md` T7): no mutual post-slate order is
recorded in `docs/design/slate-sequencing.md` yet, so treat cross-plan
`journal.py` edits as strictly serial in whatever order executes.

**Wave A (parallel — disjoint files):**

- **MT1** `state/utterances.py` — the actor parameter on
  `utterances_path` / `append_utterance` / `read_utterances` per MH2
  (suffixed locator, slug validation via the shared tag class, union read
  with ts-merge, actor-scoped read excludes the unsuffixed log, invalid
  slug fails open to unsuffixed). Tests: `tests/state/test_utterances.py`
  — per-actor round-trip, union order, the scoped-read exclusion firing,
  no-scaffold preserved, single-actor byte-identity (no actor → the
  identical file and bytes as before the change).
- **MT2** `state/attestation.py` — `attestor_id` per MH3 (optional field,
  `view_sha`-style validation; `bind`/`reduce` behavior untouched). Tests:
  old-shape records still validate byte-compatibly; a present-but-empty
  `attestor_id` refuses.
- **MT3** `_wire/actions/interview.py` + `ops/memory/interview.py` — the
  `actors` block per MH1 (`ids` slugs, optional `policy` mapping,
  dangling-slug refusal, `exclude_none` byte-identity). Regen (wire
  change). Tests: absent block → interview.json byte-identical.
- **MT4** `_kernel/hooks/utterance_capture.py` +
  `_kernel/hooks/answer_capture.py` — read `HPC_ACTOR` from the hook
  process env, pass it to `append_utterance`; unset/invalid → today's
  path. Tests in the existing hook suites: attributed capture lands in the
  suffixed file; unset env is byte-identical.
- **MT4b** `ops/harness_capabilities.py` — the capability-1 log-presence
  probe (`utterances_path(...).exists()`) extends to the suffixed locator
  (non-creating glob over `utterances.*.jsonl` — MH2 consequence 1).
  Tests: actor-only capture detects capability 1; empty namespace still
  reads absent; no directory created.

**Wave B (after Wave A, parallel — one new file each):**

- **MT5** `ops/notebook/draft_op.py` (new) — the `notebook-draft` mutate
  verb per MH5 (server-recomputed `section_sha`, server-resolved session
  actor, code attestation via `bind`; refuses when >1 actor is declared
  and no session actor resolves; when ZERO actors are declared the verb
  still records with `attestor_id=None` — harmless provenance,
  comparisons stay off). + `_wire/actions/notebook_draft.py` (no actor
  field on the wire — enforcement row). Regen. Tests: fabricated-sha
  refusal, redraft-stales-old-draft via the reducer.
- **MT6** `skills/hpc-notebook-audit/SKILL.md` — the prelude records a
  `notebook-draft` after each accepted (re)draft; skill prose only, no new
  affordance class (the verb is the affordance).

**Wave C (sequential — the hot gate file, after slate phases on it):**

- **MT7** `ops/decision/journal.py` — `_session_actor()` +
  actor-scoped `_harness_human_texts` (MH4), the `attestor_id` stamp on
  gated human records, the reviewer≠author extension of
  `_assert_signoff_authorship` (MH6), and `_assert_actor_policy` wired
  into the existing gated blocks (MH8). Fire tests per refusal:
  self-sign refused; missing draft attribution refused (>1 actor); missing
  session actor refused (>1 actor); cross-actor evidence refused (tokens
  only in the OTHER actor's log); policy non-member refused; and the
  byte-identity pass battery — zero declared actors runs the ENTIRE
  existing gate suite unchanged.
- **MT8** `docs/internals/harness-contract.md` — capability 1 becomes the
  "attributed utterance log" (the one-line capability extension + the
  additive locator sentence in §2 + the trust-limit paragraph extension:
  impersonation via env/filesystem/harness-config out of scope, as today;
  + the DEGRADATION sentence per MH2 consequence 2: under >1 declared
  actors an unattributed conforming writer earns only the friction tier —
  stated in the contract's degrades-when-absent form).
  `tests/contracts/test_harness_contract.py` doc-pins updated in lockstep.
- **MT9** — the conformance-kit RESERVATION (`docs/design/conformance-kit.md`
  is a planned sibling; this task is a note + fixture stub, not kit code):
  the kit's capability-1 module gains, as an ADDITIVE minor when the kit
  lands, an attributed-capture assertion — an adapter driven with an
  actor-configured session lands its record in the actor-suffixed locator
  and the frozen 3-field schema STILL holds per file; an
  unconfigured adapter is byte-identical to the v1 assertions (both
  reference adapters stay green by construction, satisfying the kit's
  additive-minor rule).
- **MT10** `tests/contracts/test_multi_human_boundary.py` (new) — the
  enforcement suite (rows below).
- **MT11** — this doc: status flip + drift log, at the end.

### Enforcement rows (accrue to `docs/internals/engineering-principles.md` maps)

| Rule | Enforced by | Fires when |
|---|---|---|
| **The attribution-honesty pin: attributed ≠ verified.** No core path verifies an actor identity — no credential check, no signature verification, no OS-user probe; every doc/docstring line describing `attestor_id` or the attributed log names the harness-asserted tier | `tests/contracts/test_multi_human_boundary.py` (AST scan over the actor-touching modules for auth-shaped imports/calls: `getpass`/`pwd`/`ssl`/signature verbs) + doc-prose pins in `tests/contracts/test_harness_contract.py` | core grows an identity-verification code path, or prose starts claiming verified identity |
| **The byte-identical-single-actor pin.** Zero declared actors → every gate, every hook, every verb behaves byte-identically to today: no new refusal, no new file, no new record field emitted, no policy read | MT7's pass battery (the full existing gate suite runs green with no `actors` block) + MT1/MT3 byte-identity tests | any comparison, refusal, or write fires without the >1-actor declaration |
| The actor is never caller-suppliable on a gated write: no append-decision / notebook-draft wire field carries an actor; the session actor is resolved server-side only | same suite (wire-schema walk: no `actor`/`attestor_id`-shaped property on the mutate specs; the receipt-sha-pin form from `docs/design/domain-packs.md`) | a spec model grows an actor field the gate then trusts |
| No role vocabulary anywhere: actor slugs are opaque; no core field, constant, or fixture carries a role word (`pi`, `advisor`, `supervisor`, `student`, …) | same suite (the `_FORBIDDEN_FIELD_NAMES` walk pattern extended with the role set + a fixture token scan; toy fixtures use `alice`/`bob`) | a wire model or fixture names a role |
| Identity comparisons route through opaque equality only — `!=` / set membership over slugs; no gate branches on WHICH actor (a named-actor special case in core is a vocabulary) | same suite (AST pin over `ops/decision/journal.py`'s actor helpers: no string-literal actor comparison) | a core branch hard-codes an actor id |
| The utterance write API stays frozen per file: attributed capture adds a locator, never a record field; `append_utterance` emits exactly `{ts, sha256, text}` in every file | existing `tests/contracts/test_harness_contract.py` schema pins, now parameterized over suffixed files (MT8) + the kit reservation (MT9) | a writer adds a fourth field to any utterance file |
| Actor-scoped evidence excludes anonymous text: an actor-scoped `read_utterances` never returns unsuffixed-log records | MT1 fire test | the union leaks into a scoped read (cross-actor laundering re-opens) |
| The LLM still never gains an utterance-writing affordance — including the suffixed files | the existing registry pin (`test_no_utterance_writing_verb_in_registry`), unchanged: `notebook-draft` writes a JOURNAL record, never an utterance file; asserted explicitly | a verb writes any `utterances*.jsonl` |
| Multi-human attestations route through the ONE kernel: draft records, `attestor_id` stamps, and the reviewer≠author reduction never re-inline recompute/newest-first | route-through assertions in MT5/MT7 tests (the accruing-member rule on the existing attestation row) | an actor-bearing path bypasses `state/attestation.py::bind`/`reduce` |

## Boundary-drift flags (the Q1 watch list)

- **No identity verification in core, ever.** The moment PKI, signatures,
  or credential checks look necessary, the feature has left core — that is
  federation's problem or a harness's, and the honest tier ("harness-
  asserted") must keep being said out loud until then.
- **No role vocabulary.** Pressure to add `"reviewer_role"`, a `"pi"`
  default, or an approval-hierarchy concept is the caller-vocabulary line
  crossing; roles are what `policy` MAPPINGS express caller-side without
  core naming them.
- **No auth UI.** An account-management verb, an actor-registration flow,
  or a login prompt is out of scope by construction; declaring actors is
  editing interview.json.
- **Policy stays declarative.** Lists and mappings core membership-tests;
  the first conditional policy ("may greenlight IF …") is a predicate and
  belongs pack-side as a receipt, never in core.
- **The comparisons stay `!=`/`in`.** Quorum (n-of-m), seniority ordering,
  or weighted approvals are future instances that must arrive as COUNTING
  over opaque ids with their own design pass — not as creep here.
- **Federation stays deferred.** A cross-machine merge tool, a portable
  repo identity, or an actor directory each needs its own plan; nothing
  lands ambient.
- **Anonymous never satisfies attributed.** Any convenience path that lets
  unsuffixed-log text count as a specific actor's evidence, or lets an
  unresolvable session actor pass a >1-actor gate, is the laundering
  channel re-opening — the friction is the feature working.

## Related, planned separately

- **`docs/design/challenge-attestation.md`** (concurrent sibling) — defines
  challenge/resolution records; consumes MH1–MH4 and the MH7 ruling.
- **`docs/design/live-conformance.md`** (concurrent sibling) — referenced
  for awareness; no seam shared beyond the harness contract both extend.
- **`docs/design/registration-kernel.md`** — the maximally-human gate;
  inherits `attestor_id` + policy consultation per MH8 once both land.
- **`docs/design/domain-packs.md`** — the reserved `actor_policy` seam.
- **`docs/design/conformance-kit.md`** — the reserved attributed-capture
  assertion (MT9).
- **`docs/design/slate-sequencing.md`** — this plan is post-slate; Wave C
  serializes behind the slate's `journal.py` tasks.

## Implementation drift log

- **Fifth-pass adversarial verification 2026-07-08 (independent Opus sweep;
  no code had landed) — GO.** MH8 verified CONFIRMED: the policy key
  `"campaign-greenlight"` matches the real gated block name
  (`ops/block_drive_op.py`), so `_assert_actor_policy(block, actor)` is a
  guard that can actually fire — not the un-fireable-guard failure the pass
  probed for. reviewer≠author (MH6) and resolver≠challenger (MH7) refuse only
  when >1 actor is declared; at exactly one declared actor there is no refusal
  (solo mode), so the constraint cannot deadlock resolution. Phase 9 lands
  after every other plan's gate additions. Multi-human attestations route
  through the ONE kernel (`bind`/`reduce`), never re-inlining recompute. No
  defect surfaced.

(Populate per deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
