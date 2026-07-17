# Sharpening clean-reproduction extraction — scoping + build-ready proposals

2026-07-17 · baseline `main @ 6d29e23b` (clean tree). **Scoping-only unit** — no
`src/**` change lands here; the deliverable is the inventory, the gap analysis,
and the ranked proposals. Cite `path::symbol`; where this doc and the code
disagree, the code and its enforcement-mapped tests win.

## The directive this doc scopes (user, 2026-07-17)

The system amplifies human effort — nothing refuses a bare `y`. "We provide the
journal as a tool for [humans to hold themselves to rigor] when they have the
energy, which means we should sharpen the feature that allows us to **extract the
clean reproduction of results after the messy process of experimentation**."

Concretely: a scientist finishes a messy multi-run exploration (dead ends,
retargets, parameter drift, superseded runs, an operator-bypass reduce) and wants
the **clean minimal recipe** — *which* run(s) produced the citable numbers, their
exact spec/env/wheel/data provenance, the mechanical steps to re-derive the table
from scratch, and the receipts chain proving the numbers came from those runs.
This is the product one-liner (`docs/design/onboarding-map.md`) — **"what changed
since last-known-good, answered mechanically instead of by archaeology"** — applied
at *publication time*, where "last-known-good" is the citable table and the
archaeology is grepping a journal of dead ends to reconstruct what actually made it.

---

## §1 — Inventory of the existing extraction / reproduction machinery

Every row: the verb/file, the question it answers **today**, its input, its emitted
record shape. Grouped forward chain → provenance surfaces → projections/exports →
reproduction → conclusions.

### 1.1 The forward chain (run → citable number) — where provenance is *minted*

| Stage | File / symbol | Mints (durable) |
|---|---|---|
| interview/spec | `ops/write_run_sidecar.py::write_run_sidecar` | the per-run **sidecar** `.hpc/runs/<run_id>.json`: `cmd_sha`, `tasks_py_sha`, `data_sha`, `data_manifest_sha`, `env_hash`, `cluster`, `profile`, **`hpc_agent_version`** (the wheel), `scopes`, `parent_run_ids`, `node_sha`, `wave_map`, `trial_params`, `reproduces?` |
| canary (×2) | submit-s2 double canary | first **determinism-fingerprint sample** → `_aggregated/_fingerprints/<cmd_sha[:16]>.jsonl` (`source:"double-canary"`, `scale:"canary"`, `same_submission:true`) |
| per-task done | `ops/monitor/announce.py` | cluster-side **announce markers** `<remote>/.hpc/announce/<run_id>/task_<id>.complete\|.failed` (lifecycle only); `read_announced_task_ids` → `AnnouncedTaskIds{present, done_ids}` |
| task promote | `execution/mapreduce/dispatch.py` (`PER_TASK_CMD_SHA_FILENAME`) | per-task-dir **`.hpc_cmd_sha`** staleness sidecar (= the submission `cmd_sha`) |
| terminal | `ops/monitor/harvest_guard.py::harvest_on_terminal` | **harvest receipt** `<run_id>.harvest.jsonl`: `{harvested_at, run_id, terminal_cause, metrics_harvested, aggregated_metric_keys, harvest_ok, ...}`; `harvest_receipt_exists` is the durable backstop trigger |
| reduce | `ops/aggregate_flow.py::_persist_local_aggregate` | the **reduced table** `_aggregated/<run_id>/metrics_aggregate.json` = `{aggregated_metrics, provenance:{incomplete_waves, source, reduced_at}}`; cluster path adds a footer + a manifest pointing at the `_combiner/wave_<N>.json` partials (each partial carries its own `run_id`; `_final_reduce` skips foreign-run partials, F05) |
| pack reduce | domain-pack `aggregate_cmd` | the **actually-cited artifact** (e.g. `causal_tune_linear/metrics_table.csv`) — a NON-json pack output; `per_task_fallback_reducible` (`aggregate_flow.py:305`) is FALSE for it |

### 1.2 Provenance surfaces (walk toward the run-set) — what EXISTS

