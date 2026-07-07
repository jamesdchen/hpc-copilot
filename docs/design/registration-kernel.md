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
| `reproduction` | the newest receipt in `_aggregated/<run_id>/reproduction_receipts.jsonl` (`docs/design/reproduction-receipt.md` — experiment-local, append-only) | verdict recorded, no code drift since (`state/code_drift.py::detect_code_drift`), and the `requires` evidence-tier floor met (below) |
| `scope-budget` | `state/scopes.py` look-ledger count + lock state | the named scope's look count `<=` the caller-declared budget number in `requires` (COUNTING vs a caller number — core compares, never picks) and the scope is not locked |
| `pack-receipt` | `state/pack_receipts.py` slot reduction (domain-packs S6 — **lands only after the pack plan ships**; this kind is reserved exactly as S6 reserved the seam) | the named slot's receipt reduces CURRENT and `passed=true` |
| `attestation` | `state/attestation.py::reduce` over a named journal `{scope_kind, scope_id}` | the newest attestation for `subject_id` in that journal carries the entry's `content_sha` — the generic escape hatch; accepts NO `requires` (nothing core could interpret) |

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
 "content_sha": "…", "requires": {"min_n": 3, "scale": "main"}}
```

Core compares `n >= min_n` (counting) and requires the named scale label
present in the recorded scales set (identity over labels the repro machinery
itself recorded — core never learns what a scale means). This is the
registration-side half of the fingerprint's anti-gaming story: a thin
envelope produces `needs_verdict` items rather than wrong auto-verdicts, and
**registration is the seat that can demand main-scale evidence before
"reproducible" counts**. Unknown `requires` keys for a kind are a LOUD
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
  pack receipts enter as `pack-receipt` chain members). **Core ships NO
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
  for any `scope_kind` other than `"registration"`, and vice versa (the
  `scope-unlock` mirror).
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
- **`view_sha` required:** binds the code-rendered brief the human saw
  (canonical-JSON sha of the `verify-registration` projection — the
  `relay_render` posture; D5's archive-vs-interface separation). Validated
  present, not recomputed at the gate (the T8 provenance-witness ruling —
  the recompute locks are the three sha legs).

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
  (`ops/attention_queue.py::_fanout_for` — the one dispatch from item kind
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
moves +1 (`verify-registration`; the registry is 138 as of 35a954a3 —
re-check at implementation time). The `ScopeKind` literal change also
regenerates schemas. Inventory tails: `docs/generated/operations.md`
regenerates.

**Coordination note:** `ops/decision/journal.py`, `state/decision_journal.py`,
and `ops/attention_queue.py` are HOT files touched by concurrent work —
Wave C is strictly sequential and lands after any in-flight waves on those
files.

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
  `_fanout_for` edges: a registration blocked on a prerequisite counts on
  that prerequisite's item (a non-creating read of the registration
  journals, the `_count_runs_echoing_audit` fail-open posture).

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
| All three recompute legs are server-computed: dossier sha via the ONE signature seam (`compute_dossier_signature`), template sha from disk, every chain sha via its kind's route-through — no wire/resolved field is trusted as a sha the gate then records | T7 fire tests (a store edited between export and append is refused; a fabricated chain sha is refused) + the T3 dry-vs-export byte-equality test | the gate starts trusting a caller-asserted sha (the receipt-laundering hole, at the capital boundary) |
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

(Empty — populate per deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
