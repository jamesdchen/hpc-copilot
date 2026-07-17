# The reproducibility program — the definitive capability map + build queue

2026-07-17 · baseline `main` (clean tree). **Program-scoping unit** — the sole
file this unit writes is this memo. No `src/**` change, no commit. Concurrent
sessions are editing `reconcile`/`jobmap`/`backends`+`faultinject`, the
`provenance-manifest` area, and `agent_assets`/`harness_capabilities`/`cli-setup`;
this memo READS everywhere and flags coordination points. Cite `path::symbol`;
where this doc and the code disagree, the code and its enforcement-mapped tests
win.

## The directive this program answers (user, 2026-07-17, verbatim intent)

> "I'm more interested in how the repo contributes to solving the reproducibility
> crisis in modern science. How do we improve the repo's capabilities of doing
> that — how do we capture a clean reproduction from the messy process of
> experimentation? Are most current y's enough?"

Bounding rulings folded in: the attestation kernel's **human-sign-off ceremony
half is DEPRIORITIZED** (individuals keep journals; multi-human ceremony is not
the focus) — but the **recompute-lock half** (sha-bound receipts, drift
revocation) IS reproducibility machinery and stays. Bare-`y`/amplification
stands — no new gates, disclosure over refusal. Sole consumer is Claude Code —
compat ceremony can be reckless. MCP is projection.

## The standard we audit against

> **A stranger, given the experiment repo + the journal + cluster artifacts,
> mechanically re-derives the citable table and every number in it — no
> archaeology, no asking the original human.**

Every link below is classed **MECHANICAL** (code enforces it — enforcing
code/receipt cited), **DILIGENCE** (works only if the human did the right thing —
failure mode named), or **ABSENT** (no capture exists). The audit was run against
the canonical `src/hpc_agent/` tree (build/worktree copies ignored) by four
parallel code walks; every citation is `path::symbol` with the module the reader
can open.

**Headline finding.** Since the scoping doc
(`docs/plans/clean-reproduction-extraction-2026-07-17.md`) was written this
morning, its whole `#1`/`#2`/`R3`/Task-1 queue has LANDED: `extract-recipe`,
`settle-aggregate`, the reduce-time `contributing_run_ids` provenance, the signed
wheel-sha (manifest schema v2), and the run-13 stale-cache guard are all now in
`src/`. The **curation/selection and reduction links — historically the weakest —
are now the STRONGEST**. The two links that remain the classic reproducibility-
crisis failures — **INPUT DATA** and **ENVIRONMENT** — are exactly where the chain
is still DILIGENCE-or-ABSENT, and they are Wave 1.

---

## The chain, link by link

### 1. INPUT DATA — *the classic crisis link; still DILIGENCE-gated*

Two independent data fingerprints, both stamped on every run sidecar, computed by
two modules that do not agree on what "data identity" means:

| Field | Symbol | Hashes | Class |
|---|---|---|---|
| `data_sha` | `state/run_sha.py::compute_data_sha` (L213-270) | the run's **declared `input_datasets`** paths — DVC `outs[0].md5`, else raw file bytes, else the sentinel `"absent"`; sorted, joined, hashed | MECHANICAL compute |
| `data_manifest_sha` | `state/data_manifest.py::data_identity` (L165-191) → `manifest_doc_sha` | canonical sha over `{relpath:{sha256,size}}` of every file under the interview-declared **`input_roots`** | MECHANICAL compute |

Both flow through `state/runs.py::write_run_sidecar` (fields L440/L445) and are
backfilled additively at submit (`ops/submit_flow.py::_spec_provenance` L1118-1123).
Hashing is **by content** (raw bytes; DVC md5 is content by reference).

**The gap: capture is opt-in, and the default is uncaptured.** `data_sha` is
`None` unless the submit spec declared `input_datasets`; `data_manifest_sha` is
`None` unless a human (i) declared `audited_source.input_roots` in `interview.json`
**and** (ii) ran the `data-manifest` verb to mint `.hpc/data_manifest.json`. A run
that declares neither writes a **byte-identical sidecar with both data fields
`null`** and is **silently invisible to all data-drift attribution** — no warning
at submit. This is precisely the quiet-corruption class the manifest was built for
(`docs/design/data-manifest.md`: same filename, silently rebuilt bytes, every
downstream number subtly wrong, nothing ever throwing).

**When it IS declared, drift is caught — as DISCLOSURE, never a block.**
`reproduce_run.py::_data_drift_disclosure` (L265-291) compares recorded vs current
`data_manifest_sha` → `match`/`drifted`/`unknown`, surfaced on the greenlight brief
(L563-571), never blocking. `verify_reproduction.py` (L1172-1196) folds it into the
fingerprint envelope: a cross-data prior is **excluded** (`excluded_data_drift`),
an absent leg disclosed (`data_identity_unknown`). So: **MECHANICAL disclosure
gated on a DILIGENCE precondition.**

- **Class: DILIGENCE** (the human must declare inputs + mint a manifest; default is
  silent-null). Mechanical once declared, but never enforced.

