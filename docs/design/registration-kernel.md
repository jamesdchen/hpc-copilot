---
status: plan
---
# The registration kernel — the deployment-boundary attestation

**Status: PLANNED (2026-07-07), not yet implemented.** The durable hand-off
for the registration substrate: settled decisions with recorded rationale,
the prerequisite-chain mechanism, the exact sign-off bar, and the
file-disjoint task waves for parallel Opus dispatch. Cite `path::symbol`,
never line numbers. Record implementation drift in a drift log at the foot
of this document (the `docs/design/notebook-audit.md` convention).

## Product intent — the problem (user-validated 2026-07-07)

**The most consequential promotion in the pipeline has the least rigor.**
The system gates $2 compute jobs behind typed, un-fakeable attestations —
sign-offs that recompute hashes, greenlights that refuse bare acks, receipts
that cannot be asserted into existence — and then the decision that deploys
real capital happens in chat. Four failure classes nothing prevents today:

1. **Unrecorded promotion.** A strategy goes live with no record, no
   authorship bar, no evidence digest. Nothing distinguishes "the human
   registered this after reviewing the dossier" from "an agent said it was
   fine."
2. **No evidence binding.** What runs in production is not hash-linked to
   what was validated. `errors.SourceUnaudited` closes this class at the
   *submit* boundary (`ops/notebook_gate.py::assert_source_audited`); the
   *deployment* boundary has no equivalent — code can drift between
   validation and go-live and nothing reads stale.
3. **No prerequisite integrity.** A strategy can go live with a stale audit
   or an unverified reproduction. The attestations exist individually
   (sign-offs, reproduction receipts, look ledgers), but nothing COMPOSES
   the question "were all prerequisites current at these shas, at the moment
   of promotion?"
4. **No revocation semantics.** Markets are non-stationary; this repo's
   no-kill-ledger decision already settled the posture — evidence is
   TIME-INDEXED, never permanent. "Is this clearance still valid?" needs a
   mechanical `current | stale` answer, and today there is no object to ask
   it of.

The registration kernel is the last-mile gate: **a promotion becomes one
more attestation** — the same object as every trusted record in the system
(`state/attestation.py` module docstring: "every trusted thing in the system
is one of these and nothing else"), at the strongest human tier the system
has, over the strongest subject it seals.

## The boundary this feature sits on

The same IDENTITY / ORDERING / COMPARISON / COUNTING surface every rigor
primitive holds (`docs/internals/engineering-principles.md`, Q1). A
registration is IDENTITY (this sealed dossier, these prerequisite records at
these shas) plus ORDERING (newest registration wins; append-only) plus
COUNTING (every template field filled, every prerequisite slot current) —
over opaque caller content. Core never learns what is being registered, what
a field means, or what "ready to deploy" means in any domain. The registry
INSTANCE — which registrations exist, what they authorize — lives
caller-side (the consuming repo); core ships only the mechanism.

## Architecture decisions (settled — user-co-designed 2026-07-07)

### R1 — a registration IS an attestation instance, riding append-decision

A registration is a decision-journal record projected to the ONE attestation
object (`state/attestation.py::Attestation`): `attestor="human"`,
`subject_kind="dossier"`, `subject_id=<registration_id>` (caller-authored
slug — the fabrication class), `content_sha=<the sealed dossier's
bundle_sha256>`, `view_sha=<the verify-registration brief the human saw>`,
evidence = the prerequisite chain. It is written ONLY via `append-decision`
under a gated block — **no registration verb, no chain, no next_block, no
skill affordance** (the no-unlock-verb doctrine,
`docs/design/rigor-primitives.md`; D5 lock 1). No new store, no migration:
the record rides the existing journal machinery, flock and all.

### R2 — the subject is the SEALED DOSSIER, bound by `bundle_sha256`

**Verified against `ops/export_dossier.py`:** the dossier manifest carries
`bundle_sha256 = manifest_signature(entries)` — the canonical-JSON signature
over the path-sorted `{source, path, sha256, bytes}` entries list ONLY, with
`generated_at` / `tool_version` deliberately excluded from the pre-image so
two exports of unchanged stores fingerprint identically. That is exactly the
property a registration subject needs: **content identity of the evidence,
stable across re-exports, immune to timestamps**. The registration's
`content_sha` is therefore the dossier `bundle_sha256` — NOT the `.zip`
file's raw sha (the archive embeds `manifest.json` with `generated_at`, so
its byte sha moves on every re-export of identical evidence; rejected).

**The recompute lock at append time re-gathers from the LIVE stores**, never
from the archive: registering routes through
`state/attestation.py::bind` with `recompute` wired to a dry re-gather of
the run's stores (T3's `compute_dossier_signature` seam — the gather logic
`ops/export_dossier.py::_gather_run` already builds the entries list; the
seam runs it without writing a zip). Consequence by construction: **"you may
not register what has drifted since it was validated."** If any sealed store
— the sidecar, the audit journal, the aggregated numbers — moved after the
dossier was exported, the recomputed signature differs and the append is
refused with the recorded-vs-recomputed pair. A sealed artifact cannot be
registered while its ground truth has moved out from under it.

### R3 — the PREREQUISITE-CHAIN RULE (the genuinely new mechanism)

A registration NAMES its required prior attestations, and the append gate
checks that **each one reads CURRENT at append time via the existing kernel
reducers** — one definition, route-through, never a re-inlined newest-first
or sha-compare (the enforcement-map "one kernel" row,
`docs/internals/engineering-principles.md`).

**The naming shape (settled — full addresses, never bare slugs).** Each
chain entry is:

```json
{"slot": "<caller-authored slug>",
 "kind": "<one of PREREQUISITE_KINDS>",
 "subject_id": "<opaque — audit_id / run_id / scope tag / pack slot>",
 "content_sha": "<the sha the prerequisite was current at>",
 "requires": {"…": "…"}}
```