| Verb | File / symbol | Question today | Input | Emits |
|---|---|---|---|---|
| **`trace`** | `ops/trace.py::trace` (query) | "explain exactly what produced this result, in what order" | **`campaign_id` XOR `run_id`** | a derived DAG: `run`+`wave` nodes with `project_run_provenance` fingerprints, `derived-from` edges (via sidecar `parent_run_ids`), `member`/`contains` edges, a campaign `signature`. `--format dag/flat/dot` |
| **`provenance-manifest`** | `ops/provenance_manifest.py` | "attest these results were produced by exactly these {code,data,env,params}" | **`campaign_id`** | `{manifest_schema_version, campaign_id, run_count, runs:[{run_id, cmd_sha, tasks_py_sha, data_sha, env_hash, cluster, profile, submitted_at, trial_tokens}]}` + `manifest_signature` (sorted-keys SHA-256). **`_RUN_PROVENANCE_FIELDS` OMITS `hpc_agent_version`** |
| **`settle-run`** | `ops/settle_run.py::settle_run` | "close a provably-terminal run without journal surgery" (run-12 #25) | `{run_id, status, evidence(req), artifact_refs?, task_counts?, provenance?}` | a directed **decision sign-off** (`block:"settle-run"`, `provenance:{directed:true, kind:"human-directed-settle", evidence, artifact_refs, task_counts}`) + the SAME `mark_run` + receipt-gated `harvest_on_terminal`. **Scope = a single run's TERMINAL state only** |
| **`migrate-remainder`** | `ops/migrate/migrate_remainder.py` | "move undone tasks to a new cluster, keep lineage" | `{source_run_id, target_cluster, produced_by?}` | a DERIVED run with sidecar `parent_run_ids=[source]` → `node_sha`; a `migrate-remainder` brief (`migrated_from`, `what_moves`, `derived:{parents, cmd_sha, node_sha}`) |

### 1.3 Projection / export / brief surfaces — what EXISTS

| Verb | File / symbol | Question today | Input | Emits |
|---|---|---|---|---|
| **`export-dossier`** | `ops/export_dossier.py::export_dossier` (mutate, NOT MCP-curated) | "this **one run's** whole record trail as one integrity-sealed unit" | `{run_id, include_lineage?, output_path?}` | a store-typed zip; entry = **exactly `{source, path, sha256, bytes}`**; `bundle_sha256 = manifest_signature`. Per-run identity `_DOSSIER_RUN_FIELDS` = `{run_id, cmd_sha, node_sha, cluster, **hpc_agent_version**, scopes, supersedes, reproduces?, audit_id?}`. Lineage via `lineage_chain` (supersession). `aggregated` bytes copied, NEVER parsed |
| `archive-dossier` / `export-attestations` | `ops/archive_dossier.py`, `ops/export_attestations.py` | S3 immutable archival / in-toto DSSE statements over the SAME gather | `{archive_path, bucket, ...}` / `{run_id}` | `{bucket, key, etag, sha256, bundle_sha256}` / one Statement per sealed entry |
| **`run-story`** | `ops/run_story.py::run_story` (query, NOT curated) | "why did **this run** take the shape it did" — one deterministic journal timeline | `{run_id, include_lineage?, since_ts?, limit?}` | `{run_ids, events, story_sha, markdown, total_events, omitted_count}`; `StoryEvent{ts,stream,actor,kind,subject_id,evidence,text}` |
| **`evidence-brief`** | `ops/evidence_brief_op.py::evidence_brief` (query, MCP) | POINT query "what have we tested under tag/lineage, with what envelopes & verdicts" | `{tags[], lineage?, as_of?, fleet?}` | dated sha-cited `conclusions`, `activity` counts, `envelopes` (verbatim), `citations_status` re-resolved at read |
| **`attention-queue`** | `ops/attention_op.py::attention_queue` (query, MCP) | fleet "what needs your verdict first" | `{fleet?, class_order?}` | `items[{kind, class, subject, unblocks, since, action, evidence}]` ordered by leverage fan-out |
| campaign briefs | `meta/campaign/blocks.py::campaign_complete` | end-of-campaign code digest | `{campaign_id}` | `brief{goal, iterations, run_ids, spend, coverage, stop_reason, outcome_table, proposed_interpretations:[]}` (empty interpretations — human concludes) |

### 1.4 Reproduction machinery — what EXISTS

| Verb | File / symbol | Question today | Input | Emits |
|---|---|---|---|---|
| **`reproduce-run`** | `ops/reproduce_run.py::reproduce_run` (workflow, MCP) | mint a pinned-identity re-run of ONE finished run | `{original_run_id, new_run_name?, task_sample?}` | a repro **sidecar** with `reproduces:<orig>`, a disjoint `<orig>-repro` `remote_path`, `next_block=submit-s2`. Refuses on param OR code drift (`detect_code_drift`); discloses data drift |
| **`verify-reproduction`** | `ops/verify_reproduction.py::verify_reproduction` (query, MCP) | "did it reproduce?" (recorded-original) / "is the claim consistent?" (external-baseline) | `{repro_run_id, original_run_id?, tolerance?, external_baseline?}` | a **reproduction receipt** `_aggregated/<repro>/reproduction_receipts.jsonl` (or `claim_check_receipts.jsonl`) + an appended **fingerprint sample**; tiered `auto_cleared/needs_verdict/mismatch/incomparable`. Byte-diffs each run's `metrics_aggregate.json` |
| reproduction-verdict gate | `ops/decision/journal/reproduction.py` | admit a `needs_verdict/mismatch` sample into the envelope | `append-decision` block `reproduction-verdict` | canonicalizes `resolved.content_sha` to the ledger's full sha; refuses a bare ack / an unnamed sample |
| `verify-relay` | `ops/decision/journal/verify_relay.py` | audit the LLM's relayed numbers/state vs the run's corpus | `{run_id, relay_text}` | `{clean, mismatches:[{claim,kind,detail,nearest_source_value}]}`; corpus `_load_run_sources` = journal + sidecar + record + briefs + `metrics_aggregate.json`/`wave_*.json`/`*.csv` + campaign briefs |

**Substrate under all of it:** `state/attestation.py` (`validate`/`bind`/`reduce`) — every trusted record recomputes its `content_sha` at append (`bind`: "a hash cannot be asserted into existence") and reads `CURRENT`/`STALE`/`ABSENT` newest-wins. Evidence-memory conclusions cite `{kind∈dossier/run/fingerprint/attestation, ref, sha}`, resolved live at append, disclosed at read.

---

## §2 — The target: publication-time extraction, stated as the product move

The scientist's citable artifact is a table (a `metrics_aggregate.json`, or more
often a pack `metrics_table.csv`). The **clean minimal recipe** is a single derived
answer to four questions, each mechanical, none narrated:

1. **Which runs?** — the MINIMAL contributing run-set: the runs whose pieces are
   actually *in* the table, with canary siblings, superseded lineage members, and
   dead-end campaign runs mechanically EXCLUDED (each exclusion disclosed + counted).
2. **From what?** — each contributing run's full provenance fingerprint:
   `cmd_sha` (params) · `tasks_py_sha` (code) · `data_sha`/`data_manifest_sha` ·
   `env_hash` · **`hpc_agent_version` (the wheel)** · `cluster` · `profile`.
3. **How to re-derive?** — the mechanical steps: the submit spec(s) that mint the
   same identities + the aggregate invocation that reduces them to the same table —
   emitted as a **runnable artifact**, not prose.
4. **Proven how?** — the receipts chain, walked end-to-end with gaps DISCLOSED:
   table → aggregate record → harvest receipts → run records → interview/spec →
   wheel sha, plus any reproduction receipts and the human greenlights that resolved
   the boundaries.

The honest framing (per `run-story` D6 + the dossier no-parse pin): the recipe is
IDENTITY + ORDERING + COUNTING over opaque records. It never names a metric, never
picks the "best" run, never concludes — it says *these runs, at these shas, reduced
by this command, produced this table; here is what is missing.*

---

## §3 — Gap analysis (each candidate confirmed or killed against code)

