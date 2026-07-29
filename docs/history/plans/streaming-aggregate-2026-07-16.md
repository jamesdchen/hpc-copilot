# streaming-aggregate — DESIGN SPEC

2026-07-16 · baseline `main @ 7615ca67`. **Parked at spec time:** the wave-1
aggregate/transport/submit train is DIRTY in the tree — `ops/aggregate_flow.py`,
`execution/mapreduce/reduce/status.py`, `infra/transport/_pull.py`,
`ops/submit_and_verify.py`, `ops/submit_flow.py`, `ops/verify_canary.py`,
`ops/monitor/kill.py`, `ops/auto_resume_flow.py` are uncommitted (`git status`).
**Every unit here rebases on that train AFTER it commits** (§9). Machine twin:
`unit-specs.json` alongside (owed at dispatch; this SPEC settles the calls).

USER DIRECTIVE (2026-07-16, live meeting-deadline run): *"incorporate the
STREAMING of the results."* The operator ran **two legs** — `lgbm@carc` (9 buckets
done) and `xgb@hoffman2` (draining bucket-by-bucket) — and **assembled a
progressively-refining table by hand**: *"40-arm table now (36 + hoffman2's 4 xgb
buckets), refresh to 44 when carc lands, with `xgb/vol_demand` as the one
disclosed PENDING arm."* Mechanize that: the framework must emit a **partial-but-
honest** aggregate table as buckets/waves/legs land, instead of all-or-nothing at
final harvest.

**Live motivating artifacts** (computed this session, `run14-rigor-runsheet.md`):
the lgbm envelope `_aggregated/causal_tune_tree_lgbm-7905102a/
causal_tune_tree_lgbm-7905102a.json` declares `schema="causal_tune_tree
metrics_table v1"`, `n_arms=9`, 9 rows keyed by bucket
(`all_features/baseline/implied_vol/…/vol_demand`), each with `qlike` (0.126–0.131
vs `incumbent_qlike=0.13415`), `dm_better`, and `n=218934`. The reducer is a
**custom `aggregate_cmd`** = `python3 specs/reduce_causal_tune_tree.py` — the
cluster-reduce path, not the built-in weighted-mean. An **arm = one (model,
bucket) grid-point row**; a bucket = one 100-task wave (bucket-major tiling, the
`migrate-remainder` [LIVE-1] shape). The operator's manual table = two run_ids'
`_aggregated` envelopes concatenated, minus the still-draining `xgb/vol_demand`.

---

## 1. One-paragraph shape

`aggregate-stream` is a **read-mostly, seconds-returning, re-callable query verb**
that, given **one run OR a set of parent run_ids**, emits the **current best
table over whatever ARMS are complete NOW** and discloses every incomplete arm
**by name**. It: **(a) censuses per-arm completeness** from the cluster's per-task
terminal announcements (`read_announced_task_ids`, `ops/monitor/announce.py:183`)
joined to the sidecar `wave_map`/grid so each arm resolves to its task set and is
`COMPLETE` iff **all** its tasks announced `complete`, `PENDING` otherwise (an
IDENTITY/COUNT/COMPARE over opaque arms — no metric touched); **(b) reduces only
the complete arms** through the run's **own deterministic reducer** (the
`aggregate_cmd` cluster-reduce path, `ops/aggregate/cluster_reduce.py:190`, or the
built-in `reduce_metrics`) — every number is reducer-computed, never the LLM;
**(c) emits a partial envelope** shaped exactly like the final
`metrics_aggregate.json` (`ops/aggregate_flow.py:1261 _persist_local_aggregate`)
but carrying `arms_complete` + an `arms_pending:[{arm, tasks_done, tasks_expected,
owner_run_id}]` disclosure block — the never-silent-cap rule; **(d) refines
monotonically** — each call supersedes the prior snapshot, the `arms_complete` set
is non-decreasing (a stale-piece graft is the only shrink, and it re-uses
`aggregate_flow._evict_stale_mirror_pieces`, `:191`), and the brief reports the
delta since the last snapshot; **(e) merges multiple parents via the ownership
map** — a migrated/split run (lgbm-leg + xgb-leg, or a `migrate-remainder`
source+derived pair) reduces both parents' mirrors with M-HARVEST's
ownership-aware selection (`ops/migrate/harvest.py:257 multi_parent_reduce`), NOT
the single-run `run_id` F05 filter (`reduce/metrics.py:265`). The verb **actuates
nothing** — no submit, no kill, no journal terminal — it returns a brief the human
reads and re-calls until every arm lands.

