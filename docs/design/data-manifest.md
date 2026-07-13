---
status: shipped
---
# The data manifest — rung 0 of the onboarding map

**Status: IMPLEMENTED (landed in the slate merge train, 2026-07-09; originally USER-RULED 2026-07-07, ruling 0a/0b).** Companion to
`docs/design/onboarding-map.md` (rung 0) and an AMENDMENT INPUT to
`docs/design/determinism-fingerprint.md` (the data-identity dimension).
Registry: +1 (`data-manifest`), from the 142 @ `326a9124` baseline.

## Motivation — the worked example (2026-07-07, harxhar-clean)

Two OptionMetrics parquets were added to `data/` by an unrecorded ingest;
the next local `load_raw_data("data")` died with `KeyError: 'endbartime'`
deep inside a pandas merge, with nothing connecting the crash to the file
additions. Diagnosis required manually schema-checking every parquet.

The manifest converts data changes from **invisible to attributed** — it
does NOT prevent them (prevention is caller/pack-side, where format
knowledge lives; the caller-side fix that night was `loading.py` learning
to skip non-bar-keyed parquets loudly). The class the manifest uniquely
covers is the QUIET one: same filename, silently rebuilt bytes (vendor
restatement, a re-run of a build script), every downstream result subtly
wrong, nothing ever throwing. No robustness layer catches that, because
nothing fails — only an identity record sees it.

## The verb

`data-manifest` — mutate, `side_effects [file_write]`, agent_facing.

- **Spec**: `{roots?: [relpath], output_path?}`. When `roots` is absent,
  default to the EXISTING input declaration — `audited_source.input_roots`
  when the experiment opted in, else the interview's declaration. ONE
  "what are my inputs" declaration in the system (the one-definition rule
  applied to a declaration); a hardcoded `data/` default is REFUSED as a
  design choice (core never guesses which directories are data).
- **Home**: `.hpc/data_manifest.json` (ruled 0a) — it sits with
  `interview.json`/`axes.yaml` as a copilot-consumed caller record,
  git-trackable, machine-minted.
- **Record shape**: `{relpath: {sha256, size, built_by?}}` + a manifest-doc
  sha computed via the ONE canonical-sha definition (P-S1 UNIFIED — see the
  drift log; `state.determinism.canonical_sha`; file-content shas are
  raw-byte hashes — two hash disciplines, each in its lane, allowlisted in
  the grep lint). `built_by` is OPTIONAL caller-authored free text, carried
  opaquely (the scope-tag pattern) — the build audit itself is out of
  scope, disclosed-absent.
- **Mint is journaled** — re-minting after a legitimate data change is a
  recorded act; the mint history IS the tier-0 "who changed the data,
  when" timeline the repo otherwise lacks.
- **Performance**: content-keyed cache by `(size, mtime)` fast-path
  (describe-cache precedent); re-checks never re-hash unchanged gigabytes.

## The consumers (ranked)

1. **The run sidecar echoes input-data shas at submit** — data identity
   rides every run record. This is the Phase-3 amendment (below), and the
   load-bearing consumer.
2. **Greenlight brief disclosure** — counts and identities only: "N match,
   M drifted, K new, J missing, or: no manifest (runs invisible to
   data-drift attribution)". VERDICT-FREE: core never says
   "updated/appended/corrupted"; drift meaning is human judgment at the
   brief. **AND LLM-FREE (user-pinned 2026-07-07): the interpretation
   "these bytes differ ⇒ drifted" is itself MECHANIZED — the drift report
   is a deterministic code-rendered projection (sha-able, the
   trusted-display class), and the LLM's only role is POINTING the human
   at it, relay-verbatim. The LLM never re-derives, re-summarizes, or
   characterizes drift; a relayed drift section that doesn't match the
   code render is the existing relay-audit violation.**
3. **Audit sections** assert against it caller-side (the template's
   `universe-and-alignment` prints shas today; the manifest is its
   comparison target).