### G1 — Is there a single verb that walks a citable ARTIFACT back to its minimal run-set + specs?  **NO (killed).**
`trace` is the closest — but it is seeded by a `run_id` or `campaign_id`, **never
by the artifact**, and it returns the WHOLE campaign (every dead end, every
retargeted-away run, every canary sibling). There is no artifact→run-set resolver.
The table→run-set link is IMPLICIT: it lives only in the `_combiner/wave_<N>.json`
partials' `run_id` membership and the per-task `.hpc_cmd_sha` files — never surfaced
by any verb, never recorded in the table itself. The scientist reconstructs it by
grepping the journal. **This is the central gap.**

### G2 — Can superseded / dead-end runs be mechanically excluded?  **PARTIAL — supersession yes, dead-ends NO.**
Supersession IS first-class: `supersede_run`, the ONE `lineage_chain` walk, and
`dossier --include-lineage` / `trace` all use it; `trace --format dot` even colors
failed runs red. BUT **"dead end" ≠ "superseded"**: a run that was explored and
abandoned (never aggregated into the citable table, never formally superseded) is
indistinguishable from a contributing run today. Nothing records the set "these runs
contributed to THIS table," so `trace`/`provenance-manifest` necessarily over-report
the whole campaign. Mechanical dead-end exclusion is **not possible** without G4.

### G3 — Is the reproduction recipe exportable as a RUNNABLE artifact (a derived clean campaign spec)?  **NO (killed).**
`dossier` exports RECORDS (bytes), `run-story` a TIMELINE, `provenance-manifest`
FINGERPRINTS + a signature, `reproduce-run` a re-run of exactly ONE recorded run.
None emits the **derived minimal spec set** that, fed back to submit + aggregate,
re-derives the table. `reproduce-run` is per-run and requires a recorded identity,
so it cannot express "re-derive this multi-run table." The recipe today is PROSE the
human writes by hand.

### G4 — Do the receipts chain end-to-end without gaps?  **NO — four concrete breaks.**
- **(a) table → run-set.** `metrics_aggregate.json` `provenance = {incomplete_waves,
  source, reduced_at}` — it records NO contributing `run_ids`, no per-run `cmd_sha`,
  no manifest signature. For a multi-run or repair table the combine reads a shared
  results tree and the table keeps no record of which runs' pieces it consumed.
- **(b) pack CSV.** The actually-cited artifact is usually a pack-reduced
  `metrics_table.csv` (non-json); `per_task_fallback_reducible` is FALSE for it and
  core has NO provenance path through it at all — it is one hop further from the
  receipts than `metrics_aggregate.json`.
- **(c) wheel sha.** The sidecar records `hpc_agent_version` and the **dossier**
  identity projection carries it — but `provenance-manifest`'s `_RUN_PROVENANCE_FIELDS`
  and `trace`'s node provenance OMIT it. The wheel sha the directive explicitly names
  is absent from the *signable* provenance manifest.