---

## 2. What EXISTS vs what is NEW (with citations)

| Capability | Exists today (file:line) | Gap → new work |
|---|---|---|
| Per-task terminal census (the "which arms are done NOW" signal) | `read_announced_task_ids` returns the SET of `task_<id>.complete` ids under the ACK discipline (`ops/monitor/announce.py:183`); COMPLETE-only (a `.failed` marker is not done, `announce.py:170`) | Reads TASK ids, not ARM completeness. Need a **task-set→arm join** (wave_map / grid grouping) + the whole/partial classification → **S-CENSUS** |
| Wave→task-id partition | sidecar `wave_map` `{str(wave):[global ids]}` (`ops/submit_flow.py` `build_wave_map`); `census._wave_alignment` intersects done×wave_map (`ops/migrate/census.py:113`) | `_wave_alignment` yields whole undone WAVES for a migration; streaming needs whole DONE ARMS (the inverse) + the reducer's grid grouping when an arm ≠ a wave → **S-CENSUS** |
| Reducer grid grouping (task→arm key) | `reduce_by_grid_point` groups tasks by `params` → grid key (`reduce/metrics.py:174`); custom reducers group by their own bucket field | Core does not know a custom reducer's arm key per task without the grid; the join must key on the SAME grouping the reducer emits → **S-CENSUS** (reads the run's grid / `tasks.py`) |
| Deterministic reduce over a task/dir SUBSET | `reduce_metrics(result_dirs, filename=…)` takes an explicit dir list (`reduce/metrics.py:134`); `cluster_reduce` runs the custom `aggregate_cmd` (`cluster_reduce.py:190`) | The built-in path already accepts a dir subset (feed only complete arms' dirs). The **custom reducer** takes NO arm allowlist — it reduces whatever is on disk, so a half-drained bucket would emit a wrong `n`/`qlike` → **S-REDUCE contract** |
| Partial-aggregate opt-in | `ensure_all_combined=false` bypasses the terminal gate for a deliberate partial (`aggregate_flow.py:1845`); `missing_waves` under `allow_partial` is surfaced-not-blocking (`aggregate_blocks.py:474`) | That partial is at the WAVE grain and still all-or-nothing on the emitted table — no per-arm complete/pending split, no by-name pending disclosure → **S-STREAM** |
| Durable aggregate envelope | `_persist_local_aggregate` writes `{aggregated_metrics, provenance:{incomplete_waves, source, reduced_at}}` (`aggregate_flow.py:1261`); the custom reducer emits its own richer envelope (`n_arms`, per-bucket rows) | No `arms_complete`/`arms_pending` provenance; no monotonic snapshot / delta → **S-STREAM** (extend the provenance block, additively) |
| Multi-parent ownership reduce | `multi_parent_reduce(source_mirror, derived_mirror, ownership, …)` selects each cell's single owner then `reduce_metrics` (`ops/migrate/harvest.py:257`); `OwnershipMap` + `select_owned_dirs` (`ownership.py:48`, `harvest.py:160`) | Built for a migrate source+derived pair keyed by cell id. Streaming needs the SAME reduce keyed by **arm→owner** across N legs (lgbm-run owns lgbm arms, xgb-run owns xgb arms) → **S-STREAM** composes it; a two-leg case with disjoint arm spaces is a **generalization**, not an edit |
| env-python reducer interpreter | `env_python` clusters.yaml key + `remote_activation_for_sidecar` emits a preamble-free PATH-prepend prefix (`infra/clusters.py:852,925`, commit `9c410a8e`); `run_final_reduce` threads `remote_activation` (`transport/_combiner.py:157`) | `cluster_reduce._build_remote_cmd` threads **NO activation** (`cluster_reduce.py:90-95`) — the user's `aggregate_cmd` literal `python3` hit bare login python (run-14: py3.13-syntax reducer crashed under login py3.8) → **S-REDUCE** |
| Ship-the-reducer | deploy items ship `.hpc/_hpc_combiner.py`, `_hpc_dispatch.py`, `reduce/status.py`, etc. (`infra/transport/_deploy_items.py:101,126,153`) | The **custom `aggregate_cmd` reducer** (`specs/reduce_causal_tune_tree.py`) is experiment-repo code with NO deploy analogue — it was **scp'd by hand** this run → **S-REDUCE** (the docketed "S4 ships its own reducer") |
| Gated brief surface | `aggregate-run` returns the results table + EMPTY `proposed_interpretations` (`aggregate_blocks.py:746`); `status-snapshot` embeds a fleet digest (`status_blocks.py:343`) | A streaming brief is a **query** (no greenlight, no next_block — a reporter, like `verify-registration`), re-callable → **S-STREAM** |

**Net new surface:** one package `src/hpc_agent/ops/aggregate/stream.py` (arm
census + the streaming reduce + the partial-envelope emit), a new wire model +
`aggregate-stream` verb, plus **two reducer-contract fixes** on the (post-commit)
transport seam: activation-threading in `cluster_reduce` and a deploy item for the
run's declared reducer. **No parked file is edited in place** (§9): the arm census
reuses `announce.py`'s existing id-sibling and the ownership reduce reuses
`ops/migrate/harvest.py` verbatim.

---

## 3. The flow, step by step, with refusals

Every step is a guard-that-can-fire (the engineering-principles rule; verify each
guard can actually fire).

### Step A — arm census (S-CENSUS)
1. `read_announced_task_ids(ssh_target, remote_path, run_id)` per parent → the SET
   of `task_<id>.complete` ids (bounded `ls`, ACK-gated, `announce.py:183`). A
   missing announce dir / no ACK → `present=False` → **refuse** with "no per-task
   census present (pre-announce run or dispatcher not started); reconcile first" —
   NEVER read absence as "all arms complete" (the Δ1 discipline, `census.py:215`).
   An ssh transport failure (rc 255) raises — a blip is never an empty done-set.
2. **Task→arm join.** Resolve each arm's task set from the run's grid grouping:
   when the tiling is bucket-major (an arm = a whole wave, [LIVE-1]) use the
   sidecar `wave_map` (`_wave_alignment` precedent, `census.py:113`); otherwise
   group tasks by the reducer's grid key (`reduce_by_grid_point`'s `params`→key,
   `reduce/metrics.py:196`) read from `tasks.py`/the sidecar grid. The join key
   MUST be the SAME grouping the reducer emits, else a "complete arm" would not
   line up with a reducer row.