### 2. CODE — *fully mechanical, one legacy hole*

`cmd_sha` (parameter identity, `#207` — does NOT fold executor/tasks bytes),
`tasks_py_sha` (code bytes), `executor`, the per-task `.hpc_cmd_sha` staleness
markers, and the deploy content-hashing + stage-swap. `reproduce-run` **REFUSES**
on both param and code drift: `_assert_no_drift` (`reproduce_run.py` L202-262)
recomputes `cmd_sha` and routes `state/code_drift.py::detect_code_drift` over
`executor`/`tasks_py_sha`; either mismatch → `SpecInvalid` naming the evidence and
the first differing task index. Deploy content-hashing caches by `(relpath, size,
mtime_ns)→sha` and ships a delta.

- **Class: MECHANICAL** (the one axis science already had via git, extended to
  params + executor). **One DILIGENCE hole:** `detect_code_drift` (L68-75) treats
  an empty/absent recorded value as NOT-drift ("cannot prove a pre-`#351` record
  changed"), so reproducing an original whose sidecar predates the
  `executor`/`tasks_py_sha` stamping passes the code-drift guard **vacuously** — a
  real edit against a legacy sidecar is not caught.

### 3. ENVIRONMENT — *the second crisis link; captured weakly, compared not at all*

| What | Symbol | Class |
|---|---|---|
| wheel (`hpc_agent_version`) | `ops/write_run_sidecar.py` L193; **signed** in `ops/provenance_manifest.py` L71 (manifest schema **v2**, R3 landed) | MECHANICAL |
| `env_hash` | `state/run_sha.py::compute_env_hash` L273-312 | MECHANICAL compute, **weak identity** |
| full package env (numpy/pandas/… versions, lockfile) | — | **ABSENT** |
| `env_python` / interpreter path / `sys.version` | — (zero hits in `src/`) | **ABSENT** |
| cluster name | sidecar field; `state/determinism.py::IDENTITY_FIELDS` L139 (NOT in it) | MECHANICAL capture, **coverage-only** |
| node / hostname / SKU / scheduler / hardware | — (only the `same_submission` n=2 caveat, `verify_reproduction.py` L752) | **ABSENT** |

`env_hash` hashes only the **activation directive** — `module load` names + the
conda env **name** + the source-script path + the `uv`/unset runtime selector. Two
materially different environments sharing one conda-env *name* produce the **same**
`env_hash`. Nothing captures a `pip freeze`/conda-export/lockfile; the
`compute_env_hash` `extra` hook *could* carry measured versions but no caller
populates it.

**The decisive gap: the captured `env_hash` is never compared in any gate.**
`reproduce_run._assert_no_drift` checks param + code only — no env leg. The
fingerprint identity filter (`determinism.py::IDENTITY_FIELDS` L139) excludes
`env_hash`, so a changed environment does **not** invalidate a reused envelope.
Every `env_hash` occurrence in `ops/` is a write, a docstring, or a verbatim
receipt echo — **none is a comparison.** So environment drift between an original
and its reproduction is the **opposite posture to data drift**: data is disclosed,
code/param refuse, **env is neither refused nor disclosed — it is invisible.**

- **Class: ABSENT** for full-env + interpreter + hardware; **DILIGENCE-grade
  fidelity** for `env_hash`/cluster (captured, but weak and never compared). This
  is the single largest silent hole in the chain.

### 4. EXECUTION — *mechanical, with the un-fakeable fingerprint*

Journal records, cluster-side announce markers
(`ops/monitor/announce.py`), the harvest receipt ledger
(`ops/monitor/harvest_guard.py::harvest_on_terminal`), and the **double canary**
(n=2 determinism-fingerprint sample). The fingerprint:

- **Sample shape** — `state/determinism.py::build_sample_record` (L380-428): one
  definition. Identity legs `IDENTITY_FIELDS = (cmd_sha, tasks_py_sha, executor)` +
  optional `data_sha`; evidence axes `source` (double-canary / verify-reproduction),
  `scale` (canary/main), `cluster`, `same_submission`, `partial`, `n`.
- **Written** append-only to `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl`
  (`state/fingerprint_store.py::fingerprint_path` L167-176) — keyed on cmd_sha, so
  an original's double-canary and every later reproduction accrete to one ledger.
- **Double canary** — `ops/submit_and_verify.py` fires `<run>-canary` +
  `<run>-canary2` concurrently (L490-535), verifies both, diffs their task-0
  metrics, appends the n=2 prior (`_mint_double_canary_sample` L415-488). A failed
  second canary **blocks the main array** exactly like a failed first (L582-600).
- **Envelope** — `reduce_envelope` (L783-812) reduces prior admitted samples to
  per-key **order statistics** (`lo=min, hi=max, rel_spread`) — no mean, no stddev,
  no invented epsilon. `classify` routes deviations to `auto_cleared` /
  `needs_verdict` / `mismatch` / `incomparable`.
- **The un-fakeable leg** — `append_sample` bind-locks each sample's `content_sha`
  against the two on-disk payloads via `attestation.bind` (L307): a spread cannot
  be asserted over payloads that were never on disk.

- **Class: MECHANICAL.** **DILIGENCE residue:** admitting a `needs_verdict`/
  `mismatch` sample into the envelope needs a human `reproduction-verdict` naming
  the already-locked `content_sha` token-exact (`fingerprint_store::_is_admitted`
  L399-443; `ops/decision/journal/reproduction.py` naming bar L22-60) — a human who
  accepts a genuine nondeterminism launders it, guarded only by the naming bar.
  Double-canary minting is best-effort and disable-able (`HPC_NO_DOUBLE_CANARY`),
  so the fingerprint can silently not grow.

### 5. REDUCTION — *now strong; one cluster-path residual*

- **Reduce-time provenance (Task 1, LANDED).** `aggregate_flow.py::_persist_local_aggregate`
  (L1382-1394) now writes `contributing_run_ids` + `piece_cmd_shas` +
  `hpc_agent_version` alongside `{incomplete_waves, source, reduced_at}`, sourced by
  `_reduce_input_provenance` (L1229-1333) from the reduce's own `_combiner/wave_*.json`
  partials + `_per_task_results/.hpc_cmd_sha`. **The table→run-set link is now
  first-class for the local path.**
- **Combine-cache sha guard (run-13 finding 13-addendum, LANDED).** The per-task
  mirror cache is now fingerprint-checked: `_mirror_piece_fingerprints` snapshots
  pre-pull `.hpc_cmd_sha` (L666), `_evict_stale_mirror_pieces` (L191-216) evicts +
  re-pulls any task dir whose sha moved and invalidates its wave partial. The
  fingerprint that "existed on disk but was never compared" **is now compared.**
- **Canary reducer-check (rung 2, LANDED).** `submit_and_verify.py::_check_reducer_on_canary`
  (L113-211) EXECUTES the declared `aggregate_cmd` via the same `cluster_reduce` the
  final harvest uses, against the verified canary's real row, before the array
  launches — asserts contract SHAPE only, discloses any error verbatim, never
  refuses (`docs/plans/amortized-reduction-check-2026-07-17.md`).
- **Streaming labels.** `ops/aggregate/stream.py` marks per-arm values
  `per-arm-final` and cross-arm stats `final-harvest-only`, every emission carries a
  `completeness_label`.

- **Class: MECHANICAL** (cluster leg closed 2026-07-17, U-RED1). The cluster
  combiner footer (`execution/mapreduce/combiner.py::_final_reduce`) now mirrors the
  SAME three reduce-time fields the LOCAL path stamps —
  `contributing_run_ids` (the run-scoped partial membership it consumed, F05-filtered),
  `piece_cmd_shas` (the run's sidecar cmd_sha — the combiner pre-reduces cluster-side,
  so its consumed wave partials carry no per-piece sha, exactly the local combiner-wave
  fallback), and `hpc_agent_version` (the deployed sidecar's wheel) — plus
  `source: "cluster_final"`. On the `HPC_CLUSTER_FINAL_REDUCE` path `extract-recipe` now
  reads a first-class table→run-set link instead of degrading to lineage (§6). The
  local reader `aggregate_flow::_read_reduce_provenance` PREFERS the cluster footer and
  discloses the source; an old footer (a combiner predating the mirror) reads
  not-captured, disclosed, never a wrong answer.

### 6. SELECTION / CURATION — *the messy→clean step; now mechanical*

- **`extract-recipe` (BUILT).** `ops/extract_recipe.py::extract_recipe` (L406-483) +
  `docs/primitives/extract-recipe.md`. A read-only query that walks a citable
  artifact (`run_id` / `campaign_id` / `aggregate_path`) back to the **minimal
  contributing run-set**, applying three disclosed+counted exclusions in order —
  **canary** (`canary_parent_of`), **superseded** (non-head of `lineage_chain`),
  **dead-end** (no `harvest_receipt_exists`) — then emits per-run fingerprints
  (incl. wheel sha, preferring the signed v2 manifest), a `recipe_signature` over
  only the minimal set, runnable re-derivation steps, the receipts chain, and every
  G4 gap DISCLOSED. Never names a metric, never picks a "best" run.
- **Supersession.** `ops/supersession.py::supersede_run` (L201+) + `state/scopes.py::lineage_chain`
  (L294-308) — first-class, canary-paired, kill-routed.
- **`settle-run` + `settle-aggregate` (BOTH BUILT).** `settle_run.py` (L87-231)
  settles a single run's terminal state with directed evidence; `settle_aggregate.py`
  (L119-243) is the **run-13 finding-14 close**: an operator-bypass TABLE gets a
  provenance home — artifact sha256 computed at record time, named runs validated,
  a synthesized utterance refused, journaled `source:"operator-settled, provenance
  human-asserted"`. It **RECORDS, never GATES; the numbers are never blessed.**

- **Class: MECHANICAL.** **DILIGENCE edge (G2, disclosed):** the dead-end
  discriminator is the local harvest-receipt ledger; for a **pre-Task-1 table** with
  no `contributing_run_ids`, `extract-recipe` falls back to `lineage_chain` and the
  dead-end/contributing ambiguity re-opens — disclosed as the
  `table-run-set-link-absent` (G4a) gap, not silently masked. The cluster-reduce
  path (§5 residual) triggers exactly this fallback.

### 7. ANALYSIS / PRESENTATION — *bound to the sealed dossier; then it stops*

- **The recompute-lock (the reproducibility half of the attestation kernel, KEPT
  per the ruling).** `state/attestation.py`: `bind` (L169-200) recomputes-and-refuses
  — "a hash cannot be asserted into existence"; `reduce` (L203-241) is drift
  revocation, `CURRENT`/`STALE`/`ABSENT` newest-wins — "an edit revokes stale
  trust." Every trusted record rides these.
- **Notebook output ↔ code.** `state/notebook_audit.py::record_render_receipt`
  (L542) binds a render receipt to the section sha; it reads **STALE the instant the
  section drifts** (`read_render_receipts` L587-593). The trusted-display render lock
  (`ops/notebook/render_store.py::write_render`, enforced at
  `ops/decision/journal/signoff.py::_assert_signoff_render_current`) makes the
  relayed view a code-written, content-addressed artifact, not model-carried bytes.
  **DILIGENCE seam:** the receipt's `output_sha` is caller-attested and **never
  recomputed by core** — core verifies the receipt is bound to current code, not
  that the emitter actually executed (`_wire/actions/notebook_record_receipt.py`
  L41-51).
- **`verify-relay`** (`ops/decision/journal/verify_relay.py::verify_relay`) audits
  every LLM-relayed number against a run's corpus (`_load_run_sources` L1113-1174:
  journal + sidecar + record + briefs + `metrics_aggregate.json`/`wave_*.json`/`*.csv`
  + campaign briefs), one grammar shared by the verb and the Stop hook. The run-13
  false-positive floods (date fragments, unit-suffixed `du -sh` figures, per-token
  corpus) are now carved out **with accounting** — MECHANICAL audit, DILIGENCE-grade
  precision (each carve-out shipped after a real flood).

**Where the mechanical chain STOPS.** The terminal bound artifact is the sealed
dossier (`ops/export_dossier.py` — sidecar + journal + briefs + `_aggregated` bytes +
the fingerprint ledger, copied verbatim, **never parsed**, integrity-manifested). A
repo-wide search for LaTeX/`\cite`/manuscript emission returns **no code**: the step
"a human reads the reducer's number off the sealed dossier and types it into the
figure caption / `\num{}`" is **purely human and entirely unbound** — no
attestation, no recompute, no relay audit reaches into the manuscript.

- **Class: MECHANICAL up to the dossier; the number→paper transcription is
  ABSENT.** This is the last unprotected link.

### 8. THE REPRODUCTION ACT — *can a stranger run it?*

- **`reproduce-run`: NO without a recorded original.** It reads the original's
  sidecar by `original_run_id` (L405-414); no sidecar → `SpecInvalid`.
- **`verify-reproduction`, recorded-original mode: NO.** Needs both sidecars and the
  reproduction's `reproduces` link to name the original (L1025-1032).
- **`verify-reproduction`, external-baseline / claim-check mode: YES — this IS the
  stranger path.** `_run_claim_check` (L852-936) needs only a fresh observed run's
  sidecar + a human-authored claim; no recorded original. **But it PROVES nothing
  about the original claim** — it asserts *consistency with a fresh observed run
  under caller tolerance*, never "reproduced." The anti-laundering seam
  `_assert_receipt_kind_matches_baseline` (L778-802) refuses by construction to call
  a claim-match a reproduction: "an external claim was never observed." The n=2
  fingerprint samples come from the fresh double-canary, **never from the claim.**

- **Class: MECHANICAL** (the input requirements + the observed-only sample lock +
  the anti-laundering split are all code-enforced). A stranger CAN check a claim
  against their own fresh run; they cannot fabricate a reproduction of a run they
  never observed.

### 9. THE Y'S — *what each consent record contributes to reproduction*

Directly answering "are most current y's enough?":

| Consent record | Symbol | CONTRIBUTES | CANNOT contribute |
|---|---|---|---|
| greenlight (`response=="y"`) | `state/decision_journal.py::append_decision` L257-268 | authorization lineage — *whose `y`, at which boundary, why the run took its shape* | fingerprints **nothing** — `evidence_digest`/`resolved` are opaque; no sha of data/code/env |
| settle provenance | `settle_aggregate.py` L204-227 | human-asserted derives-from + the artifact's byte-sha256 | any verification the numbers are correct — "**never blessed**" |
| sign-off shas | `state/notebook_audit.py` sign-off (`section_sha`/`view_sha`) | the `bind` recompute-lock on the section (the *machinery*, §7) | the `y` itself adds authorship, not a fingerprint |
| reproduction-verdict | `reproduction.py` L63-224 + `_is_admitted` L399-443 | admission authorization for a sample **already** bind-locked | the fingerprint — `content_sha` was locked at `append_sample`, *before* any verdict |

**The plain answer: more consent ceremony would change reproducibility NOT AT
ALL.** Provenance lives entirely in the bind-locked shas
(`_CODE_IDENTITY_FIELDS`, `data_sha`, the fingerprint `content_sha`), all minted
with `attestor:"code"` and **no human** — `_is_admitted` admits every
`double-canary` sample with zero human involvement (L417-418). A greenlight is
authorization lineage (a genuine and useful axis — *decisions* in the five-axis
model), categorically not provenance. **The `y`'s are exactly enough for what they
do; the reproducibility gaps (§1, §3) are in machine data/env capture, not in
consent — so the ruling to deprioritize the human-ceremony half is correct, and
effort saved there should go to Wave 1.**

---

## (a) The DILIGENCE / ABSENT links, ranked by damage × frequency

| Rank | Link | Class | Damage | Frequency | Why |
|---|---|---|---|---|---|
| **1** | **INPUT DATA capture opt-in** (§1) | DILIGENCE | Highest | High (default undeclared) | Data is *the* classic crisis link; an undeclared run is silently invisible to all data-drift attribution, no warning |
| **2** | **ENVIRONMENT drift invisible** (§3) | ABSENT + never-compared | Highest | High (every reproduction) | Full package env + interpreter ABSENT; `env_hash` captured but never compared; a mutated conda-env-under-same-name or a version bump reproduces "clean" |
| **3** | **number → paper transcription** (§7) | ABSENT | High | High (every paper) | The chain seals the number but does not follow it into the manuscript; the actual citable digit is hand-typed, unaudited |
| **4** | **cluster combiner footer mirror** (§5) | ~~ABSENT~~ **MECHANICAL** (closed 2026-07-17, U-RED1) | Medium-High | Medium (cluster-reduce path) | ~~`HPC_CLUSTER_FINAL_REDUCE=1` publishes a table with no `contributing_run_ids`; `extract-recipe` degrades to lineage~~ — the cluster `--final` footer now mirrors `contributing_run_ids`/`piece_cmd_shas`/`hpc_agent_version`+`source`, at parity with the local reduce; `extract-recipe` reads the real run-set on the cluster path |
| **5** | **hardware / scheduler variance** (§3) | ABSENT | Medium | Medium | GPU/SKU numeric variance is surfaced only as an n=2 `same_submission` caveat, never recorded — invisible cross-run |
| **6** | **legacy-sidecar code-drift vacuous pass** (§2) | DILIGENCE | Medium | Low (pre-`#351` records) | Reproducing an old original skips the code-drift leg |
| **7** | **notebook `output_sha` caller-attested** (§7) | DILIGENCE | Low-Med | notebook users only | Core binds the receipt to current code but trusts the emitter's output hash |

Gaps 1–3 are the program. 4–5 are Wave-2 finishing. 6–7 are known, disclosed, and
cheap.

## (b) The build program, in waves — disclosure not gate, lean on what exists

**House rules honored:** extend shipped verbs / compose shipped walks; render is
pure code; every unit is IDENTITY/ORDERING/COUNTING over opaque records; no new
gate, bare `y` always stands; the reckless-compat ruling means schema/version bumps
are cheap. Each unit lists `{spec, files, size, disclosure-not-gate check}`.

### Wave 1 — the two crisis links (data + env), plus the S1 nudge

**U-ENV1 — capture the full run environment, disclose env drift.** *The top build.*
The canary already executes in the run's env — have it emit a resolved-environment
snapshot (`pip freeze` / conda-export / lockfile) and fold its sha into a new
additive sidecar field `env_lock_sha` (and optionally into `compute_env_hash`'s
existing `extra` hook). Then add `reproduce_run._env_drift_disclosure` mirroring
`_data_drift_disclosure` (§1), and an env leg in `verify_reproduction` exactly like
the data leg — a cross-env prior **disclosed**, never a refusal.
- *Files:* `ops/submit_and_verify.py` (canary emits snapshot — rides the existing
  detached canary leg, ~0 wall-clock), `state/runs.py` + `ops/submit_flow.py::_spec_provenance`
  (additive `env_lock_sha`, only-write-non-None → byte-identical when absent),
  `state/run_sha.py` (optional `extra` fold), `ops/reproduce_run.py`
  (`_env_drift_disclosure` + brief phrase), `ops/verify_reproduction.py` (env leg +
  receipt block). Coordinate with the concurrent `provenance-manifest` session
  (whether `env_lock_sha` joins the signed manifest — recommend yes, another cheap
  schema bump).
- *Size:* MEDIUM. *Disclosure-not-gate:* ✓ — env drift becomes a named DISCLOSED
  dimension (like data), never a block; a contract test asserts the disclosure path
  has no raise/gate branch (the data-manifest never-blocking pin).

**U-ENV2 — compare the `env_hash` we already capture.** Even before U-ENV1 lands,
`env_hash` is captured and never compared. Add the same `_env_drift_disclosure`
over the existing `env_hash` so an activation-directive change is at least
disclosed at reproduce/verify. Subsumed by U-ENV1's disclosure surface if that
lands first; ship standalone if U-ENV1 slips.
- *Files:* `ops/reproduce_run.py`, `ops/verify_reproduction.py`. *Size:* SMALL.
  *Disclosure-not-gate:* ✓.

**U-DATA1 — the uncaptured-data nudge at S1.** At submit resolve, if neither
`input_datasets` nor `input_roots` is declared, fold a NEVER-blocking standing
disclosure into the S1 brief: "data identity uncaptured — this run is invisible to
data-drift attribution; declare input_roots + mint data-manifest to enable it."
Mirrors the shipped dirty-worktree disclosure (`resolve_submit_inputs._dirty_worktree_disclosure`).
Raises the diligence floor without a gate.
- *Files:* `ops/resolve_submit_inputs.py` / `ops/submit_blocks.py`. *Size:* SMALL.
  *Disclosure-not-gate:* ✓ — one standing line, tier-mapped (never per-run repeat),
  bare `y` stands.

### Wave 2 — finish the freshly-built extraction to full coverage

**U-RED1 — mirror Task-1 provenance into the cluster combiner footer. BUILT
2026-07-17.** Stamps `contributing_run_ids`/`piece_cmd_shas`/`hpc_agent_version` +
`source: "cluster_final"` in `execution/mapreduce/combiner.py::_final_reduce` so the
`HPC_CLUSTER_FINAL_REDUCE` path is at parity with the local reduce; `extract-recipe`
stops degrading to lineage there (closes gap 4, the G4a cluster leg). The local
reader `aggregate_flow::_read_reduce_provenance` PREFERS the cluster footer, discloses
the source, and falls back to the local `_reduce_input_provenance` when no footer was
persisted; an old footer reads not-captured (disclosed, never a wrong answer).
- *Files:* `execution/mapreduce/combiner.py` (footer mirror) + `ops/aggregate_flow.py`
  (`_read_reduce_provenance` reader + a cluster-final disclosure). *Size:* SMALL.
  *Disclosure-not-gate:* ✓ — additive footer fields + a disclosure line, no gate.

**U-HW1 — capture hardware/scheduler as a disclosed coverage axis.** Record the
scheduler-reported exec node/hostname (already in scheduler artifacts) into the
sidecar or the fingerprint sample as **evidence/coverage, not identity** (like
`cluster`), so hardware variance is *attributed* rather than surfaced only as the
n=2 `same_submission` caveat. Recommend coverage-not-identity so benign node moves
never poison an envelope.
- *Files:* sidecar field + `ops/monitor/announce.py` or the canary mint;
  `state/determinism.py` sample evidence axis (NOT `IDENTITY_FIELDS`). *Size:*
  MEDIUM. *Disclosure-not-gate:* ✓.

### Wave 3 — extend the mechanical chain toward the paper

**U-PUB1 — recipe-in-dossier + a `cite-check` surface.** The number→paper link
(gap 3). Can't follow into LaTeX, but can bring the audit to the manuscript's door:
(a) seal the `extract-recipe` recipe INSIDE the dossier (scoping-doc proposal #3 — a
`recipe` store noun, a string add per the dossier extensibility ruling), so a
reviewer with only the zip re-derives + re-checks the signature; (b) a `cite-check`
query that takes a claimed number + the sealed dossier/`aggregate_path` and audits
it against the reducer's bytes using the `verify-relay` corpus machinery — the human
pastes their table row, code confirms it matches the sealed number (or names the
nearest source value). Not MCP-curated (the `extract-recipe`/`trace` precedent).
- *Files:* `ops/export_dossier.py` (recipe file), NEW `ops/cite_check.py` (rides
  `verify_relay._load_run_sources` + `_collect_source_numbers`), wire + render.
  *Size:* MEDIUM. *Disclosure-not-gate:* ✓ — reports match/nearest-value, never
  blocks a paper.

## (c) Rulings needed (recommendation each)

- **RR1 — env-drift posture: disclose, not refuse?** *Recommend DISCLOSE* (mirror
  data; reproducing under an upgraded env is a legitimate reproduction whose moved
  dimension must be named). Refusing on env would break the bare-`y` doctrine.
- **RR2 — capture full `pip freeze`/conda-export by default?** *Recommend YES,
  best-effort, disable-able* (`HPC_NO_ENV_LOCK=1`, the `HPC_NO_DOUBLE_CANARY`
  precedent). It rides the canary leg at ~0 wall-clock and closes the largest silent
  gap; a per-submit opt-in's cost case is weak.
- **RR3 — hardware node: identity or coverage?** *Recommend COVERAGE* (like
  `cluster`) — disclosed, folded into `_well_evidenced`, never an identity filter, so
  a benign node reassignment does not read as nondeterminism.
- **RR4 — does `env_lock_sha` join the signed provenance manifest?** *Recommend YES*
  — another cheap schema bump (v2→v3) under the reckless-compat ruling; coordinate
  with the live provenance-manifest session.
- **RR5 — `cite-check` curation.** *Recommend NOT MCP-curated*, matching
  `extract-recipe` (operator/reviewer projection; revisit if an agent hand-rolls the
  walk).

## (d) Thesis — what this repo's contribution to the crisis IS

The reproducibility crisis is, mechanically, an **archaeology problem**:
"which code, which data, which environment, which runs produced this number?" is
reconstructed from memory and vibes — and narrative reconstruction is exactly where
both humans and LLMs confabulate. This repo's distinctive move is the product
one-liner (`docs/design/onboarding-map.md`) applied at *publication time*:

> **"What changed since last-known-good" — answered mechanically instead of by
> archaeology.**

It records a last-known-good on all five axes git only ever gave science on one —
**code** (git), **data** (the manifest), **behavior** (the determinism
fingerprint's envelope), **beliefs** (registrations), **decisions** (the journal)
— and makes the diff mechanical. Three properties make the claim distinctive:

1. **The reducer — never the LLM — computes every citable number**, and the number
   is byte-sealed in the dossier; the LLM only *points* the human at a code render,
   the human *concludes*, and `verify-relay` audits any number the LLM relays.
2. **The messy→clean extraction is a first-class walk, not a memory act.**
   `extract-recipe` turns "which runs are THE result" into IDENTITY + ORDERING +
   COUNTING over opaque records — dead ends, canary siblings, superseded lineage,
   and operator-bypass tables all mechanically excluded-and-disclosed, never named
   or judged.
3. **Reproducibility is carried by code-minted bind-locked shas, independent of
   consent** — a hash cannot be asserted into existence, and an edit revokes stale
   trust. Human `y`'s are authorization lineage, orthogonal to provenance.

**What it can claim publicly TODAY, honestly:** *every citable number is
reducer-computed and byte-sealed; the minimal run-set that produced it is a
first-class, signature-verified, gap-disclosing recipe; determinism is fingerprinted
under an un-fakeable recompute-lock; the reproduction act refuses to launder an
unobserved claim into the trust chain.* The chain is complete for **code**, and for
**data/environment only when the scientist opted in.** The last unbound step is a
human typing the sealed number into the paper.

**What it should claim once Wave 1 closes:** *A stranger, given the experiment repo
+ the journal + cluster artifacts, mechanically re-derives the citable table and
every number in it — with code, data, AND environment identity fingerprinted and
drift-attributed by default — no archaeology, no asking the original human.* Wave 1
is what turns "the strongest reproduction tooling for the axis science already had
(code)" into "the first tool that makes data + environment reproducible **by
default, mechanically, at publication time**" — which is the crisis's actual center
of gravity.

---

## Drift log

**2026-07-17 — created (program-scoping only).** Cites the user directive
(verbatim above): "how the repo contributes to solving the reproducibility crisis
… how do we capture a clean reproduction from the messy process of experimentation
… are most current y's enough?" Full end-to-end audit run against the canonical
`src/hpc_agent/` tree by four parallel code walks (input-data/env,
reproduction/fingerprint, reduction/selection, analysis/relay/consent); every link
classed MECHANICAL / DILIGENCE / ABSENT with `path::symbol` citations. Established
against the tree that the scoping doc's (`clean-reproduction-extraction-2026-07-17.md`)
entire #1/#2/R3/Task-1 queue has LANDED — `extract-recipe`, `settle-aggregate`, the
reduce-time `contributing_run_ids` provenance, the signed wheel-sha (manifest v2),
and the run-13 stale-cache guard are all in `src/` — moving the reduction and
curation links from weakest to strongest. The two links that remain the classic
reproducibility-crisis failures are **INPUT DATA** (§1, DILIGENCE — capture is
opt-in, default silent-null) and **ENVIRONMENT** (§3, ABSENT — full package env +
interpreter uncaptured, `env_hash` captured but never compared in any gate; env
drift between an original and its reproduction is invisible, the opposite posture to
data's disclosure and code's refusal). Ranked the DILIGENCE/ABSENT links by damage ×
frequency (data capture / env drift / number→paper transcription are the top three);
proposed a three-wave build program (Wave 1 = env capture + data nudge; Wave 2 =
cluster combiner mirror + hardware coverage; Wave 3 = recipe-in-dossier +
cite-check), every unit disclosure-not-gate per the bare-`y` ruling, leaning on
shipped verbs. Answered the y's question directly: consent records are authorization
lineage and contribute NOTHING to provenance — more `y`-ceremony changes
reproducibility not at all, so deprioritizing the human-sign-off half is correct and
the saved effort belongs in Wave 1. Five rulings flagged (RR1 env-disclose,
RR2 env-lock-by-default, RR3 hardware-as-coverage, RR4 env-lock-in-manifest,
RR5 cite-check-not-curated), recommendation on each. No `src/**` touched; no commit.
This doc is the canonical program home; the reproduction machinery it maps lives
across `ops/{reproduce_run,verify_reproduction,extract_recipe,settle_aggregate,
export_dossier}.py`, `state/{run_sha,data_manifest,determinism,fingerprint_store,
attestation}.py`, and `execution/mapreduce/`. Coordination points with the three
concurrent sessions (reconcile/mapreduce, provenance-manifest, agent-assets) are
named per unit.

- 2026-07-17 — **U-RED1 BUILT (gap #4 → MECHANICAL, the cluster leg closed).**
  `execution/mapreduce/combiner.py::_final_reduce` now stamps the SAME reduce-time
  provenance the LOCAL `_reduce_input_provenance` records into its footer:
  `contributing_run_ids` (the run-scoped wave-partial membership it consumed, unioned
  with `run_id`, foreign partials F05-dropped), `piece_cmd_shas` (the run's sidecar
  cmd_sha — the combiner pre-reduces cluster-side so its consumed inputs carry no
  per-piece sha, the local combiner-wave fallback), `hpc_agent_version` (the deployed
  `.hpc/runs/<run_id>.json` wheel), and `source: "cluster_final"` — all derived from
  the reduce's OWN consumed inputs at write time, best-effort (an absent sidecar
  degrades to `[]`/`None`, never failing the reduce). The local reader
  `aggregate_flow::_read_reduce_provenance` PREFERS the cluster footer's provenance and
  discloses the source (`cluster_final`), reads an old footer as `not-captured`
  (disclosed, never re-derived after the fact), and re-derives via
  `_reduce_input_provenance` only when NO footer was persisted (`source: "local"`);
  `_cluster_final_reduce` prints a one-line disclosure of whether the table→run-set
  link is first-class. `extract-recipe` over a cluster-reduced `metrics_aggregate.json`
  now reads real `contributing_run_ids` instead of the `table-run-set-link-absent`
  (G4a) lineage degrade — the gap-closing pin. Additive fields; verify-reproduction and
  every existing reader are byte-unaffected. `reduce/metrics.py` untouched (the local
  reduce path already carried the provenance via Task 1). Built in an isolated
  worktree.
- 2026-07-17 (integration) — **U-ENV1 BUILT**: canary emits a resolved-env
  snapshot (SOURCE_ORDER pip_freeze > lockfile > python_env, source tag folded
  into the sha, ack-gated fetch so truncation = could-not-capture never a wrong
  sha) → additive env_lock_sha/env_lock_status on the sidecar (backfill-only,
  old records read None); reproduce/verify DISCLOSE drift (env_identity receipt
  block + reason clause) and never gate — proven: drifted env + matching
  metrics leaves the verdict untouched. As-built detail in
  docs/design/determinism-fingerprint.md's drift log. The ENVIRONMENT link
  moves ABSENT/weak → DISCLOSED. Built in an isolated worktree, integrated by
  the coordinator.

- 2026-07-17 — **RR4 BUILT: env-lock joins the SIGNED provenance manifest**
  (schema v2→v3, the ruled-obvious follow-on cited above; MIRRORS the R3
  wheel-sha precedent at 008198ee exactly). A captured env claim held OUTSIDE
  the signature can be edited after the fact — the same hole R3 closed for the
  wheel sha. `env_lock_sha` AND `env_lock_status` join
  `provenance_manifest._RUN_PROVENANCE_FIELDS` (both belong under the signature:
  the signed `env_lock_status` is what makes an absent sha an HONEST signed
  `null` rather than a silent omission); `PROVENANCE_MANIFEST_SCHEMA_VERSION`
  bumps 2→3, `KNOWN_PROVENANCE_MANIFEST_SCHEMA_VERSIONS` → `{1, 2, 3}`.
  `verify_provenance_manifest` continues to verify v1 AND v2 manifests unchanged
  (the version-inside-the-signed-body discipline — re-hash the on-disk body AS
  WRITTEN, the signed `manifest_schema_version` names the field-set); a
  tampered/flipped `env_lock_sha` or a null-marker turned into a value breaks the
  signature. `extract-recipe`'s fingerprint now PREFERS the signed `env_lock_sha`
  over the sidecar (generalizing R3's `_signed_wheel` → `_signed_field` over
  `hpc_agent_version` + `env_lock_sha`) and discloses `env_lock_sha_source`
  (`signed-manifest` vs `sidecar`) per run, mirroring the wheel-sha disclosure;
  `env_lock_sha` joins the recipe/render/boundary identity fingerprint set. New
  targeted tests red-then-green: v3 carries+signs both env fields (tamper the sha
  OR the status → verify False), absent env capture signs the explicit null
  markers (smuggling either into a value → False), v1+v2 read-compat both verify,
  extract-recipe prefers-and-discloses the signed env over a drifted sidecar.
  Gates green: test_provenance_manifest + test_extract_recipe + test_trace +
  boundary + evidence + cli/aggregate (102 targeted), 26/26 lint gauntlet,
  regen --check green (build_schemas + bake_operations_json regenerated for the
  extract-recipe description/help edits — the manifest core owed no schema
  regen, per the R3 precedent), ruff/format/mypy clean. Built in an isolated
  worktree, integrated by the coordinator.