- **(d) operator-bypass (run-13 finding 14).** When the reducer runs OUTSIDE
  hpc-agent (the operator's direct reduce), the table has no aggregate record, no
  harvest receipt, and journal provenance is LOST. `settle-run` settles a single
  run's terminal state — there is NO analogue for "a table was produced outside the
  flow; attach provenance to it retroactively." The "post-hoc human-directed settle
  record" the run-13 close-out owes has no home today.

### G5 — What did run-13's operator-bypass settle record teach?  (`docs/design/history/run13-findings.md`)
Findings **13 / 13-addendum / 14** are one lesson: **the table's provenance — which
runs' pieces, at which `cmd_sha` — is not a first-class object**, so BOTH failure
shapes lose the run-set→table link. (13) the fixmask *graft/repair* re-ran 300 arms
under a new run id into another run's tree; the combine fell back to walking 2,700
payloads because the grafts invalidated no wave bookkeeping. (13-addendum) the fresh
reduce faithfully reproduced stale exploded arms because the per-task cache is keyed
by task-id and the `.hpc_cmd_sha` staleness fingerprint **exists on disk but is never
compared**. (14) the operator then ordered a full bypass; numbers stayed code-computed
but provenance was lost. The extraction the user wants is EXACTLY the missing
first-class link: **"this citable table ← these run pieces at these shas,"** recorded
at reduce time and walkable at publication time.

---

## §4 — Proposals, ranked by leverage

House rule honored throughout: **extend existing verbs / compose shipped walks**;
prefer a role-root op over a new subject; render is pure code (`*_render.py`), never
LLM. The reproduction is IDENTITY/ORDERING/COUNTING over opaque records — no metric
named, no run judged "best."

### ★ #1 — `extract-recipe`: the artifact → minimal-run-set → runnable-recipe walk (TOP; buildable next session)

**What it is.** A read-only `query` verb that, given a citable artifact reference
(a `run_id`, a `campaign_id`, or a path to a `metrics_aggregate.json`), walks BACK to
the **minimal contributing run-set** and emits one deterministic recipe:

1. the minimal run-set — canary siblings (`sibling_run_ids`), superseded lineage
   members (`lineage_chain`, keep newest), and dead-end runs (no harvest receipt /
   no piece under the reduced path) EXCLUDED, each exclusion disclosed + counted;
2. each contributing run's full fingerprint **including `hpc_agent_version`**, plus
   a `manifest_signature` over ONLY the minimal set (a table-specific attestation,
   not a whole-campaign one);
3. the **runnable re-derivation steps** — the submit spec(s) that re-mint those
   identities + the aggregate invocation — as a runnable artifact, not prose;
4. the receipts chain (harvest receipts + reproduction receipts + the resolving
   greenlights) with every gap from G4 DISCLOSED, never papered over.

**Existing machinery it composes.** `ops/trace.py` (the DAG walk + `parent_run_ids`
lineage) · `ops/provenance_manifest.py` (`project_run_provenance` + `manifest_signature`
— extend the field list) · `sibling_run_ids` + `lineage_chain` (the two exclusion
walks, reused not re-implemented) · `harvest_receipt_exists` + the `wave_<N>.json`
`run_id` membership (the table→run-set source) · `reproduce-run`'s spec
materialization (the runnable steps) · `relay_render.py` posture (the markdown).

**The minimal-set mechanism (the new logic).** Two honest sources, in order:
(i) inside-flow — read the table's contributing `run_ids` from Task-1's new
`contributing_run_ids` provenance field (below); (ii) fallback — the campaign trace
minus dead ends (runs with no harvest receipt / no piece under `record.remote_path`).
Superseded members collapse via `lineage_chain`. Every exclusion is a countable
disclosed fact.

**Files touched.**
- **Task 1 (foundational — do first):** extend `ops/aggregate_flow.py::_persist_local_aggregate`
  provenance from `{incomplete_waves, source, reduced_at}` to add
  **`contributing_run_ids`, `piece_cmd_shas`, `hpc_agent_version`** (the `.hpc_cmd_sha`
  set the combine already reads); mirror in the cluster combiner footer
  (`execution/mapreduce/combiner.py::_final_reduce`). This makes the table→run-set link
  first-class for the NORMAL flow and simultaneously gives the run-13 graft/stale-cache
  class the fingerprint it needs. SMALL.
- extend `ops/provenance_manifest.py::_RUN_PROVENANCE_FIELDS` with `hpc_agent_version`
  (closes G4c) — bump `manifest_schema_version`.
- NEW `ops/extract_recipe.py` (role-root, composes the above) + `_wire/queries/extract_recipe.py`
  + NEW `ops/recipe_render.py` (deterministic markdown).
- regen (six scripts) · `tests/contracts/test_extract_recipe_boundary.py` (the
  no-metric / one-lineage / disclosed-gaps pins, the dossier AST-scan precedent) ·
  one `docs/internals/engineering-principles.md` enforcement row · this doc → build spec.

**Size:** MEDIUM (rides shipped walks; the genuinely new code is the minimal-set
selection + the runnable-recipe render; Task 1 is a small standing fix).

**Rulings needed.**
- **R1 (MCP curation).** `trace`/`provenance-manifest`/`dossier`/`run-story` are all
  deliberately NOT MCP-curated (operator/renderer actions). Recommend `extract-recipe`
  follow the precedent — NOT curated in v1; revisit trigger = an agent hand-rolling the
  walk (cat-ing `wave_*.json` through raw shell).
- **R2 (artifact input boundary).** Accept `run_id`/`campaign_id` (identity core owns)
  + optionally a path to a `metrics_aggregate.json`. Do **not** parse a pack `metrics_table.csv`'s
  content (the dossier no-parse boundary) — a CSV is accepted only as an OPAQUE citation
  whose provenance is its containing run's, disclosed as such (G4b stays a disclosed gap,
  not a parse).