3. **Whole-arm classification (the n-guard).** An arm is `COMPLETE` iff **every**
   task in its set has a `.complete` marker; `PENDING` otherwise, carrying
   `tasks_done`/`tasks_expected`. This is the arm-grain analogue of the
   cardinality gate (`aggregate_flow.py:751` refuses > total) — here the FEWER
   side per arm is the pending signal, not an error. A partial bucket (some chunks
   missing) is `PENDING`, never emitted — the reducer would otherwise mean a wrong
   `n`/`qlike` over the drained subset (the live `xgb/vol_demand` case).
4. **Status-reporter cross-check** (optional, when a report is in hand):
   `rows_observed_from_report` (`infra/cluster_status.py`) vs the announce done-set;
   a disagreement is **surfaced never auto-masked** (the aggregate-check integrity
   precedent, `aggregate_blocks.py:453`; the `census._cross_check` shape, `:150`).
5. **Refuse** when **zero** arms are complete (nothing to emit yet) — a clean
   "still draining, N arms pending by name" brief, not a fabricated empty table.

### Step B — reduce the complete arms (S-REDUCE contract, S-STREAM drive)
- **Built-in path** (`reduce_metrics`): feed ONLY the complete arms' result dirs
  (`aggregate_flow.py:730` builds the dir list; filter to complete-arm dirs before
  the call). A dir subset is already the reducer's native input — no contract
  change, just a narrower list.