A bare slug was rejected: a slug cannot be mechanically checked for
currency. The full address lets the gate dispatch each entry to the ONE
existing checker for its kind and compare the asserted `content_sha` against
the checker's recomputed answer — the prerequisite chain is a list of
recompute locks, not a list of claims.

**`PREREQUISITE_KINDS` is a CLOSED set of core MECHANISM nouns**
(`state/registration.py::PREREQUISITE_KINDS`, equality-pinned — the
`DOSSIER_SOURCES` pattern; adding a kind is a reviewed vocabulary change).
These are store/mechanism nouns like the dossier's, never domain words:

| Kind | Route-through (the ONE existing definition) | CURRENT means |
|---|---|---|
| `notebook-audit` | `state/notebook_audit.py::audit_module` (+ the linked-source drift check the gate layer owns, `ops/notebook_gate.py::_linked_source_drift`) | every required section `signed_current`/`auto_cleared` AND the recomputed `module_sha` equals the entry's `content_sha` — "audit passed at sha X", composable |
| `reproduction` | the newest receipt in `_aggregated/<subject_id>/reproduction_receipts.jsonl`, where **`subject_id` is the REPRODUCTION run's id** — the receipts ledger lives under the repro run, never the original (`ops/verify_reproduction.py::_receipt_path`; `docs/design/reproduction-receipt.md` — experiment-local, append-only) | verdict recorded, no code drift since (`state/code_drift.py::detect_code_drift` over the receipt's recorded identity vs the current tree), the receipt LINKS INTO the registration's dossier (its `original` identity block's `run_id` appears in the dossier manifest's `runs` projection — else refused: an unrelated run's receipt cannot fill the slot), and the `requires` evidence-tier floor met (below) |
| `scope-budget` | `state/scopes.py::count_prior_looks` + `state/scopes.py::is_scope_locked` | the named scope's look count `<=` the caller-declared budget number in `requires` (COUNTING vs a caller number — core compares, never picks) and the scope is not locked |
| `pack-receipt` | `state/pack_receipts.py` slot reduction (domain-packs S6 — **lands only after the pack plan ships**; this kind is reserved exactly as S6 reserved the seam) | the named slot's receipt reduces CURRENT and `passed=true` |
| `attestation` | `state/attestation.py::reduce` over a named journal `{scope_kind, scope_id}` | the newest attestation for `subject_id` in that journal carries the entry's `content_sha` — the generic escape hatch; accepts NO `requires` (nothing core could interpret). The checker echoes the satisfying record's `{block, attestor}` VERBATIM into the slot's `evidence_note`, so the brief discloses exactly what record filled the slot (an ungated journal append is visible in the evidence, never silent) |

**What each entry's `content_sha` binds (recomputed at append — added
pre-implementation verification 2026-07-07; the table's currency conditions
alone left the recompute leg undefined for two kinds).** The uniform rule: an
entry's `content_sha` is the canonical-JSON sha (per
`docs/internals/harness-contract.md` "The sha canonicalization") of the
checker's recomputed EVIDENCE for that kind — the exact state the registrant
reviewed — except where a kind has a natural content identity:

- `notebook-audit` — the module sha (`state/audit_source.py::sha256_normalized`
  over the audited source `.py`), the natural code identity.
- `reproduction` — the canonical-JSON sha of the NEWEST receipt record,
  recomputed by re-reading the ledger (binds "the exact receipt evidence I
  reviewed"; a later re-verify appends a newer receipt and the leg reads stale
  — re-registration is the remedy, R7).
- `scope-budget` — the canonical-JSON sha of the checker's evidence projection
  `{prior_looks, distinct_lineages, locked}` (a new look moves it — dated
  evidence, deliberately).
- `pack-receipt` / `attestation` — the receipt's / newest attestation's own
  `content_sha` (already content-identified).

At VERIFY time (R7/R8), the per-slot `status` is the kind's CURRENT condition
re-evaluated; a moved evidence sha is REPORTED as the slot's
`recorded_sha`-vs-recomputed pair — R8's detail shape already carries both.

Each kind's checker accrues an `inspect.getsource` route-through assertion
as it lands (the `test_layers_share_one_drift_predicate` precedent). The
chain composer itself (`state/registration.py::check_chain`) is pure
dispatch: it never re-implements any member's currency logic.

### R4 — evidence-tier requirements (tiered registration, declaratively)

