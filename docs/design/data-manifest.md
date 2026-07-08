# The data manifest — rung 0 of the onboarding map

**Status: PLANNED, USER-RULED (2026-07-07, ruling 0a/0b).** Companion to
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
  sha computed via the canonical-JSON helper (P-S1; file-content shas are
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
   brief.
3. **Audit sections** assert against it caller-side (the template's
   `universe-and-alignment` prints shas today; the manifest is its
   comparison target).

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

Format-aware validation ("top-level parquets must be bar-keyed") is PACK
content forever: caller-executed, receipt-producing, binding to the
manifest's identity substrate.

## Sequencing

- The verb + brief disclosure: self-contained; land post-run-#10, before
  or alongside Phase 1 (registry 142→143).
- The sidecar echo + fingerprint amendment: INSIDE Phase 3 (hot-file
  serialization with `submit_flow`).
- The `manifest-current` prerequisite: Phase 2's vocabulary, one row.

## Drift log

- 2026-07-07: written (Fable, pre-deadline), rulings 0a/0b folded.
