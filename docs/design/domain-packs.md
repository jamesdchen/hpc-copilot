# Domain packs — bind-as-data design + implementation plan

**Status: PLANNED (2026-07-07), not yet implemented.** The durable hand-off
for the pack substrate: settled decisions with recorded rationale, the
per-seam declarative schema, the bind/receipt/gate mechanics, and the
file-disjoint task waves for parallel Opus dispatch. Cite `path::symbol`,
never line numbers. Record implementation drift in a drift log at the foot of
this document (the `docs/design/notebook-audit.md` convention).

## Product intent

Core's agnostic surface is IDENTITY, ORDERING, COMPARISON, and COUNTING over
opaque caller content (`docs/internals/engineering-principles.md`, Q1;
`docs/design/rigor-primitives.md`, "The boundary the feature crystallized").
Everything that NAMES what content means — what a holdout is, which reader
functions load data, what a failure pattern implies, what tolerance a metric
deserves — was deliberately deferred to "a domain pack above core," and the
deferrals have accumulated across four design docs. A **domain pack** is that
layer, made concrete: a versioned bundle of DECLARATIVE files (vocabularies,
patterns, mappings, templates) plus domain CHECK CODE that runs entirely
outside core. Core gains the ability to (a) **bind** pack content into an
experiment by hash, (b) carry an opaque `{pack, version, sha}` echo on every
record that used it, and (c) **gate** on named pack RECEIPTS — code
attestations the pack's own execution emitted — without ever running or
interpreting a line of pack logic.

The rigor claim this earns: a run's dossier can prove *which domain standards*
(exact files, exact hashes) it was checked under, and an edit to those
standards revokes every clearance signed under the old ones — the same
drift-revocation the notebook audit already gives source code, extended to the
domain layer.

## Architecture decisions (settled — user-confirmed 2026-07-07)

- **DP1 — core implements BIND-AS-DATA only.** Pack content enters an
  experiment as caller-referenced files: relpath + sha, exactly the
  `_wire/actions/interview.py::_AuditedSource` precedent (a campaign-dir
  relative path core reads and hashes, never a blessed directory, never a
  search path). Binding is an explicit, journalable event — an attestation
  like any other, routed through `state/attestation.py::bind` — and every
  record that consumed pack content carries an opaque `{pack, version, sha}`
  echo. Consequence by construction: pack content changes → the manifest sha
  moves → drift-revocation (`state/attestation.py::reduce`) fires on
  everything signed under the old standards, with no state machine.