- **Custom `aggregate_cmd` path** (the live lgbm/xgb reducers): the reducer runs
  over whatever is on the cluster and emits ALL arms it finds. To stream a partial
  honestly the reducer MUST reduce only complete arms. Two contract options, both
  surfaced as **S-REDUCE**:
  - *(preferred)* thread an **arm/task allowlist** env var (`HPC_STREAM_ARMS=…` /
    `HPC_STREAM_TASK_IDS=…`) the reducer honors — a small additive convention the
    reducer opts into; core computes the allowlist deterministically from S-CENSUS.
  - *(fallback, reducer-agnostic)* reduce **per-complete-arm** over that arm's
    dirs and union the rows — works for any value-keyed reducer without a reducer
    edit, at the cost of one reducer invocation per arm.
- **Every number is reducer-computed** — core selects the INPUT (which arms) and
  the reducer computes the OUTPUT (the metrics). Core never means, sums, or
  interprets a value (§4).

### Step C — reducer-contract fixes (S-REDUCE, gates on the parked train)
1. **env-python interpreter.** `cluster_reduce._build_remote_cmd`
   (`cluster_reduce.py:90-95`) threads NO activation, so the reducer's literal
   `python3` binds bare login python (run-14 py3.8-vs-3.13 crash). Thread
   `remote_activation_for_sidecar(sidecar, fallback_cluster=record.cluster)`
   (`infra/clusters.py:852`, the ONE control-plane seam) as the prefix — the same
   preamble-free `env_python` PATH-prepend `run_final_reduce` already gets
   (`_combiner.py:157`). The reducer's `python3` then resolves to the run's env
   interpreter. Disclose the pinned interpreter in any refusal (the `9c410a8e`
   `pre_stage_smoke` precedent) so a local-env miss is not read as a cluster bug.
2. **Ship-the-reducer.** Add the run's declared reducer file to the deploy set so
   submit stages it like `.hpc/_hpc_combiner.py` (`_deploy_items.py:126`), killing
   the manual scp. The reducer relpath is derivable from `aggregate_cmd` (the
   `specs/reduce_*.py` token) or an explicit `aggregate_defaults.reducer_file`
   sidecar key; a reducer that is not on disk at submit → **refuse loudly** at
   stage (never a mid-harvest "no such file"). This is the docketed **"S4 ships
   its own reducer."**

### Step D — emit the partial envelope + monotonic snapshot (S-STREAM)
Write `metrics_aggregate.json` at the canonical flat location (`aggregate_flow.py:1197`
convention) with the reducer's `aggregated_metrics` over complete arms PLUS an
additive provenance block:
```
provenance: {
  source: "stream", reduced_at, parents: [run_id, …],
  arms_complete: [arm, …],
  arms_pending:  [{arm, tasks_done, tasks_expected, owner_run_id}, …],  # by name
  snapshot_seq: <monotonic int>, superseded: <prior snapshot_seq or null>,
  disagreement: <census cross-check or null>
}
```
- **Monotonic refine.** Each call bumps `snapshot_seq`; `arms_complete` is
  non-decreasing across calls (assert it — a shrink is a bug UNLESS a stale-piece
  graft evicted an arm, in which case re-use `_evict_stale_mirror_pieces`
  (`aggregate_flow.py:191`) + `_invalidate_waves_for_refreshed_tasks` (`:219`) and
  DISCLOSE the eviction). The brief reports `newly_complete` = this call's
  `arms_complete` − the prior snapshot's.
- **PENDING never silently omitted** — every pending arm is named with its
  progress. The table the human reads = complete-arm rows; the pending list = what
  is still coming and from where (`owner_run_id`).