- **R3 (signature schema).** Adding `hpc_agent_version` to the signable manifest is a
  `manifest_schema_version` bump — needs the version-bump ruling so old signatures
  re-derive honestly.

### #2 — `settle-aggregate`: give the operator-bypass table a provenance home (closes G4d)

**What it is.** Extend the `settle-run` directed-evidence pattern to the AGGREGATE
stage. Given a table produced outside the sanctioned flow + directed evidence (the
reducer identity, the source-tree sha, the run-set), journal a directed aggregate
sign-off AND synthesize the missing `metrics_aggregate.json` provenance
(`source:"human-directed"`, `contributing_run_ids`, `reduced_by`, `evidence`) + a
harvest-style receipt — so the bypass table re-enters the receipts chain. This IS the
"post-hoc human-directed settle record" run-13 owes, and it makes #1's walk succeed on
a bypass table instead of disclosing a total gap.

**Composes.** `ops/settle_run.py` (the directed-evidence journaling shape) ·
`_persist_local_aggregate` (Task-1's extended provenance shape) · the harvest receipt
ledger. **Files:** NEW `ops/settle_aggregate.py` (or a mode on `settle_run`) + wire +
schema + enforcement row. **Size:** MEDIUM. **Ruling:** should the aggregate provenance
gain `contributing_run_ids` as a STANDING field — yes, it is #1's Task 1, so #2 rides it.

### #3 — recipe portability + wheel-sha in the sealed bundle

`run-story` already names a deferred `--from-dossier` reading mode. Ship the recipe
INSIDE the sealed dossier (a derived `recipe` file, or a new `recipe` store noun in
`DOSSIER_SOURCES` — a string add, zero wire-schema change per the dossier extensibility
ruling) so a reviewer with only the zip can re-derive + re-check the signature. Lower
leverage; deferred until #1 lands. **Size:** SMALL-MEDIUM.

### #4 — evidence-brief conclusion ↔ recipe cross-link

Add a `recipe` citation kind to `CITATION_KINDS` (`state/evidence.py`) so a published
conclusion cites the `extract-recipe` signature: "this claim ← this minimal run-set
recipe," one resolvable citation, re-checked at read. Rides evidence-memory's citation
extensibility. Deferred until #1's signature exists. **Size:** SMALL.

**Ranking rationale.** #1 is the whole deliverable — it turns the implicit table→run-set
link into a first-class, walkable, runnable, gap-disclosing recipe, and its Task 1 is the
one mechanical fix (`_persist_local_aggregate` provenance) that also retires the run-13
graft/stale-cache class. #2 closes the operator-bypass hole #1 would otherwise only
disclose. #3/#4 make the recipe portable and citable once it exists.

---

## Drift log

- **2026-07-17 — created (scoping-only).** Cites the user directive (2026-07-17): amplify
  human effort, refuse nothing, sharpen clean-reproduction extraction after messy
  experimentation. Inventory read against `main @ 6d29e23b` across the reproduction,
  provenance, and projection/export surfaces (`reproduce-run`/`verify-reproduction`,
  `trace`/`provenance-manifest`/`settle-run`/`migrate-remainder`,
  `dossier`/`run-story`/`evidence-brief`/`attention-queue`/campaign briefs, the
  attestation kernel). Gap analysis grounded in `docs/design/history/run13-findings.md`
  (findings 13/13-addendum/14 — the operator-bypass + graft/stale-cache class). No
  `src/**` change lands here; the four §4 proposals are the build queue, #1 buildable
  next session with Task 1 (`_persist_local_aggregate` provenance) as its foundation.