- **DP2 — pack CODE never runs in core.** Domain checks execute caller-side
  (the experiment env, or the pack's own CI) and emit RECEIPTS: code
  attestations bound to the shas they checked. The template is the render
  receipt (`state/notebook_audit.py::record_render_receipt` +
  `ops/notebook/record_receipt_op.py` — "the parse IS the recompute": the
  recording verb recomputes the checked shas server-side from disk, so a
  receipt can only ever be recorded against current content and reads STALE
  the instant anything it covered drifts). Gates require named receipts
  CURRENT; they never run domain logic, import pack modules, or evaluate a
  pack predicate.
- **DP3 — distribution is INVISIBLE to core.** A pack may arrive via pip,
  git submodule, a vendored folder, or a tarball — core never validates
  installation, never reads pip metadata, never touches entry points for
  packs. Absence of a pack simply reads as missing receipts / unbound data at
  gate time (the D7 fail-safe posture, `ops/notebook_gate.py`). Core gains
  ZERO knowledge of the plugin lane for packs — the plugin registry seam
  (`examples/plugins/hpc-agent-github-actions/`) remains the CAPABILITY lane
  (backends, renderers); packs are the TRUST lane and trust must be
  content-addressed, not install-addressed.
- **DP4 — receipt slots are caller-authored.** A gate learns WHICH pack
  receipts it requires from caller-authored slot names in the experiment's
  opt-in block — never from a core vocabulary, never from the pack manifest
  alone (a pack cannot self-appoint into a gate). Slot slugs follow the
  caller-authored-id rule (the fabrication class, notebook-audit D3): core
  never invents or defaults one. The future registration kernel
  (`docs/design/attention-queue.md` notes; planned separately — reference it
  as a sibling, do not build it here) is the primary consumer of this slot
  mechanism: a registration template's required-receipt list is exactly a set
  of caller-authored slots.

### Why bind-as-data beats packs-as-plugins (recorded rationale)

`examples/plugins/hpc-agent-github-actions/` shows what packs-as-plugins
would have been: an installable package registering into a core seam,
discovered by import. Right for a backend; wrong for a trust layer, for two
recorded reasons:

1. **Install-state trust.** A plugin-lane pack makes the gate's meaning
   depend on what happens to be importable in the current env. Two machines
   with different installed pack versions would gate the same experiment
   differently, with nothing in the journal recording which standards
   applied — the determinism doctrine's exact enemy. Bind-as-data pins the
   standards to hashes in the journal: the gate's inputs are on disk and in
   the record trail, reproducible anywhere.
2. **The uninstall-softens-gates laundering channel.** If gate strictness
   came from an installed package, `pip uninstall` (or a version downgrade)
   would silently relax every gate with NO journal event — an un-audited
   un-signing, the exact laundering the attestation substrate exists to
   refuse. Under bind-as-data, removing a pack cannot soften anything
   retroactively: the bind record and the receipts remain journaled, and
   *un*-binding is itself an explicit journal event a human can see. Absence
   before any bind is the D7 silent path; disappearance after a bind is a
   loud dangling reference (see "The bind event" below).

## The declarative schema per seam

Every current "pack territory" deferral, what a pack may declare at it, what
core does with the declaration, and what may NOT be declared. The universal
rule: a pack declares **lists, patterns, and mappings** — data core can
match, count, and verify the presence of. A pack never declares an
**executable predicate**: nothing core would have to *evaluate* to know what
it means. Anything requiring evaluation runs pack-side and comes back as a
receipt (DP2).

| # | Seam | Deferral recorded at | Declarative shape | What core does with it | What CANNOT be declared |
|---|---|---|---|---|---|
| S1 | **executes-live reader vocabularies** | `docs/design/notebook-audit.md` Q1 flags; `ops/notebook/lint.py` module docstring ("NEVER a reader-function vocabulary … the Q1 flags ban") | `reader_calls: [<dotted callable name>, …]` — e.g. `["pandas.read_csv", "widgets.load_widget"]` (toy vocab in examples, never a real domain's) | The lint matches call NAMES syntactically (`ast.Call` name identity — the same opacity as `input_roots`) and applies the EXISTING exists-under-roots check to their first string-literal argument. Identity match + the existing path check; core never learns what a reader *does* | A predicate deciding whether a path/arg is acceptable; any argument-semantics rule ("column must exist"); anything beyond name-identity |
| S2 | **failure-features patterns** | `ops/recover/features_glue.py` (`FailureFeatures` evidence vector, #240); the resolver pattern-matches, core's catalog is substrate-only | `failure_patterns: {<pattern_id>: <regex string>, …}` — caller-opaque ids, plain regexes over stderr/log text | Core compiles and matches (COUNT), and records the HIT ids as `pack_pattern_ids: [id, …]` on the evidence vector, with the pack echo. The ids ride to the resolver/human as evidence — core never maps a hit to a category, an action, or a retry decision | A pattern→action mapping; a pattern→failure-category mapping (core's category set stays core's); any auto-recovery behavior |
| S3 | **axis-classification heuristics** | `incorporation/classify_axis_auto.py` (the heuristic classifier); the axis-matcher dispatcher is a declared Q2 assembly point | `axis_hints: [{pattern: <name regex>, axis: <core axis literal>}, …]` — the axis value is from core's EXISTING closed `DataAxis` set (identity against a core vocabulary, not a new one) | **Hints add caution, never clearance** (the scope-lock "locking is the safe direction" posture): when a matching hint AGREES with core's structural heuristic, the classification proceeds unchanged (the hint is echoed as confirmation evidence); when it DISAGREES, the case demotes to needs-decision with both candidates named. A hint can never auto-resolve an axis core's own heuristic would not have resolved | A new axis kind; a hint that auto-resolves; any per-parameter semantics ("this is a seed", "this is a learning rate") |
| S4 | **audit templates** | Works TODAY — say so. `_AuditedSource.template` is already a caller-referenced percent-format `.py` hashed by `state/audit_source.py`; a pack template is just such a file that happens to live in a pack | The template `.py` itself, listed in the pack manifest `files` | Nothing new mechanically. The ONLY addition: when the referenced template is a bound pack file, the `audited_source` block (and its sidecar echo, `ops/notebook_gate.py::audited_source_echo`) carries the `{pack, version, sha}` echo, so the dossier can prove which pack's template gated the audit | Already correctly bounded — template slugs stay opaque; content-meaning checks stay in the pack's own receipt-emitting CI |
| S5 | **tolerance defaults** | `docs/design/reproduction-receipt.md` ("core never picks a tolerance — caller-owned"); `verify-reproduction` compares opaque numbers under a caller tolerance | `tolerances: {<tolerance_id>: <number>, …}` — opaque ids to plain numbers | Pure id→value RESOLUTION at the caller boundary: the caller names a `tolerance_id`, the pack-declaration resolver returns the number + echo, and the number flows down the EXISTING caller-owned-tolerance path unchanged. Core still compares; it still never picks. **Note (corrected, pre-implementation verification 2026-07-07):** the determinism fingerprint DOES demote this seam (its doc calls the demotion "required by this design", and it lands BEFORE packs — slate Phase 3 vs Phase 4). Under the fingerprint's settled precedence (caller `tolerance_spec` override, labeled `caller_override` > measured envelope > **S5 pack default** > exact), the pack value must enter the comparison as its OWN labeled tier inside the fingerprint's precedence resolution around `ops/verify_reproduction.py::_resolve_key_tol` — it must NOT be pre-resolved "at the caller boundary" into the existing `ReproTolerance`/`tolerance_spec` path, because a pre-folded value is indistinguishable from a caller override and would OUTRANK a measured envelope (the exact inversion the fingerprint's precedence row forbids). This plan therefore ships only the shape-only `tolerances` loader + echo (T1/declarations); the consumer wiring belongs to the fingerprint's precedence seam and is deliberately NOT a task here | A per-metric semantic ("loss uses X, accuracy uses Y" as core-visible meaning — the *caller* maps metrics to tolerance ids); a tolerance FUNCTION |
| S6 | **registration template fields** | The registration kernel (sibling, planned separately; `docs/design/notebook-audit.md` reuse-accounting + `docs/design/attention-queue.md`) | `registration_fields: [<field slug>, …]` and `required_receipts: [<slot slug>, …]` — presence lists the future kernel counts | RESERVED in this plan: the manifest schema carries the seam name and `state/pack.py` loads it shape-only, but NO core consumer lands here — the registration kernel instantiates it when it lands. Core will only ever verify PRESENCE (every declared field slug has a record; every declared slot has a current receipt) — counting, never interpreting | Field semantics, field validation logic, default values — a registration field is a slug core counts, nothing more |

Shape-validation posture for all seams: `state/pack.py` validates STRUCTURE
only (a list of strings is a list of strings; a mapping's keys are slugs via
the shared `state/scopes.py::validate_tag` slug class; a regex compiles).
It never checks a declared value against a meaning. A seam file that fails
shape validation in a BOUND pack is loud `errors.SpecInvalid` naming the file
(the opted-in-repo-is-broken posture of `ops/notebook_gate.py::_read_required_py`).

## The bind event

**Mechanics.** Binding is a new mutate verb, `pack-bind`
(`ops/pack/bind_op.py`): given a caller-referenced manifest relpath, it reads
the manifest ON DISK, recomputes every listed file's sha (raw-bytes SHA-256,
the `export_dossier` store posture — pack files are not necessarily Python,
so `normalize_source` does NOT apply; see "sha discipline" below), refuses on
any mismatch, and appends a bind record to the decision journal under a new
**dedicated scope kind `"pack"`** (`state/decision_journal.py::SCOPE_KINDS` +
a path branch → `.hpc/packs/<pack_name>.decisions.jsonl` — the exact D3/T7
notebook precedent). The ordering is nominal only: packs and the
registration kernel take the next two scope-kind slots in whichever order
they land; the kinds are independent (`docs/design/registration-kernel.md`
R9). The record:

- `block="pack-bind"`, `response="bound"` (an honest mechanical string, never
  a human-ack token — the `record_auto_clear` naming discipline),
- `resolved={pack, version, manifest_sha, files: [{path, sha256}, …],
  seams: [<seam name>, …]}`,
- projected to a CODE attestation (`attestor="code"`,
  `subject_kind="pack"`, `subject_id=<pack name>`,
  `content_sha=<manifest_sha>`) and routed through
  `state/attestation.py::bind` with the recompute wired to the fresh
  manifest hash — a bind can no more assert a sha into existence than a
  sign-off can (D5 lock 2).

**What the echo rides on.** The experiment opts in via a `packs` block on
`_wire/actions/interview.py::InterviewSpec` (the `audited_source` precedent —
sibling field, persisted verbatim in interview.json, absent → byte-identical):
`packs: [{pack, manifest: <relpath>, receipt_bindings: [{slot, pack}]}, …]`
(the opt-in binding's object form is `receipt_bindings`, renamed from
`required_receipts` in the coherence review 2026-07-07 to disambiguate it
from S6's manifest list `required_receipts: [<slot slug>]`).
Downstream, the opaque `{pack, version, sha}` echo is stamped on every record
that consumed pack content: the lint result when `reader_calls` came from a
pack, the FailureFeatures vector when a pack pattern hit, the `audited_source`
echo when the template was a pack file, the receipt records themselves, and
the run sidecar (→ the dossier, T10). Core copies the echo verbatim; it never
reads it back for meaning (identity only — the `reproduces` field precedent).

**Re-bind = drift.** A second `pack-bind` at a new manifest sha is just a
newer record; `attestation.reduce` over the pack journal makes the old bind
STALE. Currency for every consumer is then defined once: a receipt (or any
pack-echoed clearance) is current only if the pack sha it recorded equals the
sha of the CURRENT bind. Editing pack content without re-binding is equally
revoked: the gate recomputes file shas from disk against the current bind's
recorded shas (the `ops/notebook_gate.py::_linked_source_drift` pattern), so
a changed-on-disk pack file reads as drift even before any re-bind. Either
way: hashes move → everything signed under the old standards reads stale →
re-check, re-receipt, re-sign. No drift state machine (the D8 property).

**Unbound / dangling — the D7/opted-in split, applied.**

- **Absence of the pack = SILENCE.** No `packs` block on interview.json →
  every pack gate returns silently and byte-identically, zero filesystem
  probes beyond the interview.json read the seats already do
  (`ops/notebook_gate.py::_read_audited_source` is the template). A repo
  that never opted in never pays.
- **A DANGLING reference from an opted-in record = LOUD.** An opted-in repo
  whose declared manifest is missing/unreadable/sha-drifted, whose bind
  journal names files that no longer resolve, or whose `receipt_bindings`
  name a pack with no current bind, raises `errors.SpecInvalid` naming the
  path/slot — a broken setup, never a silent pass (the
  `_read_required_py` posture: D7 silence applies ONLY to the absent
  opt-in block, resolved first). Recorded reason: a silent pass on a
  dangling reference IS the uninstall-softens-gates laundering channel
  reintroduced one layer up — deleting the pack folder must not quietly
  relax a gate the caller explicitly opted into.

**Sha discipline** (`docs/internals/harness-contract.md`, "The sha
canonicalization (normative)"): pack FILES hash as raw bytes (SHA-256,
lowercase hex — the dossier manifest-entry form), EXCEPT percent-format `.py`
audit templates, which keep their existing normalized-source shas
(`state/audit_source.py::normalize_source`) because the notebook gate already
recomputes them in that form — one file, one canonical form, decided by which
existing recompute consumes it. Receipt `content_sha` values are
canonical-JSON shas (the normative `json.dumps(sort_keys=True, separators=
(",", ":"), ensure_ascii=False)` form). No new canonicalization is invented.

## Receipt naming + the gate contract

**How a gate names required pack receipts: caller-authored SLOTS (DP4).**
The `packs` opt-in block carries `receipt_bindings: [{slot: <slug>,
pack: <name>}, …]`. A slot slug is the caller's name for one obligation
("data-audit", "stats-check" — toy examples; slugs are opaque to core). The
pack manifest may LIST the slots its checks can fill
(`fills_slots: [<slug>, …]`, identity only, so `pack-status` can report an
unfillable requirement early), but the REQUIREMENT always originates with the
caller — a pack cannot appoint itself into a gate, and core never defaults a
slot (the fabrication class).

**Recording a receipt.** `pack-record-receipt` (`ops/pack/record_receipt_op.py`,
the `notebook-record-receipt` template): given `{pack, slot,
checked: [<relpath>, …], passed: bool, evidence: <opaque>}`, the verb
recomputes ON DISK the sha of every checked file AND the current bind's
manifest sha, builds `content_sha` = canonical-JSON sha of
`{manifest_sha, checked: {relpath: sha, …}}` **server-side** (the parse IS
the recompute — never caller-asserted), binds through
`state/attestation.py::bind`, and appends
`block="pack-receipt"`, `response="checked"` (mechanical, never an ack),
`resolved={pack, version, manifest_sha, slot, checked, passed, evidence,
attestor:"code"}` to the pack's journal. `passed` is a mechanical boolean
(the render receipt's `error: bool` precedent — comparison, not
interpretation); `evidence` is opaque, never read by core.

**Currency semantics — the notebook_audit reduction reused, one definition.**
`state/pack_receipts.py` reduces a slot's receipt records through the ONE
kernel (`attestation.reduce`, newest-last append order, `subject_id=<slot>`),
with `current_sha` recomputed from disk at read time. A receipt is CURRENT
iff nothing it covered moved: the current bind's manifest sha and every
checked file's on-disk sha still hash to the recorded `content_sha`. Stale
receipt = missing receipt (drift = unsigned by construction; a stale CODE
record has no human to inform — the T6 stale-auto-clear ruling, reused).
Never a re-inlined newest-first or sha-compare — the enforcement-map "one
kernel" row applies, and each new member accrues its `inspect.getsource`
route-through assertion.

**The gate.** `ops/pack_gate.py::assert_pack_receipts_current` — ONE
definition, the two synchronous notebook-gate seats:
`ops/resolve_submit_inputs.py` (pre-sidecar, the S1 human boundary) and
`ops/submit_flow.py` (pre-staging, before any SSH). Not opted in → silent
byte-identical return. Opted in → for every `receipt_bindings` entry, the
slot's reduction must be CURRENT **and** `passed=true`; otherwise raise
naming every failing slot and its status (missing / stale / failed). Refusal
reuses `error_code="precondition_failed"` (the `SourceUnaudited` /
`ScopeLocked` precedent — no new wire enum). Broken-setup cases (dangling
manifest, unresolvable pack) raise `SpecInvalid` instead, per the T9 refusal
split.

**The un-fakeability story, end to end.** (1) No unlock-shaped affordance: a
receipt exists only via `pack-record-receipt`, whose shas are server-computed
— an agent cannot assert a receipt for content not on disk. (2) Core
recomputes at BOTH ends: record time (bind lock) and gate time (currency
reduction) — a receipt survives only while every byte it covered is
unchanged. (3) The check itself ran outside core, but what the gate trusts is
not the check's honesty — it is the journaled, hash-bound claim "this code,
at these shas, under this pack version, reported passed" — exactly the trust
grade of a render receipt, and honestly NO MORE: a pack receipt is evidence a
check ran, never proof the check is correct (that is the pack's CI's
problem, per Q4: core CI verifies core's handling with crafted fixtures, the
pack's CI carries the domain dependency).

**Receipts never soften the human tiers.** A pack receipt is a CODE
attestation. It can satisfy a code-receipt slot; it can never substitute for
a human sign-off, auto-clear a HUMAN_REQUIRED notebook section, clear a scope
unlock, or downgrade anything D-attention routes to a human. The two
attestor classes share one record shape but distinct locks (authorship vs
recompute — `state/attestation.py` module docstring), and no gate may accept
`attestor="code"` where its contract names a human. Enforcement row below.

## The pack manifest

**Minimal shape** — a pack file itself, at a caller-referenced relpath,
hashed like the rest (raw bytes; its sha IS the pack identity sha):

```json
{
  "name": "toy-widgets",
  "version": "1.2.0",
  "files": [{"path": "vocab/readers.json", "sha256": "…"}, …],
  "seams": {"reader_calls": "vocab/readers.json",
            "failure_patterns": "patterns/failures.json"},
  "fills_slots": ["widget-audit"]
}
```

- `name` — a slug (the shared `validate_tag` class; it keys the journal
  path, so it must be filesystem-safe).
- `version` — an opaque string core echoes and never compares (no semver
  logic in core; ORDERING between versions is the sha's job, via bind
  order).
- `files` — every pack file with its raw-bytes sha; the closed integrity
  set. A seam pointer must name a listed file.
- `seams` — seam name → declaration-file relpath, keys drawn from the
  CLOSED seam vocabulary `state/pack.py::SEAM_NAMES` (equality-pinned, the
  `DOSSIER_SOURCES` pattern — adding a seam is a reviewed vocabulary
  change). *(Pre-implementation note 2026-07-07: one such reviewed
  addition is already anticipated — `actor_policy`, reserved by
  `docs/design/multi-human.md` MH8 for team delegation policy; it enters
  SEAM_NAMES via this doc's own reviewed-vocabulary process when
  multi-human lands, not before.)*
- `fills_slots` — advisory identity list (see the gate contract).

**Where it lives:** wherever the caller says — inside the experiment repo,
a vendored `packs/toy-widgets/` folder, a pip-installed package's data dir
the caller points at by path. Core resolves the relpath against the
experiment dir exactly as `_AuditedSource.source` resolves; DP3 means core
never asks how the bytes got there.

**What core reads from it: identity only.** Name, version, file list + shas,
seam pointers, slot list — every one an identity/pointer. Core never
executes, imports, or interprets a manifest-named file beyond the shape-only
seam loaders in `state/pack.py`. No `default_pack`, no bundled pack in
package data, ever (enforcement row below; the clusters.yaml package-data
hazard is the cautionary precedent for shipping caller content in the wheel).

## Task waves (file-disjoint, for parallel Opus dispatch)

Every task: fires+passes test pair required (each new refusal demonstrates
its fire path on a synthetic violation — the
`test_lint_rule_fires_on_synthetic_input` doctrine). New verbs ⇒ run ALL SIX
regen scripts (`scripts/bake_operations_json.py --write`,
`scripts/build_verb_module_map.py`, `scripts/build_operations_index.py`,
`scripts/build_schemas.py`, `scripts/build_primitive_index.py`,
`scripts/build_primitive_frontmatter.py`) — the 0.8.0 lesson; registry count
moves +3 (`pack-bind`, `pack-record-receipt`, `pack-status`; the registry
is 141 as of e1e9ab27; cross-slate sum = 146 after packs(+3) /
registration(+1) / kit(+1) — re-check at implementation).
Inventory tails: `docs/generated/operations.md` regenerates; the dossier
closed store set gains two nouns (T10, a reviewed vocabulary change).

**Wave A (parallel — all files new):**

- **T1** `state/pack.py` (new) — manifest model + `SEAM_NAMES` (closed set)
  + raw-bytes sha helper + shape-only seam-declaration loaders (lists /
  mappings / regex-compiles; slug validation via the shared tag class).
  Tests: `tests/state/test_pack.py` — crafted manifests, each refusal fires
  (bad seam name, unlisted seam pointer, sha mismatch, non-slug name).
- **T2** `state/pack_receipts.py` (new) — the slot reduction over the pack
  journal, routing through `state/attestation.py::reduce` (never re-inlined;
  ships its `inspect.getsource` route-through assertion) + the
  bind-currency read (`current_bind`, newest-valid `pack-bind` projection).
  Tests: `tests/state/test_pack_receipts.py`.
- **T3** `_wire/actions/pack_bind.py`, `_wire/actions/pack_record_receipt.py`,
  `_wire/actions/pack_status.py` (new) — Pydantic wire models. Boundary
  rule: no domain vocabulary in field names (the
  `tests/contracts/test_dossier_boundary.py::_FORBIDDEN_FIELD_NAMES` walk,
  mirrored in T11).

**Wave B (after Wave A, parallel — one file each):**

- **T4** `ops/pack/bind_op.py` (new) — the `pack-bind` mutate verb per "The
  bind event". Tests include the loud dangling-manifest refusal firing.
- **T5** `ops/pack/record_receipt_op.py` (new) — the `pack-record-receipt`
  mutate verb per the gate contract (server-side recompute; unknown
  slot/pack → skipped-vs-loud per the D7 split). Template:
  `ops/notebook/record_receipt_op.py`.
- **T6** `ops/pack/status_op.py` (new) — `pack-status` query: current bind,
  per-slot receipt status, unfillable-requirement report. Read-only.
- **T7** `state/pack_declarations.py` (new; **placement corrected from
  `ops/pack/declarations.py`, pre-implementation verification 2026-07-07**) —
  the ONE seam-declaration resolver: reads the opt-in block + current bind +
  seam files → typed opaque lists/mappings + the `{pack, version, sha}` echo.
  It MUST live in `state/`, not `ops/pack/`: its named consumers sit in OTHER
  ops subjects (`ops/notebook/lint.py` = the `notebook` subject,
  `ops/recover/features_glue.py` = the `recover` subject), and
  `scripts/lint_subject_imports.py` refuses cross-subject `ops.pack` imports
  from them — subjects compose only via the `state`/`infra` substrate (the
  `ops/notebook/record_receipt_op.py` module docstring states the rule). The
  resolver is pure I/O + reduction (opt-in read, `state/pack.py` loaders,
  `state/pack_receipts.py::current_bind`), so state placement is natural.
  Consumers (T9x) call this and stay pack-ignorant in their own logic:
  `notebook-lint` still just receives a `reader_calls` list the way it
  receives `input_roots`.

**Wave C (sequential — hot files, one at a time):**

- **T8** `state/decision_journal.py` — the dedicated scope kind `"pack"` +
  path branch (the notebook T7 precedent; contract tests updated in
  lockstep; packs and the registration kernel claim the next two slots in
  whichever order they land — the kinds are independent).
- **T8a** the InterviewSpec `packs` opt-in block —
  `_wire/actions/interview.py::InterviewSpec` gains the
  `packs: [{pack, manifest, receipt_bindings: [{slot, pack}]}, …]` field
  (sibling to `audited_source`, persisted verbatim, absent → byte-identical)
  + `ops/memory/interview.py` persistence. Sequenced after v1.6's
  `_AuditedSource` change (landed). Regen (wire change).
- **T9** `ops/pack_gate.py` (new) + the TWO seat wirings
  (`ops/resolve_submit_inputs.py`, `ops/submit_flow.py` — hot files) +
  enforcement rows. Refusal split per the gate contract.
- **T9a** `ops/notebook/lint.py` + `_wire/actions/notebook_lint.py` — S1:
  optional `reader_calls: list[str]` on `NotebookLintInput` (caller-declared
  opaque, default empty → byte-identical); `_check_executes_live` gains the
  name-identity call match. Regen (wire change).
- **T9b** the failure-features seam — S2: `pack_pattern_ids` on the evidence
  vector (`ops/recover/features_glue.py` + `schemas/failure_features.json`;
  match-and-record only). Regen (schema change).
- **T9c** `incorporation/classify_axis_auto.py` — S3: hint
  confirmation/demotion (agreement echoes, disagreement demotes to
  needs-decision; never auto-resolves).
- **T9d** `ops/notebook_gate.py` — S4's ONLY mechanical addition (added in
  pre-implementation verification 2026-07-07: this edit previously had no
  owning task): when the `audited_source` template resolves to a bound pack
  file, `audited_source_echo` carries the `{pack, version, sha}` echo.
  Hot file (the notebook gate); sequenced inside Wave C like T9.
- **T10** sidecar `packs` echo + dossier: `ops/export_dossier.py` gains store
  nouns `pack-manifest` + `pack-journal`;
  `tests/contracts/test_dossier_boundary.py::_EXPECTED_SOURCES` updated in
  the same commit (the closed-set equality pin makes this a deliberate,
  reviewed change — that is the pin working).
- **T11** `tests/contracts/test_pack_boundary.py` (new) — the enforcement
  suite (rows below).

**T12 — the FIRST CONSUMER (after Wave C): the TOY pack.** A complete
`examples/packs/toy-widgets/` + `tests/fixtures/toy_pack/` exercising EVERY
seam end-to-end: manifest, a reader vocabulary (`widgets.load_widget`), a
failure pattern (`widget-jam`), an axis hint, an audit template with toy
sections, a tolerance mapping, a ~30-line caller-side check script that
emits a `pack-record-receipt` call, and an integration test driving
bind → lint-with-vocab → receipt → gate-pass, then editing one pack file and
asserting the gate REFUSES (drift-revocation live). **Toy-domain vocabulary
only — never harxhar's** (the toy-domain fixture rule: real domain words in
fixtures would smuggle a vocabulary into the tree that greps and future
maintainers mistake for core knowledge).

**T13** — this doc: status flip + drift log, at the end.

### Enforcement rows (accrue to `docs/internals/engineering-principles.md` maps)

| Rule | Enforced by | Fires when |
|---|---|---|
| The seam vocabulary is CLOSED and shape-only: `state/pack.py::SEAM_NAMES` equals the agreed set exactly; seam loaders validate structure, never meaning | `tests/contracts/test_pack_boundary.py` (the `DOSSIER_SOURCES` equality-pin pattern) | a seam name is added ad hoc, or a loader grows a value-meaning check (a recognized reader name, a privileged pattern id) |
| Core ships NO default pack and NO pack vocabulary: no manifest, seam file, or vocabulary constant in package data or core source | same suite (package-data scan + a no-literal-vocab AST pin over `ops/pack/` + `state/pack.py`) | a pack file lands under `src/hpc_agent/`, or a core module inlines a reader/pattern/axis-hint vocabulary |
| Core never imports/executes pack content: no `importlib` / `entry_points` / `exec` / `eval` anywhere in `ops/pack/` or `state/pack.py` (DP3: distribution invisible; DP2: code never runs in core) | same suite (AST pin, the `test_bundler_copies_bytes_and_never_parses_content` form) | a pack module gains an import-or-execute path over pack-named content |
| Pack attestations route through the ONE kernel — bind, receipt, and reduction never re-inline recompute-and-compare or newest-first drift | `tests/state/test_pack_receipts.py` route-through assertions (the accruing-member rule on the existing attestation row) | a pack record path bypasses `state/attestation.py::bind`/`reduce` |
| Receipt shas are server-computed: `pack-record-receipt` recomputes every checked sha from disk; no wire field lets a caller assert a `content_sha`/`manifest_sha` the verb then trusts | `tests/ops/pack/test_record_receipt.py` fire test (an entry whose on-disk content changed between caller-read and record is refused) + a wire-schema pin (no caller-suppliable sha field) | the verb starts trusting a caller-supplied sha (the v1 receipt-laundering hole, re-opened one layer up) |
| A CODE receipt never satisfies a human tier: no gate accepts `attestor="code"` where its contract names a human; pack receipts appear in no authorship-gate path in `ops/decision/journal.py` | `tests/contracts/test_pack_boundary.py` (the no-affordance pin: `pack-receipt` blocks are absent from every human-tier block set) + the existing `_assert_signoff_authorship` fire tests | a pack receipt clears a HUMAN_REQUIRED section, an unlock, or anything D-attention routes to a human |
| No pack-domain vocabulary on the wire: pack wire models expose no field NAME from the forbidden domain set; the echo is `{pack, version, sha}` and nothing more | `tests/contracts/test_pack_boundary.py` (the `_schema_property_names` recursive walk, mirrored from the dossier suite) | a wire model grows a meaning-bearing field ("metric", "holdout", a reader name as a field) |

## Boundary-drift flags (the Q1 watch list for this feature)

- **Core never interprets pack values.** A reader name is matched by
  identity; a pattern id is counted; a tolerance is a number the caller
  routes; an axis hint can only demote to a human. The moment a core branch
  reads a declared VALUE for meaning ("if the pattern id is `oom`…"), the
  line is crossed.
- **Core never ships a default pack** — no bundled vocabulary, no fallback
  manifest, no "standard" pack in the wheel. An experiment with no `packs`
  block behaves byte-identically to today, forever.
- **Core never validates installation** (DP3). No pip probe, no import
  check, no version-compatibility logic. Absence = missing receipts at gate
  time; dangling opted-in references = loud; nothing else.
- **Core never matches on field meanings.** Seam loaders are shape-only;
  the manifest read is identity-only; `evidence` stays opaque end to end.
- **Pack receipts never soften the human tiers.** A pack cannot auto-clear
  what D-attention routes to a human; sign-off UX pressure to let a
  "trusted pack" skip the human bar is the feature working, not a bug —
  soften only via richer human-side evidence, never via code attestations.
- **`fills_slots` stays advisory.** If it ever becomes load-bearing (a pack
  self-registering into a gate), DP4 is broken — requirements originate
  with the caller, always.
- **The version string stays opaque.** Semver comparison, compatibility
  ranges, "minimum pack version" logic — all of it is pack-side or
  caller-side; core orders by bind records and compares by sha.

## Related, planned separately

- **The registration kernel** — the primary consumer of receipt slots (S6);
  a sibling design that instantiates the attestation kernel for
  pre-registration. This plan reserves its seam and builds nothing of it.
- **The determinism fingerprint** — may demote the tolerance-defaults seam
  (S5) from primary source to fallback; the S5 resolver is designed to be
  removable.
- **The notebook-render plugin lane** (`examples/plugins/hpc-agent-notebook-render`)
  — remains the CAPABILITY lane for pack-adjacent tooling (a pack's check
  runner, a renderer). A pack may SHIP such tooling; core's trust in the
  pack still flows only through bound data + receipts, never through the
  plugin registry.

## Implementation drift log

- **2026-07-07 (pre-implementation verification, adversarial review — three
  corrections applied in place, all against the live tree):**
  1. **S5 precedence corrected.** The original S5 cell said the fingerprint
     "may demote" the seam and had the pack value resolve "at the caller
     boundary" into the existing caller-owned-tolerance path. Verified
     against `ops/verify_reproduction.py::_resolve_key_tol` (a value entering
     via `ReproTolerance` per_key/default IS a caller tolerance) and
     `docs/design/determinism-fingerprint.md` (demotion "required by this
     design"; precedence caller > measured > S5 > exact; fingerprint lands
     in slate Phase 3, before packs in Phase 4): a literal implementation
     would have ranked pack defaults ABOVE measured envelopes. S5 now ships
     loader + echo only; consumer wiring is the fingerprint precedence
     seam's.
  2. **T7 moved `ops/pack/declarations.py` → `state/pack_declarations.py`.**
     `scripts/lint_subject_imports.py` forbids `ops/notebook/lint.py` (T9a)
     and `ops/recover/features_glue.py` (T9b) from importing an `ops/pack/`
     module — cross-subject. The resolver is pure I/O, so it moves to the
     `state` substrate both subjects may import.
  3. **T9d added.** S4's echo edit (`ops/notebook_gate.py::audited_source_echo`
     gains the pack echo) was described in the seam table but owned by no
     task; T10 only covers the sidecar + dossier.
  - Cite-integrity re-verified same pass: `state/attestation.py::bind`
    accepts a sha string for `recompute` (the bind event's "fresh manifest
    hash" wiring is directly implementable); `SCOPE_KINDS` +
    `decisions_path` branch shape matches the planned `"pack"` kind;
    `_AuditedSource` (with v1.6's `input_roots`/`source_roots`/
    `attention_order`) is landed, so T8a's sibling-field sequencing holds;
    the notebook receipt template, gate seats
    (`ops/resolve_submit_inputs.py`, `ops/submit_flow.py`), all six regen
    scripts, and registry 141 (`operations.json` length, matching the
    e1e9ab27 baseline claim) all check out.

- **Fifth-pass adversarial verification 2026-07-08 (independent Opus sweep;
  no code had landed) — GO-WITH-CHANGES.** The named S5 re-inversion fix
  holds where the precedence ORDER appears, but left two adjacent statements
  stale. (1) The "Related, planned separately" bullet still says the
  fingerprint "may demote" S5 and "the S5 resolver is designed to be
  removable," contradicting the corrected S5 cell (demotion required; this
  plan ships the loader + echo only, no S5 resolver) — and
  `docs/design/determinism-fingerprint.md` quotes that "may demote … removable"
  sentence VERBATIM, so both must be fixed in lockstep. (2) The S5 cell defers
  S5 consumption to "the fingerprint's precedence seam around
  `_resolve_key_tol`," but the fingerprint's concrete D-consume precedence
  carries NO S5 pack-default tier — a numeric case (no caller override, no
  envelope yet, pack default 5%, observed 2%) resolves two different ways
  depending on which fingerprint precedence statement you read. Weaken the S5
  cell to "the S5 tier is RESERVED; its consumer is unbuilt until fingerprint
  `_resolve_key_tol` is extended." No boundary-drift, laundering, or
  growth-trigger defect (all seams decompose to IDENTITY/COUNTING/COMPARISON;
  the bind-as-data core is sound).

(Populate per further deviation, each with its recorded reason, when
implementation lands. The `docs/design/notebook-audit.md` drift log is the
form to follow.)