### Step E — multi-parent merge (S-STREAM composes M-HARVEST)
For `parents=[lgbm-run, xgb-run]` (or a `migrate-remainder` source+derived pair):
pull each parent's `_per_task_results` mirror (read-only, `aggregate_flow.py:642`
shape), build the **arm→owner** map (each parent owns its arm space; a migrated run
uses the persisted `OwnershipMap`, `ownership.py:203`), and reduce with
`multi_parent_reduce`'s owner-selection (`harvest.py:257`) — NOT the single-run
`run_id` F05 filter (`reduce/metrics.py:265`), which would drop a whole parent. The
arm census (Step A) runs per parent; the union pending list spans both legs (the
operator's `xgb/vol_demand`-pending, lgbm-complete table). Canary-family exclusion
is applied per parent (`harvest.py:131`, `aggregate_flow.py:742`).

---

## 4. Doctrine check — streaming a partial is core-safe

**The rule:** streaming a partial table is honest IFF (a) every emitted number is
reducer-computed and (b) every pending arm is disclosed by name. Both hold here:

- **IDENTITY / COUNT / COMPARE over opaque arms.** Core does exactly three things
  to each arm: IDENTITY (which tasks belong to it — from `wave_map`/grid,
  deterministic), COUNT (how many announced `complete` — from markers), COMPARE
  (`done == expected` → COMPLETE). It never opens a `metrics.json`, never means,
  never reads a `qlike`. The arm is opaque to core; the reducer alone computes over
  its contents. This is the determinism-boundary the framework is built on
  (`docs/internals/principles/determinism-boundary.md`).
- **No LLM in the numeric loop.** The complete/pending split (set membership), the
  arm allowlist (census-derived), and the ownership selection (map lookup) are all
  code. The reducer computes every aggregate number (`reduce/metrics.py`, the
  custom `aggregate_cmd`) — never the model (the founding constraint; the run-13
  finding-14 operator-bypass is exactly what this closes: no hand-assembled table).
- **No-silent-caps / honest refusals over silent success.** A pending arm is NEVER
  dropped from the surface — it rides `arms_pending` by name. Absent census,
  zero-complete, a reducer not on disk, a census disagreement — each **refuses or
  discloses loudly**, never fabricates a full-looking table from a partial.
- **Observe / judge / route, never actuate.** `aggregate-stream` is a **query**:
  no submit, no kill, no journal terminal, no greenlight. It returns in seconds
  (bounded `ls` census + a local-or-cluster reduce over already-staged pieces) —
  the MCP-safe property. Re-callable with no state mutation beyond the snapshot
  file it overwrites.

---

## 5. What the reducer contract requires [ties §3.B / §3.C]

`reduce_metrics` is **value-keyed** with **no arm/task-id keying and no cardinality
gate** (`reduce/metrics.py:97,101-102`) — it will happily mean a half-drained
bucket. Therefore streaming correctness rests on **restricting the INPUT to
complete arms**, three ways depending on the reduce path:

1. **Built-in weighted-mean** — pass only complete arms' result dirs
   (`reduce_metrics` takes the explicit list, `:134`). Zero contract change.
2. **Custom reducer, allowlist-aware** — the reducer honors `HPC_STREAM_ARMS` and
   skips absent arms. Additive, opt-in; the clean long-term shape.
3. **Custom reducer, agnostic** — core invokes the reducer per complete arm and
   unions the rows. No reducer edit; N invocations. The fallback the live lgbm/xgb
   reducers get for free.

Disjointness across parents is guaranteed by construction (each parent's result
dirs render under its own run_id; the arm→owner map is exactly-once), so the
`unexpected_tasks`/cardinality invariants (`ops/aggregate/invariants.py:215`,
`aggregate_flow.py:751`) stay the **safety net**, firing only on genuine overlap —
never the primary guard. The qdel-race double-count [migrate LIVE-2] cannot arise
in the single-run streaming case (one owner per arm) and is handled by the
ownership map in the multi-parent case (`harvest.py:257`).

---

## 6. Settled design calls (finding → binding resolution)

| # | Finding | Binding resolution (unit) |
|---|---|---|
| S1 | "Which arms are done NOW" needs ids, not counts | Reuse `read_announced_task_ids` (`announce.py:183`); absence **refuses** (Δ1) (S-CENSUS) |
| S2 | An arm ≠ a wave in general; the join must match the reducer's grouping | wave_map when bucket-major [LIVE-1], else `reduce_by_grid_point` params-key from `tasks.py` (S-CENSUS) |
| S3 | A half-drained arm emits a wrong `n`/`qlike` | Whole-arm n-guard: emit COMPLETE arms only; PENDING by name (S-STREAM) |
| S4 | Custom reducer reduces everything on disk | Arm allowlist env var (preferred) OR per-arm reduce+union (agnostic fallback) (S-REDUCE/S-STREAM) |
| S5 | Custom reducer runs bare login `python3` (run-14 py3.8-vs-3.13 crash) | Thread `remote_activation_for_sidecar` into `cluster_reduce` (`cluster_reduce.py:90`); disclose pinned interpreter (S-REDUCE) |
| S6 | Reducer scp'd by hand | Ship the run's declared reducer as a deploy item (`_deploy_items.py:126` precedent); refuse at stage if absent (S-REDUCE) |
| S7 | Two legs assembled by hand | `parents=[…]` + `multi_parent_reduce` owner-selection, NOT the F05 `run_id` filter (`harvest.py:257`) (S-STREAM) |
| S8 | Partial must refine, never regress | Monotonic `snapshot_seq`; `arms_complete` non-decreasing except a disclosed stale-graft evict (`aggregate_flow.py:191`) (S-STREAM) |
| S9 | Streaming a partial must stay honest | Every number reducer-computed + every pending arm named; a query, no actuation (§4) |

**DECLINED:** editing `aggregate_flow.py`'s final-harvest path to emit partials
(parked; S-STREAM composes a new module instead). Making `aggregate-run` itself
streaming (it is a gated greenlight block; streaming is a re-callable **query** —
different contract). Inferring arm completeness from the reduced `n` alone (a
custom reducer may not emit a per-arm `n`; the announce census is the ground
truth). Folding the snapshot into the run sidecar (state-writer seam is
parked/forbidden — a `_aggregated/<run_id>/` snapshot file for v1, mirroring the
migrate ownership-artifact call, `SPEC §Δ3`).

---

## 7. Wave plan (file-disjoint; the `_TEMPLATE-handoff` schema)

- **Wave S0 (pre, no swarm):** rebase every unit on the committed wave-1
  aggregate/transport train (§9). No new files land here; this is the "AFTER it
  commits" gate the task names.
- **Wave S1 (parallel, file-disjoint):**
  - **S-CENSUS** — `ops/aggregate/arm_census.py` (new): the task→arm join + whole/
    partial classification + cross-check. Reuses `announce.read_announced_task_ids`
    and `census._wave_alignment` (imports, no edits).
  - **S-REDUCE** — the reducer-contract fixes: `ops/aggregate/cluster_reduce.py`
    (thread activation) + `infra/transport/_deploy_items.py` (ship the reducer) +
    `ops/submit_flow.py` stage-refuse. **Gates on the parked train landing** —
    both files are on it (§9); this unit dispatches sequentially after S0.
- **Wave S2 (single unit, regen-forcing):**
  - **S-STREAM** — `ops/aggregate/stream.py` (new, the `aggregate-stream` verb) +
    `_wire/workflows/stream_aggregate.py` (new wire model) + schema regen. Composes
    S-CENSUS, the S-REDUCE-fixed reduce, and `migrate/harvest.multi_parent_reduce`.
    The only regen-forcing unit; gates on S1.

Integration per wave: ordered merge → `scripts/regen_all.py --write` once →
ruff/format/mypy → per-unit batteries → push → CI matrix green → enforcement rows.

### Unit table (twin of `unit-specs.json`)

| unit | wave | files (exclusive claim) | forbidden_files | regen | merge_risk |
|---|---|---|---|---|---|
| **S-CENSUS** | S1 | `ops/aggregate/arm_census.py`, `tests/ops/aggregate/test_arm_census.py` | `ops/aggregate_flow.py`, `ops/monitor/announce.py`, `ops/migrate/census.py`, `infra/ssh_slots.py`, all migrate `ops/migrate/*` | no | low — new file, import-only reuse |
| **S-REDUCE** | S1 (after S0) | `ops/aggregate/cluster_reduce.py`, `infra/transport/_deploy_items.py`, `ops/submit_flow.py` (stage-refuse hunk), `tests/ops/aggregate/test_cluster_side_reduce.py`, `tests/infra/test_deploy_items.py` | `ops/aggregate_flow.py`, `infra/transport/_combiner.py`, `infra/transport/_pull.py`, `execution/mapreduce/reduce/status.py`, `infra/ssh_slots.py` | no | **HIGH — three files are on the parked train; rebase first, dispatch after S0** |
| **S-STREAM** | S2 | `ops/aggregate/stream.py`, `_wire/workflows/stream_aggregate.py`, `src/hpc_agent/schemas/stream_aggregate.input.json`, `docs/primitives/aggregate-stream.md`, `tests/ops/aggregate/test_stream.py` | `ops/aggregate_flow.py`, `ops/aggregate_blocks.py`, `ops/migrate/harvest.py` (import-only), `cli/parser.py` until it lands | **yes** (`operations.json`, `docs/generated/operations.md`) | med — new verb; regen coordinates |

Run `scripts/check_handoff_disjointness.py docs/plans/streaming-aggregate-2026-07-16/unit-specs.json --against-worktree` before each wave.

---

## 8. The single hardest problem — the task→arm join across a custom reducer's grouping

The announce census gives **task ids**; the reducer emits **arm rows**. Streaming a
partial correctly needs the map between them, and that map lives in the **reducer's
own grouping**, which core does not own. In the live bucket-major case it is trivial
— one 100-task wave = one bucket = one arm, so `wave_map` IS the join ([LIVE-1]).
But a general custom reducer buckets by an arbitrary field of each task's `params`
(e.g. `estimator`, a halo window, a feature set), and **only the reducer knows
which field**. If core guesses the grouping wrong, a "complete arm" won't line up
with a reducer row and the n-guard silently mis-fires — either emitting a
half-drained arm (wrong `n`) or withholding a complete one.

Three resolutions, ranked:
1. **Grid-declared arm key** (clean): the run declares its arm-grouping field in the
   sidecar grid / `interview` (`reduce_by_grid_point`'s `params`-key, `metrics.py:196`
   is the existing precedent for grouping by `params`). Core joins task→arm on that
   declared key — no guessing. Requires a one-field submit-time declaration; the
   right long-term shape.
2. **Wave-aligned only** (v1 fallback, [LIVE-1]): stream ONLY when the tiling is
   bucket-major (an arm = a whole wave, provable from `wave_map` via
   `_wave_alignment`, `census.py:113`). A non-wave-aligned run **refuses to stream**
   with "arm grouping not declared; final harvest only" — honest, covers the live
   case, never mis-fires.
3. **Reducer-reported arm membership** (most general): the reducer, run in a
   `--census` mode, emits `{arm: [task_ids]}` and core trusts THAT as the join. But
   it costs a reducer invocation per census tick and a reducer-side contract
   addition — heavier than (1).

v1 ships **(2)** (wave-aligned streaming, the live lgbm/xgb shape) with **(1)** as
the declared-arm upgrade path. Whichever lands, the n-guard's correctness reduces
to "core's task→arm join equals the reducer's row grouping" — so the join key is the
load-bearing invariant, and a mis-join must **refuse**, never silently emit.

---

## 9. Parked files at spec time (FORBIDDEN until committed)

`git status @ 7615ca67` is the authority. Dirty at spec time — every unit rebases
first and treats these as owned by the in-flight wave-1 train:

- **Parked aggregate/transport/submit train (uncommitted):** `ops/aggregate_flow.py`,
  `ops/submit_and_verify.py`, `ops/submit_flow.py`, `ops/verify_canary.py`,
  `ops/monitor/kill.py`, `ops/auto_resume_flow.py`,
  `execution/mapreduce/reduce/status.py`, `infra/transport/_pull.py`.
- **The ssh-slot fix** (`infra/ssh_slots.py`) and any transport `_combiner.py`
  edits from the same train.
- **CLI/regen surface** (`cli/parser.py`, `operations.json`,
  `docs/generated/operations.md`, `scripts/regen_all.py`) — S-STREAM's regen gates
  on these settling.

**All new streaming work lands under the new `ops/aggregate/` modules
(`arm_census.py`, `stream.py`) + a new wire model + new tests + docs.** The two
reducer-contract fixes (S-REDUCE) DO touch train files (`cluster_reduce.py`,
`_deploy_items.py`, `submit_flow.py`) and are therefore **explicitly sequenced
after S0** (the train commit) — they are the one unit that cannot be fully
file-disjoint from the parked work, and the memo names that as the top merge risk.
The snapshot is a `_aggregated/<run_id>/metrics_aggregate.json` artifact, not a
sidecar write, precisely to stay off the forbidden state-writer seam.