4. **THE TRANSFER SEAM (added 2026-07-08 from the live run-#10 data-loss
   chain): deploy auto-protects the declared roots.** `rsync_push`/the
   tar fallback union the experiment's declared `input_roots` into the
   protect/exclude set as ANCHORED patterns (`./data`, never bare `data`
   — the bare form silently also dropped `src/data` from a ship, the F-H
   footgun). Stage-then-swap (shipped, `84c33730`) removed the
   died-mid-transfer catastrophe; this closes the remaining aim problem —
   a *successful* deploy with a wrong caller exclude could still clean a
   data dir inside the tree. One declaration, consumed by a second seat;
   an experiment with NO declared roots gets a disclosure line ("input
   roots undeclared — data dirs unprotected by deploy"), never a guess.
   Pack-level doctrine rides alongside (onboarding guidance, not core
   enforcement): reference data OUTSIDE the deploy tree when practical —
   code trees get deployed, data trees get referenced.

No separate check verb — the brief surfaces are the check consumers.

## The attention contract (user-ruled 2026-07-07: prevent decision fatigue)

Disclosure without tiering is fatigue. The manifest's alarms obey the
D-attention pattern and four locks:

- **Tier map** (shipping requirement, not a nicety): a TRACKED file's
  sha changing = needs-attention (the quiet-corruption class); NEW
  untracked files = low tier, one line; NO manifest = one STANDING
  disclosure, never a per-run repeat.
- **One queue**: drift items route into the attention-queue and compete
  under the leverage sort (a drift blocking a reproduction verdict
  outranks one on an idle experiment). No dedicated alarm surface.
- **Fire on state change, age while unresolved**: first occurrence loud;
  thereafter one aging line ("first seen Nd, unacknowledged"). Never
  re-fire at full volume.
- **Acknowledgment = re-mint**: re-minting the manifest IS the journaled
  "this is the new known-good" act — re-tiers the alarm to
  cleared-with-reference and re-arms it. Silence-by-record, never
  suppression; and re-tiering an alarm CLASS's default is a human ruling,
  never adaptive (the system must not learn to quiet itself).

## The fingerprint amendment (ruled 0b; lands with Phase 3)

The fingerprint's staleness model currently knows one reason prior samples
stop counting: code identity drift (`tasks_py_sha` family). Without a data
dimension, a parquet rebuild reads as "this experiment is nondeterministic"
— a FALSE mismatch poisoning the envelope. The amendment:

1. Submit echoes manifest shas of files under the declared input roots
   into the sidecar (serializes with Phase 3/4's `submit_flow` edits).
2. Fingerprint samples are comparable ONLY within the same data identity;
   a sample under different data is never admitted as nondeterminism
   evidence — it is disclosed as data drift.
3. `reproduce-run`'s drift guard grows from two dimensions to three
   (code, env, **data**); verify verdicts state which dimension moved, or
   "data identity unknown (no manifest at record time)" — disclosed,
   never blocking, never fabricated.

## Enforcement posture

**Disclosure-only in v1** (the accept-with-disclosure rule). The strict
seat is deliberately deferred to the registration kernel: a
`manifest-current` prerequisite demand in the registration template's
vocabulary (ruled 0a — RECORDED HERE AS THE NOTE: when kernel R3's
prerequisite kinds land, add `manifest-current` alongside `scope-budget`;
strictness is then caller-declared per registration, never a core mood).

## Agnosticism mechanisms (the boundary test, held six ways)

1. **Opaque bytes** — core hashes, never parses; no pyarrow/pandas import
   in the manifest module (library-knowledge-boundary lint row).
2. **No invented vocabulary** — semantic fields are caller free text,
   stored and echoed, never validated.
3. **Caller-declared roots** — reused from the existing declaration;
   no `data/` convention in core.
4. **Verdict-free comparison** — counts and identities; humans conclude.
5. **Toy fixtures** — tests hash text files and random bytes, never a
   parquet, never quant vocabulary (the domain-packs pattern).
6. **Never-blocking pin** — a contract test asserts the disclosure path
   contains no raise/gate branch (the evidence-memory pattern verbatim).

Format-aware validation is PACK content forever: caller-executed,
receipt-producing, binding to the manifest's identity substrate. **The
first named rule is on the books (2026-07-07, from the live failure) —
and its altitude was corrected TWICE the same night, which mints the
three-level rule vocabulary:**

1. **Data shape (discipline-agnostic): D-V1 "keyed-panel conformance"** —
   a declared keyed-panel root names its alignment key; every top-level
   file carries it; non-conforming files live in subdirectories or are
   skipped loudly. Parameterized `{root, key}`. NOT quant knowledge —
   epidemiology/climate panels obey it identically. Shape rules
   (keyed-panel, monotonic event stream, point-in-time vintage,
   snapshot-with-revisions) can eventually be a shared PACK-SIDE commons;
   never core (core stays bytes-only).
2. **Discipline catalog (the quant pack's REAL data contribution)** —
   which families the discipline uses, each bound to a shape, plus the
   discipline's semantics: bar panels at a declared frequency; option
   chains (keyed-snapshot shape) with arbitrage-sanity checks;
   fundamentals joined only by vintage (look-ahead through revisions is
   the quant sin). Tonight's failure at THIS altitude: two families of
   different shapes shared one root with no catalog to say so.
3. **Program binding**: RV program, `{root: data/, key: endbartime,
   freq: 30min}` — key names and frequencies are program data.

**Corrections logged: naming `endbartime` collapses tiers 3→2; naming
"panel" as if it were the discipline collapses tiers 2→1 (quants also use
chains, ticks, vintaged fundamentals — the OptionMetrics files in this
very repo are the non-panel witness). The altitude test both ways: "would
a second program adopt it unedited?" (2→3 leak) and "would a second
DISCIPLINE adopt it unedited?" (1→2 leak). Today the rule exists as
caller code (harxhar-clean `src/data/loading.py`'s skip-with-warning) +
this note; when packs land, extraction collects shape rules to the
commons, the catalog to the quant pack, bindings to program specs.**

## Sequencing

- The verb + brief disclosure: self-contained; land post-run-#10, before
  or alongside Phase 1 (registry 142→143).
- The sidecar echo + fingerprint amendment: INSIDE Phase 3 (hot-file
  serialization with `submit_flow`).
- The `manifest-current` prerequisite: Phase 2's vocabulary, one row.

## Drift log
- **Status flip lag (2026-07-09):** the implementation landed in the slate merge train but this doc's status stayed PLANNED; caught by the anti-vendor-lockout plan's inventory sweep (same class as the conformance-kit flip lag). Verified against src before flipping.


- 2026-07-09: **P-S1 canonical-JSON sha UNIFIED — one definition.** The debt was
  three sibling copies of the harness-contract canonicalization. The definition
  is now `state/determinism.py::canonical_sha` (the pure kernel;
  `compute_content_sha(a, b) = canonical_sha([a, b])`). Re-pointed:
  `state/data_manifest.py::manifest_doc_sha` (the `_canonical_json` copy DELETED
  → `determinism.canonical_sha(records)`) and
  `state/fingerprint_store.py::content_sha_over_payloads` (its `_canonical_json`
  copy DELETED → `determinism.compute_content_sha`). `state/evidence.py::
  citations_content_sha` already routed. Byte-for-byte pins:
  `tests/state/test_data_manifest.py::test_manifest_doc_sha_routes_to_canonical_sha_byte_for_byte`
  and `tests/state/test_fingerprint_store.py::test_content_sha_over_payloads_routes_to_kernel_byte_for_byte`.
  Note: `data_manifest`'s old copy lacked `ensure_ascii=False`; adopting the
  canonical form is byte-identical for ASCII records (relpaths/`built_by`) and,
  for the rare non-ASCII case, converges on the ONE harness-contract form (a
  strict move toward the single definition, not a regression). Left as-is (a
  DIFFERENT canonicalization lane or a different subject, out of this debt's
  scope): `state/run_sha.py` (the run-IDENTITY discipline — cmd_sha/node_sha
  dedup keys; no `ensure_ascii=False`; changing its bytes would bust dedup +
  journal keys), `ops/provenance_manifest.py::manifest_signature` (operator-
  signable digest; no `ensure_ascii=False`), `ops/check_task_generator_mismatch.py`
  (canonical-STRING comparator, not a doc sha). The `state/conformance*` canonical
  copy stays for a later pass (owned by the Wave-C interlock).
- 2026-07-07: written (Fable, pre-deadline), rulings 0a/0b folded.
- 2026-07-08: **the fingerprint amendment LANDED (Phase-3, the three legs).**
  - **`data_sha` shape PINNED — one canonical sha over the manifest's `files`
    record** (`state/data_manifest.py::data_identity` → `manifest_doc_sha(files)`,
    recomputed FRESH from the authoritative files map, never the stored
    `manifest_doc_sha` field). Rejected the per-root file-sha MAP as the sidecar
    leg: the fingerprint needs ONE comparable identity string, and the doc-sha
    moves iff any declared-input file's `sha256`/`size` moves (or a file
    appears/vanishes under a root) — the quiet-corruption class exactly. `None`
    when no roots declared / no manifest minted / empty files (disclosed-unknown,
    never fabricated).
  - **Sidecar field COLLISION resolved: a NEW `data_manifest_sha` field**, NOT
    the existing `data_sha`. The sidecar already carried `data_sha` = the
    `input_datasets`/DVC identity (`compute_data_sha`, #222) — a DIFFERENT
    mechanism. Overloading it would conflate two data-identity disciplines under
    one key (which wins when both present?). `data_manifest_sha` is additive +
    only-write-non-None, so a run with no manifest writes a byte-identical
    sidecar. The fingerprint's data leg (T1's `DATA_IDENTITY_FIELD = "data_sha"`
    on the SAMPLE identity) is fed FROM `data_manifest_sha` at the seams
    (submit's double-canary mint, verify-reproduction) — the sample's generic
    `data_sha` leg, sidecar's manifest-specific field.
  - **Leg 1** — `submit_flow._spec_provenance` now returns a third value
    (`data_manifest_sha = data_identity(experiment_dir)`), threaded through both
    the backfill (`backfill_run_sidecar_provenance` gained the kwarg) and the
    synthesize-missing `write_run_sidecar` path. No manifest → field absent →
    byte-identical sidecar (`only-write-non-None`).
  - **Leg 2** — the wire `SampleIdentity` gained optional `data_sha` (additive,
    `None` default; v1 records parse). `verify_reproduction` lifts the repro's
    `data_manifest_sha`, stamps it on the appended sample's identity, and passes
    `data_identity=` to `reduce_envelope` (no longer `None` when known) so a
    cross-data prior is EXCLUDED + disclosed (`excluded_data_drift`); an absent
    leg is `data_identity_unknown`. Both surface on the v2 receipt's
    `data_identity` block + the reason. The double-canary mint stamps the leg
    too. **Pinned wiring choice:** the store-layer read (`load_evidence` /
    `partition_current_identity`) is fed the CODE-only identity so the kernel's
    `reduce_envelope` data leg is the AUTHORITATIVE exclusion+disclosure (feeding
    a data-carrying identity to the store would pre-strip cross-data samples as
    plain stale, losing the `excluded_data_drift` count).
  - **Leg 3** — `reproduce_run`'s guard grows to three dimensions, but data is a
    NAMED DISCLOSURE, not a refusal (the pinned honest reading: reproducing under
    a rebuilt input is a legitimate reproduction; verify names data as the moved
    dimension). `_data_drift_disclosure` compares recorded vs current data
    identity → `{status: match|drifted|unknown, recorded, current}` on the brief
    + a reason phrase. Param/code drift still REFUSE (unchanged).
  - **Schema debt (no regen run this branch):** the wire `SampleIdentity`
    gained `data_sha` and the run sidecar gained `data_manifest_sha` —
    `bake_operations_json.py --write` + schema regen owe an update. Additive +
    optional, so existing baked schemas + v1 records still parse.

## Ruling record (2026-07-10 user, recorded from session): bounded auto-prune

Remote extras that are MANIFEST-KNOWN (files the manifest names as ours,
present remotely but no longer in the deploy set) may be auto-pruned under a
disclosed bound; anything NOT manifest-known is an anomaly → ASK, never
delete. Spec + build = post-run-#12 batch item 6.

**SHIPPED (2026-07-10).** The pure planner is
`ops/transfer/prune.py::plan_prune` (→ `PrunePlan`): it splits the manifest
delta's `extra` into `prunable` (path recorded in the PRIOR push manifest —
proven ours) vs `anomalies` (never shipped by us — NEVER deleted, surfaced),
and REFUSES the whole plan (`to_prune == ()`) when the manifest-known set
breaches either conservative cap. Chosen defaults: **`DEFAULT_PRUNE_MAX_FILES
= 100`**, **`DEFAULT_PRUNE_MAX_BYTES = 100 MiB`**, overridable at the transport
call site via `HPC_DEPLOY_PRUNE_MAX_FILES` / `HPC_DEPLOY_PRUNE_MAX_BYTES`;
kill-switch `HPC_NO_DEPLOY_PRUNE=1`. Wired into `infra/transport.py`'s
rsync-less delta push (`rsync_push`, the `delete=True` branch that already
holds the dial — zero new cold SSH): after the additive delta ship,
`_prune_manifest_known_extras` reads the prior push manifest
(`_read_prior_push_manifest`, `.hpc/.push_manifest.json`), plans, discloses
(`_disclose_prune`), JOURNALS each prune (what / `reason: manifest-known` /
`old_sha256`) to `<experiment>/.hpc/deploy_prune.jsonl`, then deletes exactly
`to_prune` via one bounded ssh `rm` (`_execute_prune`); `_write_push_manifest`
then records the new shipped set for the next push. The prune is fully
fail-open — it can never break a successful transfer. A first push (no prior
manifest) routes every extra to the anomaly branch (nothing proven ours →
nothing pruned). Tests: `tests/ops/transfer/test_prune.py` (8, the planner)
and `tests/infra/test_transport_prune.py` (5, the seam: manifest-known
journaled+pruned, anomaly never-pruned+surfaced, over-bound refused+journaled,
kill-switch, own-bookkeeping-filtered).