A template prerequisite entry MAY declare an evidence floor via `requires`,
and the floor is checked by IDENTITY/COMPARISON/COUNTING against evidence
the prerequisite machinery already records — never by a predicate core
evaluates. The load-bearing case is `reproduction` + the determinism
fingerprint (the repro-completion plan's tiered-verdict vocabulary,
memory-recorded 2026-07-07): the fingerprint is an ACCUMULATING,
confidence-labeled code attestation whose envelope carries
`{n, scales, clusters}`, and verify verdicts state their evidence ("within
envelope (n=2, canary-scale)"). A template may therefore demand, e.g.:

```json
{"slot": "repro-check", "kind": "reproduction", "subject_id": "<run_id>",
 "content_sha": "…", "requires": {"min_n": 3, "scales": ["main"]}}
```

The `requires` keys are the fingerprint's exact demand vocabulary
(`{min_n, min_n_full?, scales, clusters}`, plural — one vocabulary across
both docs, coherence review 2026-07-07). Core compares `n >= min_n`
(counting — where `n` counts n_full + n_partial samples both, exactly as the
fingerprint doc separates them in evidence) and requires every named scale
label present in the recorded scales set (identity over labels the repro
machinery itself recorded — core never learns what a scale means). A demand
MAY additionally specify `min_n_full` to require scale-quality — full
(non-partial) samples — separately: `n_full >= min_n_full` is the same
counting comparison over the quality-labeled leg the fingerprint's evidence
block already isolates. This is the
registration-side half of the fingerprint's anti-gaming story: a thin
envelope produces `needs_verdict` items rather than wrong auto-verdicts, and
**registration is the seat that can demand main-scale evidence before
"reproducible" counts**.

**The address chain, pinned (pre-implementation verification 2026-07-07 —
the seam that computes the comparison was previously unstated).** T4's
`reproduction` checker, given the chain entry's `subject_id` (the repro
run's id), resolves the floor mechanically: newest receipt in
`_aggregated/<subject_id>/reproduction_receipts.jsonl` → the receipt's
identity blocks carry `cmd_sha` verbatim
(`ops/verify_reproduction.py::_IDENTITY_FIELDS`) → the fingerprint ledger at
`_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl` → samples loaded via the
fingerprint store (`state/fingerprint_store.py`, that plan's T3), filtered
to CURRENT-identity **ADMITTED** samples (the fingerprint doc's D-consume
admission rule — unadmitted samples never satisfy a demand) → one call to
`state/determinism.py::evidence_meets(samples, requires)`. The checker never
re-implements the envelope or the counting; a missing ledger is an ordinary
shortfall (`n=0`), refused with the demand named. Unknown `requires` keys for a kind are a LOUD
`errors.SpecInvalid` (an opted-in requirement core cannot check must never
silently pass — the dangling-reference posture, domain-packs "The bind
event").

**Partial registration is REFUSED.** A registration with any pending, stale,
or failing prerequisite does not append — the gate names every failing slot
and its status. Recorded rationale: a "partial registration" flag is failure
class 3 (live with a stale audit) re-admitted with a euphemism. The remedy
for partial readiness is not registering; the attention queue (R8) makes the
pending prerequisites visible instead.

### R5 — the TEMPLATE: caller-authored field slots, core ships none

The registration template is a caller-referenced file (relpath + raw-bytes
sha — the `_wire/actions/interview.py::_AuditedSource` / domain-packs DP1
bind-as-data posture; it is not percent-format Python, so
`normalize_source` does NOT apply — one file, one canonical form, decided by
which recompute consumes it, per `docs/internals/harness-contract.md`
"The sha canonicalization"). Shape, validated STRUCTURE-only by
`state/registration.py` (a list of slugs is a list of slugs):

```json
{"fields": ["<field slug>", "…"],
 "prerequisites": [{"slot": "…", "kind": "…", "requires": {"…": "…"}}, "…"]}
```

- **Field slugs are opaque caller data.** The user's three unsigned HUMAN
  slots — registration template fields, stage-3 gauntlet thresholds, RV-data
  scope policy — are PACK DATA / caller data, marked as explicit unsigned
  holes in the pipeline doc. They arrive as template content (directly, or
  via a domain pack's S6 seam — `docs/design/domain-packs.md` reserves
  `registration_fields` / `required_receipts` for exactly this consumer;
  pack receipts enter as `pack-receipt` chain members). The projection is
  mechanical: each S6 `required_receipts` slot slug becomes one chain
  prerequisite entry `{slot: <slug>, kind: "pack-receipt"}`, checked by R3's
  `pack-receipt` route-through. The pack's S6 declaration list stays
  `required_receipts: [<slot slug>]` (the manifest form); the caller's
  opt-in binding that names WHICH pack fills the slot is the `packs` block's
  `receipt_bindings: [{slot, pack}]` (domain-packs, coherence review
  2026-07-07). **Core ships NO
  default template, ever** — the fabrication class. No template resolvable
  in an attempted registration is a loud refusal, never a silent pass.
- **Completeness is COUNTING, the notebook-template-marker pattern:** every
  declared field slug must have a non-empty value in
  `resolved["fields"]` (values opaque, never interpreted); every declared
  prerequisite must appear in the chain with a CURRENT verdict. Missing
  either → refused, naming the slugs.
- **Template drift does NOT retroactively revoke a registration** (settled,
  with recorded rationale — this deliberately diverges from the pack-receipt
  posture): the registration's subject is the DOSSIER, and the template's
  sha is recorded on the record (`template_sha`, raw-bytes). A registration
  made under the standards in force at its timestamp stays a truthful dated
  record; `verify-registration` REPORTS `template: current | stale` as a
  distinct finding so a consumer can require re-registration under new
  standards. Rationale: a pack receipt is machine-re-emittable in seconds,
  so revoke-on-standards-change is cheap and safe there; a registration is
  the maximal human ceremony, and auto-revoking every registration on a
  template typo-fix would train exactly the rubber-stamp fatigue D-attention
  exists to prevent. The finding is never silent — the drift is disclosed,
  the consumer decides.

### R6 — the sign-off bar (the maximally human-required tier, verbatim)

The append gate `ops/decision/journal.py::_assert_registration_authorship`
(the `_assert_signoff_authorship` sibling; same three-lock structure,
every bar raised to its ceiling):

- **Block convention, both directions:** block `"registration"` is refused
  for any `scope_kind` other than `"registration"`; and the registration
  scope accepts only its BLOCK FAMILY — a maintained set starting
  `{"registration", "registration-revoke"}` and growing by reviewed
  addition (`registration-review` and `conformance-verdict` are already
  planned members, `docs/design/live-conformance.md`). *(Pre-implementation
  verification 2026-07-07: the earlier "and vice versa" wording was
  strictly incompatible with R7's own revoke block — the mirror is a
  family set, not a single block.)*
- **Lock 1 — no affordance:** no registration verb / chain / next_block /
  skill; append-decision under this block is the ONLY write path. Pinned by
  the contract test (no primitive named register/registration in the mutate
  registry; `verify-registration` is `verb="query"`).
- **Lock 2 — recompute, un-fakeable:** `resolved` must carry non-empty
  `{registration_id, run_id, dossier_sha, template, template_sha, fields,
  prerequisites}`. The gate recomputes ALL THREE legs server-side and binds
  through `state/attestation.py::bind`: (a) `dossier_sha` vs the dry
  re-gather (R2); (b) `template_sha` vs the template file's raw bytes on
  disk; (c) every chain entry's `content_sha` vs its kind's route-through
  checker (R3), all CURRENT. Any mismatch refuses with the
  recorded-vs-recomputed pair. A hash cannot be asserted into existence —
  at this boundary least of all.
- **Lock 3 — authorship, the raised bar (settled exactly).** Bare acks
  refused (`ops/decision/journal.py::_is_bare_ack`). The response must:
  1. NAME the `registration_id` token-exact (the #26 precedent, the
     slug-naming floor every sign-off already has); and
  2. NAME at least one prerequisite by a **sha prefix — the first 8+ hex
     characters of one chain entry's `content_sha`**, matched against the
     chain the gate just verified.
  The sha-prefix requirement is the diff-token pattern elevated to its
  strongest form (recorded rationale): a diff identifier can coincide with
  generic vocabulary; an 8-hex prefix exists NOWHERE in a human's prior
  vocabulary and can only derive from the presented evidence — the rendered
  `verify-registration` brief. Under harness capability 1
  (`docs/internals/harness-contract.md`) the tokens must derive from the
  out-of-band utterance log (full-strength tier); absent the log, the
  journal-response friction tier applies, honestly named as weaker. There is
  NO auto-cleared tier and NO redundant-mark path for a registration: the
  attestor is ALWAYS human, the bar never waives (the one instance where
  D-attention's answer is "always human-required by construction").
- **`view_sha` required and RECOMPUTED:** binds the code-rendered brief the
  human saw (canonical-JSON sha of the `verify-registration` projection —
  the `relay_render` posture; D5's archive-vs-interface separation). Because
  the registration brief is DETERMINISTICALLY renderable (T5 builds it from
  the reduced status + chain by a pure projection), the gate RECOMPUTES the
  brief sha and binds it as a fourth recompute leg — an upgrade from the
  (now-retired) T8 validated-present ruling, matching v1.6's precedent that a
  deterministically-renderable view is recomputed rather than trusted
  (coherence review 2026-07-07; a witness you can regenerate you should
  regenerate).

### R7 — revocation and supersession: append-only, dated evidence

A registration is **DATED EVIDENCE, never permanent** — the no-kill-ledger
posture. Three ways its status moves, none of them deletion:

- **Drift revocation for free (failure class 4 closed):** the registration
  is registered-at-sha; `verify-registration` recomputes the prerequisite
  chain and the live dossier signature AT READ TIME. Any prerequisite that
  now reads stale, or any sealed store that moved, flips the answer to
  `stale` with named causes. No revocation state machine — the D8 "drift =
  unsigned by construction" property, at the deployment boundary.
- **Supersession:** a NEW registration record under the same
  `registration_id` is simply the newer record; `state/registration.py`'s
  reduction (routing the drift verdict through
  `state/attestation.py::reduce`, adding only winner-selection — the
  `state/notebook_audit.py::_newest_valid` precedent) makes the older one
  historical. Re-registration is the remedy for every staleness.
- **Explicit overturn:** a `"registration-revoke"` record — human, facing
  the same authorship floor (non-bare, names the `registration_id`, and its
  free-text reason is MANDATORY: "validate or overturn WITH reason", the
  consumer-seat prior). The reduction maps a newest-record revoke to status
  `revoked`. A revoke needs no sha recompute (it binds nothing new; it
  withdraws), but it is journaled, attributed, and permanent like everything
  else.

Status vocabulary (`state/registration.py`):
`current | stale | revoked | superseded | absent` — `current` requires the
newest record to be a registration whose chain AND live dossier signature
both still hold; `stale` names every failing leg.

### R8 — consumer seats: verify-registration + the attention queue

- **`verify-registration`** — a read-only `query` verb (the
  SourceUnaudited fires/passes posture, minus the raise): given a
  `registration_id` (or a `run_id` to find registrations naming it), it
  returns the reduced status, `registered_at`, the per-leg detail
  (`dossier: {recorded_sha, recomputed_sha, drifted_stores}`,
  `template: current|stale`, `prerequisites: [{slot, kind, status,
  recorded_sha, evidence_note}]`, `fields: {declared, present, missing}`),
  and the code-rendered markdown brief (whose canonical-JSON sha is the
  `view_sha` a subsequent sign-off must carry). It REPORTS; it never blocks.
- **The deployment refusal lives CALLER-SIDE.** Core does not own the
  deploy boundary, so it ships the mechanical answer and the caller wires
  the ~10-line refusal (`verify-registration` status != `current` → don't
  deploy) into their own path — the registry instance (which registrations
  exist, what they authorize) is the consuming repo's, exactly as the
  pack-receipt trust story keeps check-correctness pack-side. The toy first
  consumer (T10) demonstrates the wiring end to end.
- **The attention queue gains registration edges**
  (`ops/attention_queue.py::_apply_fanout` — the one dispatch from item kind
  to encoded downstream count): new item kinds for a stale registration and
  a registration blocked on pending prerequisites, and — the leverage
  fan-out the queue exists for — an existing `AUDIT_SECTION_UNSIGNED` /
  `AUDIT_SECTION_STALE` item's count grows by the registrations whose chains
  name that audit. **An unsigned prerequisite blocking a registration is
  high-leverage by construction**: it blocks capital, not just a run.

### R9 — the scope-kind decision: a SIXTH kind, `"registration"`

`state/decision_journal.py::SCOPE_KINDS` gains `"registration"` with a path
branch → `.hpc/registrations/<registration_id>.decisions.jsonl`, and
`_wire/actions/decision_journal.py::ScopeKind` gains the literal (schema
regen). Reusing an existing kind was considered and rejected, with reasons:

- **Not `"run"`:** a registration can cover a lineage and outlives any
  single run's journal; its supersession chain spans dossier re-exports.
  Coupling it to one run's journal file would make the reduction filter
  cross-family records forever and would tangle its lifecycle with run
  journal hygiene.
- **Not `"scope"` / `"notebook"`:** the D3/T7 precedent is one path branch
  per attestation FAMILY, so each family's reduction reads its own journal
  with no cross-family noise — the same reason packs got their own fifth
  kind (`docs/design/domain-packs.md` T8). Note the ordering dependency is
  nominal only: packs and registrations take the next two slots in whichever
  order they land; the kinds are independent.
- `registration_id` is a caller-authored filesystem-safe slug (the
  `RunIdStrict` class — it becomes a path segment), never core-invented.

## Agnosticism by FIVE mechanisms (user-confirmed; each an enforcement row)

1. **Opaque-by-construction shapes.** Field slugs, field values,
   `subject_id`s, evidence payloads: identity-compared, counted, echoed —
   never read for meaning. The only vocabularies core owns here are
   MECHANISM nouns: `PREREQUISITE_KINDS` and the status set.
2. **No invented defaults.** No default template, no default field, no
   default prerequisite, no default `registration_id` — the fabrication
   class, enforced by the same no-literal-vocab pins packs use.
3. **Dossier-style vocabulary pins.** The wire models face the
   `tests/contracts/test_dossier_boundary.py::test_wire_models_expose_no_domain_vocabulary`
   pattern: the `_schema_property_names` recursive walk over the
   verify-registration schemas against the `_FORBIDDEN_FIELD_NAMES` set, and
   `PREREQUISITE_KINDS` equality-pinned like `_EXPECTED_SOURCES`.
4. **TOY-DOMAIN test fixtures ONLY.** Fixtures register something
   deliberately dumb (the toy-widgets lineage — a widget-batch dossier, a
   template with `widget-owner` / `jam-threshold` field slugs). **Never
   harxhar / quant vocabulary** — real domain words in fixtures smuggle a
   vocabulary into the tree that greps and future maintainers mistake for
   core knowledge.
5. **Boundary-drift flags written BEFORE implementation** — the section
   below ships in this plan, not after the fact.

## Task waves (file-disjoint, for parallel Opus dispatch)

Every task: fires+passes test pair required (each new refusal demonstrates
its fire path on a synthetic violation). The new verb ⇒ run ALL SIX regen
scripts (`scripts/bake_operations_json.py --write`,
`scripts/build_verb_module_map.py`, `scripts/build_operations_index.py`,
`scripts/build_schemas.py`, `scripts/build_primitive_index.py`,
`scripts/build_primitive_frontmatter.py`) — the 0.8.0 lesson. Registry count
moves +1 (`verify-registration`; the registry is 141 as of e1e9ab27;
cross-slate sum = 146 after packs(+3) / registration(+1) / kit(+1) —
re-check at implementation). The `ScopeKind` literal change also
regenerates schemas. Inventory tails: `docs/generated/operations.md`
regenerates.

**Coordination note:** `ops/decision/journal.py`, `state/decision_journal.py`,
and `ops/attention_queue.py` are HOT files touched by concurrent work —
Wave C is strictly sequential and lands after any in-flight waves on those
files. **Cross-slate order (`docs/design/slate-sequencing.md`, the master):
T3 lands FIRST within Wave A** — the `compute_dossier_signature` refactor
unblocks fingerprint T8 and packs T10, which add store nouns on top — and T7
lands after mcp-elicitation E2 (the authorship-evidence marker in
`ops/decision/journal.py`, which T7's fire tests inherit).

**Wave A (parallel — new or disjoint files):**

- **T1** `state/registration.py` (new) — `PREREQUISITE_KINDS` (closed,
  equality-pinned) + the template loader (shape-only: slugs via the shared
  tag class, structure never meaning) + the chain-entry model + the
  registration reduction (`current|stale|revoked|superseded|absent`,
  routing drift through `state/attestation.py::reduce`; ships its
  `inspect.getsource` route-through assertion) + the record blocks
  (`"registration"`, `"registration-revoke"`) and
  `subject_kind="dossier"` constant. Tests: `tests/state/test_registration.py`
  — crafted journals, every refusal fires (unknown kind, unknown `requires`
  key, empty field slug, revoke-wins, supersession).
- **T2** `_wire/actions/verify_registration.py` (new) — Pydantic wire
  models for the query. Boundary rule: no domain vocabulary in field names
  (the `_FORBIDDEN_FIELD_NAMES` walk, mirrored in T9).
- **T3** `ops/export_dossier.py` — factor the gather into
  `compute_dossier_signature(experiment_dir, run_id, include_lineage)`:
  the existing `_gather_run`/`_gather_scope` pipeline run DRY (entries
  built, sha'd, path-sorted, `manifest_signature` applied — no zip write).
  `export_dossier` routes through it so there is never a second signature
  definition (enforcement row). Pure refactor + the new seam's test
  (dry signature == exported `bundle_sha256`, byte-for-byte).

**Wave B (after Wave A, parallel — one file each):**

- **T4** `ops/registration/prereqs.py` (new) — the per-kind checker
  dispatch (R3 table): each kind routes through its ONE existing definition;
  the composer `check_chain` returns per-slot verdicts and never re-inlines
  any member's logic. The `pack-receipt` kind ships as a loud
  not-yet-available refusal until domain-packs T2 lands (reserved, the S6
  posture — never a silent pass).
- **T5** `ops/registration/verify_op.py` (new) — the `verify-registration`
  read-only query verb: reduction + chain recheck + live dossier recompute
  (via T3) + the code-rendered markdown brief and its canonical-JSON
  `view_sha` (the `ops/relay_render.py` posture; sha per
  `docs/internals/harness-contract.md` "The sha canonicalization").

**Wave C (sequential — hot files, one at a time, after concurrent waves):**

- **T6** `state/decision_journal.py` — the `"registration"` scope kind +
  path branch (`.hpc/registrations/`) +
  `_wire/actions/decision_journal.py::ScopeKind` literal, contract tests in
  lockstep, schema regen (the notebook T7 / pack T8 precedent).
- **T7** `ops/decision/journal.py` — `_assert_registration_authorship`
  (R6's three locks, verbatim) + the revoke floor, wired beside
  `_assert_signoff_authorship`. Fire tests per lock: fabricated
  `dossier_sha`, drifted store, stale prerequisite, missing field slug,
  bare ack, response lacking the sha prefix, redundant/auto paths refused
  (there are none — assert the gate never waives).
- **T8** `ops/attention_queue.py` — the registration item kinds + the
  `_apply_fanout` edges: a registration blocked on a prerequisite counts on
  that prerequisite's item (a non-creating read of the registration
  journals, the fail-open posture of the audit-echo edge in
  `ops/attention_queue.py` — symbol name may differ; verify at
  implementation).

**T9** `tests/contracts/test_registration_boundary.py` (new) — the
enforcement suite (rows below).

**T10 — the FIRST CONSUMER (after Wave C): the TOY registration.** Fixtures
under `tests/fixtures/toy_registration/` + `examples/` — a toy run's
dossier, a template with toy field slugs (`widget-owner`, `jam-threshold`),
a toy prerequisite chain (a notebook audit + a reproduction receipt over the
toy run), a ~10-line caller-side "deploy" script that refuses on any
`verify-registration` status other than `current`, and an integration test
driving: register (gate passes) → verify `current` → edit the audited
source → verify `stale` naming the audit slot → re-sign + re-export +
re-register → verify `current` again → revoke with reason → verify
`revoked`. Toy vocabulary only — never harxhar's.

**T11** — this doc: status flip + drift log, at the end.

### Enforcement rows (accrue to `docs/internals/engineering-principles.md` maps)

| Rule | Enforced by | Fires when |
|---|---|---|
| No registration write affordance: no primitive/chain/next_block/skill mutates a registration; append-decision under the gated block is the only write path; `verify-registration` is `verb="query"` with no side effects | `tests/contracts/test_registration_boundary.py` (registry scan — the no-sign-off-verb pin's form) | a mutate verb named register/registration lands, or the query grows a side effect |
| Registration attestations route through the ONE kernel — bind, reduce, and the revoke/supersession winner-selection never re-inline recompute-and-compare or newest-first drift | `tests/state/test_registration.py` route-through assertions (the accruing-member rule on the existing attestation row) | a registration path bypasses `state/attestation.py::bind`/`reduce` |
| All recompute legs are server-computed: dossier sha via the ONE signature seam (`compute_dossier_signature`), template sha from disk, every chain sha via its kind's route-through, AND the brief `view_sha` via the deterministic `verify-registration` projection (F12 upgrade) — no wire/resolved field is trusted as a sha the gate then records | T7 fire tests (a store edited between export and append is refused; a fabricated chain sha is refused; a fabricated `view_sha` is refused) + the T3 dry-vs-export byte-equality test | the gate starts trusting a caller-asserted sha (the receipt-laundering hole, at the capital boundary) |
| `PREREQUISITE_KINDS` is CLOSED and mechanism-only: equality-pinned; each kind dispatches to a named existing checker; `requires` keys per kind are a closed set; the generic `attestation` kind accepts none | `tests/contracts/test_registration_boundary.py` (the `DOSSIER_SOURCES` equality-pin pattern) | a kind is added ad hoc, a checker re-inlines a member's currency logic, or a `requires` key core cannot check passes silently |
| Core ships NO default template and NO registration vocabulary: no template file in package data; no field-slug/kind default in core source | same suite (package-data scan + no-literal-vocab AST pin over `ops/registration/` + `state/registration.py`) | a template lands under `src/hpc_agent/`, or core defaults a field/slot/id |
| No domain vocabulary on the wire: verify-registration schemas expose no `_FORBIDDEN_FIELD_NAMES` member | same suite (`_schema_property_names` recursive walk, mirrored from the dossier suite) | a wire model grows a meaning-bearing field name |
| The registration attestor is ALWAYS human: no code path appends a `"registration"` block; no auto-clear/redundant/waived tier exists at this gate; a code attestation (pack receipt, render receipt, fingerprint) can satisfy a CHAIN slot but never BE the registration | T7 fire tests + the no-affordance pin (`"registration"` absent from every code-writer block set) | a mechanical writer gains the block, or the gate grows a waiver tier |
| Toy-domain fixtures only: no harxhar/quant vocabulary in registration tests/fixtures/examples | same suite (a token denylist scan over `tests/fixtures/toy_registration/` + the registration test files — the toy-domain fixture rule mechanized) | a real domain word lands in a fixture |

## Boundary-drift flags (the Q1 watch list — written before implementation)

- **Core never interprets a field value.** Fields are counted for presence
  and echoed; the moment a core branch reads a field VALUE for meaning
  ("if jam-threshold > …"), the line is crossed — thresholds are caller
  policy, checked caller-side or pack-side into a receipt.
- **`PREREQUISITE_KINDS` never grows a domain member.** `backtest`,
  `risk-check`, `gauntlet` are pack/caller slot NAMES riding the
  `pack-receipt` or `attestation` kinds — never new core kinds. A new core
  kind must name a core mechanism with an existing one-definition checker.
- **`verify-registration` stays a reporter.** Pressure to make core refuse
  a "deploy" is the feature working — core does not own that boundary; the
  refusal stays a caller-side consumer of the query. Core must never grow a
  deploy/promote/go-live verb.
- **The sign-off bar softens only via richer harness-captured utterances,
  never by waiving the sha-prefix or admitting a bare ack** — the
  D-attention flag, at the tier where it matters most. UX pressure here is
  rubber-stamp fatigue announcing itself.
- **No permanence flag, ever.** No "grandfathered", no "permanent", no
  "pinned-valid" field: a registration is dated evidence; the only remedies
  are re-registration and revocation (append-only, both).
- **Template staleness stays a disclosed finding** (R5's recorded
  divergence): if it ever silently gates or silently passes, either the
  fatigue failure or the laundering failure has been reintroduced.
- **The registry instance stays caller-side.** Core never accumulates a
  cross-repo registration index, never validates what a registration
  authorizes, never learns deployment topology.

## Related, planned separately

- **Domain packs** (`docs/design/domain-packs.md`, PLANNED) — the S6 seam
  (`registration_fields` / `required_receipts`) is reserved for THIS plan;
  pack receipts are `pack-receipt` chain members. This plan's T4 ships that
  kind as a loud not-yet-available refusal until the pack substrate lands.
- **The reproduction receipt / determinism fingerprint**
  (`docs/design/reproduction-receipt.md` + the repro-completion plan) —
  supplies the `reproduction` kind's evidence tiers (`{n, scales,
  clusters}`, the tiered-verdict vocabulary). Registration is the named seat
  that demands main-scale evidence.
- **The attention queue** (`ops/attention_queue.py`, shipped) — gains the
  registration edges in T8; the run-story projection remains its sibling.
- **The harness contract** (`docs/internals/harness-contract.md`) — the
  capability-1 utterance log is what makes the R6 bar full-strength; the
  tier degrades honestly per the contract when absent.

## Implementation drift log

- **Pre-implementation verification 2026-07-07 (adversarial plan review; no
  code had landed):**
  1. *R3 reproduction row — receipt address corrected.* The receipts ledger
     lives under the REPRODUCTION run
     (`ops/verify_reproduction.py::_receipt_path` →
     `_aggregated/<repro_run_id>/…`), so a chain entry whose `subject_id`
     named the registered/original run would address an empty ledger. Pinned:
     `subject_id` = the repro run's id, PLUS a dossier cross-link requirement
     (the receipt's `original.run_id` must appear in the dossier manifest's
     `runs` projection) so an unrelated run's receipt cannot fill the slot.
  2. *R3 — per-kind `content_sha` semantics added.* The table gave currency
     conditions but never defined what sha the checker RECOMPUTES for the
     `reproduction` and `scope-budget` kinds; a literal implementer would
     have had to invent one. Added the uniform canonical-evidence-sha rule
     with the per-kind list.
  3. *R4 — the fingerprint address chain pinned.* Neither doc stated how the
     checker reaches `evidence_meets`'s `samples`: added receipt-identity
     `cmd_sha` → `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl` →
     fingerprint store → CURRENT-identity ADMITTED filter → one
     `evidence_meets` call. Verified against
     `ops/export_dossier.py::_project_run_identity` (`cmd_sha` IS in the
     manifest's `runs` projection) and
     `ops/verify_reproduction.py::_IDENTITY_FIELDS` (`cmd_sha` IS on every
     receipt identity block).
  4. *R3 attestation row — disclosure sentence added.* Arbitrary journal
     blocks face no authorship gate, so a record satisfying the generic
     `attestation` kind can be agent-authored; the checker now echoes
     `{block, attestor}` verbatim into `evidence_note` so the R6 brief
     discloses what filled the slot (disclosure, not a new gate — the
     recorded escape-hatch posture kept).
  5. *Task waves — cross-slate ordering note added* (T3 first; T7 after
     mcp-elicitation E2), matching `docs/design/slate-sequencing.md`.
  6. *Verified accurate against the tree at review time:* registry count 141
     (`operations.json`), `SCOPE_KINDS` currently four members,
     `manifest_signature` imported into `ops/export_dossier.py` from
     `ops/provenance_manifest.py`, `state/scopes.py::count_prior_looks` /
     `is_scope_locked`, the `_assert_signoff_authorship` three-lock sibling,
     `_is_bare_ack`, and the dossier-boundary test pins all exist as cited.

- **T4 implementation 2026-07-08 (`ops/registration/prereqs.py` + tests):**
  1. *`scope-budget` budget key PINNED to `max_looks`.* R3's table said the
     currency condition is "look count `<=` the caller-declared budget number in
     `requires`" but never NAMED the key, so a literal implementer had none. T4
     pins it: `requires: {"max_looks": <int>}`. A `scope-budget` entry with no
     integer `max_looks` is a loud `errors.SpecInvalid` (structurally
     un-checkable input, not a failing-slot verdict) — core compares the look
     count against the caller's number, it never picks a budget. `max_looks` is
     the sole allowed `requires` key for the kind (the closed-key rule; any other
     key is the R4 dangling-reference refusal).
  2. *`attestation` journal address PINNED to `subject_id =
     "<scope_kind>:<scope_id>"`.* R3's `attestation` row routes through
     `state/attestation.py::reduce` over a "named journal `{scope_kind,
     scope_id}`", but the `ChainEntry` carries a single opaque `subject_id`, and
     a grep found NO existing `<scope_kind>:<scope_id>` convention in the tree. T4
     pins the address as a `":"`-partitioned `subject_id` (e.g.
     `"scope:widget-lock"`); a `subject_id` with no `":"` separator is a loud
     `errors.SpecInvalid`. The checker projects each journal record to an
     attestation dict (`resolved.attestor` + `resolved.content_sha`; records
     lacking them are skipped by the kernel's tolerant read), routes the
     current/stale verdict through `attestation.reduce`, and echoes the newest
     valid record's `{block, attestor}` VERBATIM into the slot's `evidence_note`
     (the R3 disclosure sentence).
  3. *Recompute/currency legs, per kind (matching R3's per-kind `content_sha`
     rule).* Every checker's CURRENT verdict requires BOTH the kind's currency
     condition AND `recomputed_sha == entry.content_sha`; a `"stale"` verdict
     always carries the recorded-vs-recomputed pair, `"absent"` means the
     substrate/record does not exist (recomputed sha `None`). `notebook-audit`
     recomputes the module sha (`sha256_normalized` over the interview-echoed
     source `.py`) and routes the section verdict through `audit_module` + the
     gate's `_linked_source_drift`/`_winning_record`; `reproduction` recomputes
     the canonical-JSON sha of the newest receipt and checks code drift via
     `code_drift.detect_code_drift` (the receipt identity carries no `executor`,
     so only the `tasks_py_sha` dimension is live) plus the dossier cross-link;
     `scope-budget` recomputes the canonical-JSON sha of `{prior_looks,
     distinct_lineages, locked}`.
  4. *`reproduction` + `requires` is a loud not-yet-available refusal.* The
     determinism-fingerprint substrate (`state/determinism.py::evidence_meets`)
     does not exist in this worktree, so ANY `requires` floor on a `reproduction`
     entry raises `errors.SpecInvalid` naming `docs/design/determinism-fingerprint.md`
     (reserved-seam posture; never a silent pass). `pack-receipt` is likewise a
     loud not-yet-available refusal until domain-packs lands.
  5. *Canonical-JSON sha helper.* No `infra`/`state` helper of the harness-contract
     form exists to reuse (the `ops/notebook/audit_view` / `ops/story_render`
     copies are private view-sha helpers), so T4 ships ONE local
     `_canonical_sha` (`json.dumps(sort_keys=True, separators=(",", ":"),
     ensure_ascii=False)` → sha256).

- **T6 implementation 2026-07-08 (`state/decision_journal.py` + wire):**
  1. *The `"registration"` scope kind + path branch landed.* `SCOPE_KINDS` gained
     a SIXTH member; `decisions_path` branches to
     `.hpc/registrations/<registration_id>.decisions.jsonl`; the wire `ScopeKind`
     Literal followed in lockstep. T5's `# T6 seam` glob in
     `ops/registration/verify_op.py::_all_registration_ids` was RECONCILED to
     DERIVE the registrations directory from the one `decisions_path` definition
     (`decisions_path(exp, "registration", "_").parent`) — never a second path
     constant. **Regen debt (deferred per the Wave-C dispatch — NOT run):** the
     `ScopeKind` literal + the `verify-registration` verb owe the six regen scripts
     (`operations.json` registry count, indices, frontmatter). The schema-freshness
     contract test (`tests/_wire/test_schema_models_roundtrip.py`) is GREEN as
     landed.

- **T7 implementation 2026-07-08 (`ops/decision/journal.py` + facade):**
  1. *Registration authorship refusals carry the E2 elicitation marker.* The
     Lock-3 refusals (bare ack, un-named `registration_id`, missing prerequisite
     sha-prefix) and the revoke floor's bare-ack / un-named-id refusals route
     through `_refuse_missing_authorship`, attaching
     `failure_features={"authorship_evidence": "missing"}`; the SINGLE
     `append-decision` firing site therefore covers registration sign-offs over
     MCP too (no new surface). Lock-2 sha / structural refusals (dossier bind
     mismatch, template sha drift, view_sha mismatch, partial chain, missing
     field/slot, block-convention, missing reason) stay UNMARKED — a re-elicited
     utterance cannot fix a moved hash (the E2 scoping).
  2. *The `view_sha` fourth leg recomputes with `registered_at=None` /
     `status="current"`.* At append the record has no timestamp yet (the one field
     no caller asserts), so the gate recomputes the deterministic
     verify-registration projection over its append-time legs (all `current`) with
     `registered_at=None`. `verify_op.build_view` was extracted as the ONE renderer
     both the T5 reporter and the T7 gate call, so a witness the gate recomputes is
     byte-identical to the one the reporter renders over the same inputs. NOTE the
     coherence gap for T10 to close: a POST-registration `verify-registration` reads
     `registered_at=<ts>`, so its view_sha differs from the bound one; the human's
     binding witness derives from the pre-append projection, not a post-hoc verify.
  3. *The decision subject reaches the registration subject through a facade.*
     `ops/decision/journal.py` (subject `decision`) cannot import the
     `ops/registration` subject directly (the subject-import lint). A top-level
     facade `ops/registration_view.py` re-exports `check_chain` + `build_view` (the
     `ops/notebook_view.py` precedent); `compute_dossier_signature` is reached via
     the `from hpc_agent.ops import export_dossier` facade form. The pre-existing
     Wave-B cross-subject import reds (`verify_op`/`prereqs` used the direct
     spelling of `ops.export_dossier` / `ops.verify_reproduction`) were fixed to the
     facade form at the same time, restoring the lint contract test to green.
  4. *The dossier cross-link is enforced at append.* The gate passes
     `dossier_run_ids=set(sig.run_ids)` (the live re-gather's resolved run set) into
     `check_chain`, so a `reproduction` slot whose receipt names an unrelated run is
     refused — stronger than the T5 reporter, which passes `None`.

- **T8 implementation 2026-07-08 (`ops/attention_queue.py`):**
  1. *Two registration item kinds.* `registration-blocked` (a registration whose
     winning chain has a NON-CURRENT prerequisite → BLOCKED class, "blocks capital,
     not just a run") and `registration-stale` (a registration whose live dossier
     signature DRIFTED → VERDICT class, a re-registration verdict is owed). A
     `revoked` / `absent` id contributes nothing. The collector routes both
     verdicts through the ONE definitions — `reduce_registration` (dossier drift)
     and `check_chain` (prerequisite currency) — and re-derives nothing (D6).
     Fail-open per registration (a torn journal, a moved run whose dossier cannot
     be re-gathered, or an unparseable chain is skipped, never crashing the read).
  2. *The audit→registration leverage fan-out.* `_fanout_for` for an
     `audit-section-unsigned` / `audit-section-stale` item now adds
     `_count_registrations_naming_audit` to the existing runs-echoing count: an
     unsigned prerequisite blocking a registration blocks CAPITAL, so the audit
     section that gates it earns that leverage. A non-creating, fail-open read of
     the registration journals, winner-selected through `reduce_registration`
     (never a re-inlined newest-first); a revoked/absent registration no longer
     depends on the audit and is not counted.

(Populate further per deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
