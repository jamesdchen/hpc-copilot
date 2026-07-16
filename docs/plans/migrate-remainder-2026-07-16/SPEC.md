# migrate-remainder — DESIGN SPEC

2026-07-16 · baseline `main @ c9cab681` (+ parked uncommitted swarm edits: the
WS-DAEMON D-FSYNC/D-CONTEXT scope and a wave-1 aggregate/transport train are dirty
in the tree — every unit here rebases on those before dispatch, see §9). Machine
twin: `unit-specs.json` in this directory.

USER DIRECTIVE (2026-07-16): *"migrate-remainder must be possible"* — mechanize
moving a run's **undone** tasks to another cluster as **one gated verb**, replacing
tonight's "an hour of careful surgery."

**Live motivating case** (the surgery being done by hand right now): xgb run
`causal_tune_tree_xgb-0b5ef197`, **216/900 tasks done on hoffman2**, the question
= move the **684 remaining** to carc. Task identities are chunk-by-bucket cells;
wave arrays are not chunk-aligned in general; the reducer pools any *disjoint*
tiling. The live session surfaced four hard constraints this SPEC is built to —
they are cited inline as **[LIVE-1..4]**.

- **[LIVE-1]** Task ordering here is **bucket-major**: each 100-task wave = one
  whole bucket arm, so the clean migration unit is **whole waves** (5 waves = 5
  buckets; no partial-array kill needed). Partial-range kill stays required for
  the *general* case.
- **[LIVE-2]** The multi-parent reduce must partition by explicit **cell
  ownership** (which parent owns which axis-values), **never blind-pool**: in the
  qdel race window the same logical cell can exist under *both* run_ids and blind
  concatenation double-counts `n`.
- **[LIVE-3]** Ordering: the **derived-leg canary must be GREEN before the
  source-array remainder is killed** — else a failed migration loses queue
  position on both clusters.
- **[LIVE-4]** The interview/`tasks.py` **singleton hazard** fires here: minting a
  derived interview rewrites the singleton the *source* run's future S4 reporter
  validates against (the executor-divergence guard reads it).

---

## 1. One-paragraph shape

`migrate-remainder` is a **read-mostly, seconds-returning, gated recovery verb**
(the `retarget-run` family, `ops/retarget_run.py:205`). Given `{source_run_id,
target_cluster}` it: **(a) censuses** the source's done-set from the cluster's
per-task terminal announcements + the sidecar `wave_map`, preferring
**wave/axis-aligned remainders** when the tiling allows [LIVE-1] and falling back
to an explicit undone task-id set otherwise; **(b) mints a derived enumerated
run** whose `task_generator` is the `enumerated` recipe over exactly the undone
task-kwargs cells (`ops/memory/interview.py:899`), materialized **per-run-scoped**
(never over the shared `.hpc/tasks.py` singleton, [LIVE-4]) and declaring
`parents=[source_run_id]` so its `node_sha` records the lineage
(`state/runs.py:672`); **(c) computes a canary-calibrated cost estimate** for the
684 cells on the target from the *source's observed* per-task runtime
(`state/runs.py:1150 read_canary_elapsed_sec` → `ops/submit/canary_calibration.py:110`
→ `infra/cost.py:125 estimate_core_hours`); and **(d) returns a migration brief**
(`needs_decision=True`, `next_block=submit-s2`) that the human `y`s through the
existing `append-decision`/`block-drive` greenlight path. The verb **actuates
nothing itself** — the destination canary + main-array launch stay behind the
reused S2/S3 gates, and the **source remainder is not killed until the derived
canary is verified GREEN** [LIVE-3]. Harvest then pulls **both parents'** per-task
results into one mirror and reduces with an **ownership map** (not the single-run
`run_id` filter) so a race-duplicated cell is counted exactly once [LIVE-2].

---

## 2. What EXISTS vs what is NEW (with citations)

| Capability | Exists today (file:line) | Gap → new work |
|---|---|---|
| Per-task terminal census | `read_announcements` COUNTS `task_*.complete` via `ls \| wc -l` (`ops/monitor/announce.py:55,84`) | It discards the **ids** — only counts survive. Need a `read_announced_task_ids` sibling + the status-reporter id map (`infra/cluster_status.py:142`) → **M-CENSUS** |
| Wave→task-id partition | `wave_map` in sidecar, `{str(w): [global task ids]}` (`ops/submit_flow.py:1177`, `build_wave_map`); resubmit maps ids→waves (`ops/recover_flow.py:299-314`) | No verb intersects **done-set × wave_map** to yield **whole undone waves** [LIVE-1] → **M-CENSUS** |
| Enumerated task generator from an explicit items list | `interview` generator-mode, `enumerated` recipe stores `items` **verbatim**, `resolve(i)=items[i]` (`ops/memory/interview.py:899-905`; wire `_EnumeratedParams.items` `_wire/actions/interview.py:76`) | Materializes to the **singleton** `.hpc/tasks.py` (`_kernel/contract/layout.py:85`), which the source reporter reads (`infra/cluster_status.py:158`) [LIVE-4]. Need **per-run-scoped** materialization (the `.hpc/wrappers/<run_name>.py` precedent) → **M-DERIVE** |
| Derived-run lineage identity | `parents` on `SubmitFlowSpec` (`_wire/workflows/submit_flow.py:326`) → `resolve_node_sha` (`state/runs.py:672`) → `node_sha=compose_node_sha(cmd_sha,[parent identities])` (`state/run_sha.py:109`); `sidecar_effective_identity` (`state/runs.py:734`) | Reused as-is; the derived run declares `parents=[source]`. `prepare_followup_specs`/`prepare_phase2_spec` are **NOT** precedents (neither derives an undone subset — followup writes tiny monitor/aggregate specs, phase2 flips two booleans and carries the SAME task set) |
| Whole-run kill | `kill` cancels **all** `record.job_ids` (`ops/monitor/kill.py:96,110`) | **No per-task/range cancel**: `_attempt_backend_cancel` finds **no** `build_cancel_cmd` on the seam → `(False,False)`, no qdel/scancel string (`ops/monitor/kill.py:41-65`, the documented BACKEND-CANCEL GAP `kill.py:14-21`) → **M-KILL** |
| Task-range **submit** expression | `build_submit_cmd(task_range="4,8,13-15", …)` already exists (`infra/backends/__init__.py:537`); global/local array-index split via `TASK_OFFSET` (`__init__.py:56-64,167-187`) | The **cancel** side has no range analogue. SGE `qdel <jid> -t <range>`, SLURM `scancel <jid>_<indices>` → **M-KILL** adds `build_cancel_cmd` |
| Cost estimate | `estimate_core_hours(tasks,walltime_s,cores)` (`infra/cost.py:125`), `footprint_unknown` honesty (`cost.py:88`); `retarget_run._cost_estimate` (`retarget_run.py:147`) | retarget uses the **full grid** + a **cold** target walltime. Need **undone count** + **source-observed** runtime via `read_canary_elapsed_sec`(`runs.py:1150`)/`calibrate_array_walltime`(`canary_calibration.py:110`)/`roll_up_quantiles`(`state/runtime_prior.py:362`) → **M-COST** (folded into M-BRIEF) |
| Single-run harvest | `aggregate_flow` reads ONE sidecar → ONE `_per_task_results` mirror (`ops/aggregate_flow.py:118`) → `reduce_metrics` value-keyed weighted-mean (`execution/mapreduce/reduce/metrics.py:134,267-273`); `run_id` F05 filter drops foreign partials (`metrics.py:265`) | Multi-parent harvest must pull from **two** remotes into one mirror and **disable** the single-run `run_id` filter, replacing it with an **ownership map** [LIVE-2] → **M-HARVEST** |
| Gated brief + `y` surface | `retarget_run` returns `needs_decision=True`+brief+`next_block={verb:"submit-s2"…}` and stamps `resolved["next_block"]="submit-s2"` (`retarget_run.py:326-328,357-374`); `assert_greenlit_target` reads it (`ops/block_gate.py:86`); `GATED_BLOCKS={submit-s2,submit-s3,submit-s4,aggregate-run}` (`infra/block_chain.py:93`) | Reused verbatim; the migration brief mirrors submit-s2's shape (`ops/submit_blocks.py:726-758`) → **M-BRIEF** |

**Net new surface:** one package `src/hpc_agent/ops/migrate/` (census, derive,
harvest, cost, the `migrate-remainder` verb), a `build_cancel_cmd` affordance on
the backend seam + a range-aware `kill`, and a `read_announced_task_ids` sibling
on `announce.py`. **No parked file is edited** (§9).

---

## 3. The flow, step by step, with refusals

Every step is a guard-that-can-fire (the engineering-principles rule).

### Step A — census the done-set (M-CENSUS)
1. `read_announced_task_ids(ssh_target, remote_path, run_id)` → the SET of ids
   whose `task_<id>.complete` marker exists (a bounded `ls`, same ACK discipline
   as `read_announcements`, `announce.py:52,86-96`). A missing announce dir / no
   ACK → **refuse** with "no per-task census present (pre-announce run or
   dispatcher never started); reconcile the source first" — never treat absence
   as "all undone."
2. Undone set = `range(total_tasks) − done_ids`. Cross-check against the
   status-reporter map (`ssh_status_report`, `cluster_status.py:142`) when the
   announce census is partial; a **disagreement** surfaces in the brief, never
   auto-masked (the aggregate-check integrity precedent, `aggregate_blocks.py:451`).
3. **Wave-alignment [LIVE-1]:** intersect the undone set with the sidecar
   `wave_map`. If every undone id falls in a set of **whole** waves, the migration
   unit is those waves (`task_range` = the wave's global ids, one array). Only when
   the remainder splits a wave do we fall to an arbitrary-id `task_range`.
   **Refuse** a census with no `wave_map` AND a non-contiguous remainder only if
   the target backend is index-bounded and cannot express the arbitrary range
   (`uses_global_array_index=False`, `backends/__init__.py:187`) — surface the
   range so the human sees the shape.
4. **Refuse** if the source is not in flight / already terminal with no undone
   tasks (nothing to migrate) — route to plain aggregate.

### Step B — mint the derived enumerated run (M-DERIVE)
1. Build the `items` list = `[source.resolve(i) for i in undone_ids]` — the exact
   task-kwargs cells, read from the source's materialized tasks. Mint an
   `InterviewSpec` with `task_generator={kind:"enumerated", params:{items:[…]}}`
   (`_wire/actions/interview.py:76`) and `task_count=len(undone)`; the materializer
   asserts `total()==task_count` and refuses on mismatch (`interview.py:370-376`) —
   the off-by-one guard.
2. **Per-run-scoped materialization [LIVE-4]:** write the enumerated `tasks.py` to
   a **per-run path** (`.hpc/migrate/<derived_run_id>/tasks.py`), following the
   `.hpc/wrappers/<run_name>.py` per-run precedent — **NEVER** overwrite the shared
   `.hpc/tasks.py` (`layout.py:85`), which the source run's future S4 reporter
   reads over SSH (`cluster_status.py:158`). If a per-run tasks path is not yet
   plumbed through deploy+reporter, the fallback is an **explicit flip-back
   sequence** disclosed in the brief (mint → deploy derived → **restore** the
   source's `.hpc/tasks.py` before the source's next reporter read). See §8: this
   is the single hardest problem, and the run-14 per-run-materialization fix (if
   it lands) is the clean resolution — carried **GATED / PLAUSIBLE-UNVERIFIED**, no
   such planned unit was found in `docs/plans/`.
3. Derived run declares `parents=[source_run_id]` → `node_sha` derived from the
   source sidecar (`resolve_node_sha`, `runs.py:672`). A **missing source sidecar
   refuses** (`runs.py:709-716`). The derived `cmd_sha` differs from the source's
   (684 items ≠ 900), so this is **not** a resume-reattach — it is a distinct
   identity whose lineage is provable.
4. **Compute the ownership map** [LIVE-2]: `{cell_key → owning_run_id}` where every
   undone cell → derived run, every done cell → source run. Derived mechanically
   from the census (no LLM). Persisted as a **migrate-scoped artifact**
   (`.hpc/migrate/<derived_run_id>/ownership.json`) rather than the run sidecar —
   the sidecar writer is a parked/forbidden seam (§9); folding it into the sidecar
   is a follow-on once the state-writer wave lands (disclosed in the brief).

### Step C — cost estimate (M-COST, folded into M-BRIEF)
`undone_count × calibrated_per_task_walltime × effective_cores / 3600`:
- per-task walltime basis = `read_canary_elapsed_sec(experiment_dir,
  <source canary run_id>)` (`runs.py:1150`), right-sized via
  `calibrate_array_walltime(canary_elapsed_sec=…, requested_walltime_sec=<target
  ceiling>)` (`canary_calibration.py:110`, shrink-only). Multi-sample alternative:
  `roll_up_quantiles(…, cluster=<source>)['quantiles'][gpu]['p95']`
  (`runtime_prior.py:362`) + `cores_used_from_sample` (`runtime_prior.py:467`).
- fed to `estimate_core_hours(total_tasks=undone_count, walltime_s=…,
  cores_per_task=…)` (`cost.py:125`). `footprint_unknown` (`cost.py:88`) → the
  brief says "unknown core-hours," never a false "0" (run #6).
- **Cross-cluster note:** the prior is **cluster-agnostic core-hours** — there is
  no per-cluster speed/scaling field in `clusters.yaml` (`infra/clusters.py`
  carries ceilings/defaults only). The estimate is disclosed as "N core-hours from
  source-observed runtime, portable to <target> as core-hours; the target's own
  history is cold-start (`needs_canary`) so the S2 canary re-calibrates."

### Step D — the migration brief + greenlight (M-BRIEF)
Return `needs_decision=True`, a persisted brief, and
`next_block={"verb":"submit-s2","why":…,"spec_hint":{"run_id":<derived>}}`,
stamping `resolved["next_block"]="submit-s2"` (mirror `retarget_run.py:326-328,
357-374`). Brief contents (mirror `submit_blocks.py:726-758` + retarget brief):
`run_id` (derived), `cluster` (target), `migrated_from:{run_id,cluster}`,
**what moves** (undone count + the wave/`task_range` shape),
**what dies** (source remainder job_ids + the range to be cancelled — but only
AFTER the derived canary is green, [LIVE-3]), `est_core_hours` + `footprint_unknown`
+ nested `cost_estimate`, `ownership_map` digest, and the census disagreement
(if any). Persist the brief (`_persist_brief`, `submit_blocks.py:105`) so the
rule-9 provenance gate can diff the `y` (`ops/decision/journal/brief_provenance.py:67`).

### Step E — ordered execution (behind the `y`, [LIVE-3])
The `y` greenlights **submit-s2** on the DERIVED run: stage + **canary** on the
target. Only when the S2 canary is verified GREEN does the migration proceed to
**kill the source remainder** (M-KILL, range-aware) and let S3 launch the derived
main array. **The source array is never killed before the derived canary passes**
— a failed migration leaves the source queue position intact. This inverts
`retarget-run`'s resolve→supersede→canary order (`retarget_run.py:36-43`), whose
supersede-first is safe only because retarget re-runs the WHOLE grid; a
remainder-migration must not sacrifice partial progress.

### Step F — multi-parent harvest (M-HARVEST)
Pull the source's done cells (from hoffman2) and the derived run's cells (from
carc) into **one** `_per_task_results`-shaped mirror, then reduce with the
**ownership map** replacing the single-run `run_id` F05 filter (`metrics.py:265`):
for each cell key, include **exactly one** parent's result dir (the owner). The
union `total = 900`; the cardinality gate (`aggregate_flow.py:731`,
`invariants.py:215 unexpected_tasks`) then passes iff ownership is exactly-once —
a race-duplicated cell present under both run_ids is dropped to its owner, so `n`
is never double-summed (`metrics.py:101-102`). M-HARVEST is a **new module** that
composes the existing per-parent pull + `reduce_metrics`; it does **not** edit the
parked `aggregate_flow.py`.

---

## 4. Doctrine check — mechanism only, the `y` gates it

- **Observe / judge / route, never actuate** (the scope doctrine): the verb
  computes the census, plan, ownership map, and estimate, and mints the derived
  run's *spec + files*, but **submits nothing**. The destination canary + launch
  are behind `submit-s2`/`submit-s3`; the source kill is behind the green-canary
  gate. It returns in **seconds** — the property that makes `retarget-run`
  MCP-safe (`retarget_run.py:315-323`) and this verb too.
- **No LLM in the numeric loop:** the undone-set (set difference), the ownership
  map (census-derived), and the cost estimate (the `cost.py`/`canary_calibration.py`
  kernels) are all **code**. The reducer computes every aggregate number
  (`metrics.py`), never the model — the framework's founding constraint.
- **The human `y` is the only authority:** the brief carries `needs_decision=True`
  and every moved/killed cell is disclosed; the `y` commits through the existing
  authorship + brief-provenance gates (`append_decision`, `brief_provenance.py:67`).
  A nudge that names a derived field is refused exactly as elsewhere.
- **Honest refusals over silent success:** absent census, missing source sidecar,
  index-bounded backend that cannot express the range, non-JSON summary artifact
  in the harvest fallback (`aggregate_flow.py:612`) — each refuses loudly with the
  remediation, never fabricates.

---

## 5. What the reducer contract requires of disjointness [LIVE-2]

`reduce_metrics` (`metrics.py:134-171`) is **value-keyed**: it concatenates
`result_dirs`, appends each `metrics.json` as one entry, and weighted-means by
`n_samples` (`metrics.py:97,101-102,267-273`). It has **no task-id keying and no
cardinality gate** — it will happily average an over-count. Therefore:

1. **Two disjoint tilings pool cleanly** iff their **task-id AND result-dir spaces
   are disjoint** — which the migration guarantees by construction: the source
   keeps the done ids, the derived run owns the undone ids, and the derived run's
   `result_dir_template` renders under its own run_id.
2. **The break condition is the qdel race window:** the source may complete a
   cell *after* census but *before* the range-kill; the derived run may also run
   it. Now the SAME cell exists under both run_ids. Blind concatenation counts its
   `n` twice.
3. **The ownership map is the fix, not the `run_id` filter.** The single-run F05
   `run_id` filter (`metrics.py:265`) would drop *all* of one parent — wrong for a
   two-parent harvest. Instead, before reduce, select `result_dirs` by
   `ownership_map[cell] == that dir's run_id`. Exactly-once per cell → `total=900`
   → the `unexpected_tasks`/cardinality invariants (`invariants.py:215`,
   `aggregate_flow.py:731`) pass. Overlap → they fire (the safety net).
4. **The reconcile/completeness layer, not the reducer, is the guard**
   (`ops/aggregate/invariants.py:195-354`, `execution/mapreduce/reduce/status.py`
   iterate `range(total)`): the migration sets `total` to the **union** and the
   ownership map to exactly-once so those `range(total)` counters agree.

---

## 6. Settled design calls (finding → binding resolution)

| # | Finding | Binding resolution (unit) |
|---|---|---|
| Δ1 | Census gives counts, not the id set (`announce.py:84`) | `read_announced_task_ids` sibling + status-reporter cross-check; absence **refuses** (M-CENSUS) |
| Δ2 | Prefer wave/axis-aligned remainders [LIVE-1] | Intersect done-set × `wave_map`; whole-wave `task_range` when aligned, arbitrary-id range otherwise (M-CENSUS) |
| Δ3 | `tasks.py` is a singleton the source reporter reads [LIVE-4] | Per-run-scoped materialization `.hpc/migrate/<rid>/tasks.py`; else explicit flip-back, disclosed (M-DERIVE); clean fix = run-14 per-run materialization (GATED/UNVERIFIED) |
| Δ4 | No range-aware cancel; backend has no `build_cancel_cmd` (`kill.py:14-21`) | Add `build_cancel_cmd(job_ids, task_range)` (SGE `qdel -t`, SLURM `scancel _idx`) + range `kill` (M-KILL) |
| Δ5 | Multi-parent reduce double-counts a raced cell [LIVE-2] | Ownership map replaces the single-run `run_id` filter; exactly-once selection before reduce (M-HARVEST) |
| Δ6 | Canary-first ordering, not supersede-first [LIVE-3] | Derived canary GREEN before source range-kill; verb kills nothing itself (M-BRIEF Step E) |
| Δ7 | Cost must reflect UNDONE count + source-observed runtime | `read_canary_elapsed_sec`→`calibrate_array_walltime`→`estimate_core_hours` over `undone_count` (M-BRIEF/M-COST) |
| Δ8 | Greenlight surface must match the gated family | `needs_decision`+brief+`next_block=submit-s2`, `resolved["next_block"]` stamped, brief persisted (M-BRIEF) |

**DECLINED:** re-using `retarget-run` directly (it re-runs the whole grid and
supersedes-first — wrong for a partial-progress remainder). Editing
`aggregate_flow.py` for the two-parent path (parked; M-HARVEST composes instead).
Folding the ownership map into the run sidecar now (state-writer seam is
parked/forbidden — migrate-scoped artifact for v1).

---

## 7. Wave plan

- **Wave M0 (pre, no swarm):** rebase every unit on the parked tree (§9). No new
  files land here.
- **Wave M1 (parallel, file-disjoint):** **M-CENSUS** (`ops/migrate/census.py` +
  `announce.py` id-sibling) · **M-KILL** (backend `build_cancel_cmd` + range `kill`).
- **Wave M2 (parallel, file-disjoint):** **M-DERIVE** (derived enumerated run +
  per-run materialization + ownership map) · **M-HARVEST** (two-parent
  ownership reduce). Both consume M-CENSUS's undone-set contract.
- **Wave M3:** **M-BRIEF** (the `migrate-remainder` verb + cost + greenlight; the
  only regen-forcing unit). Gates on M1+M2 and on the parked `parser.py` land.

Integration per wave: ordered merge → `scripts/regen_all.py --write` once →
ruff/format/mypy → per-unit batteries → push → CI matrix green → enforcement rows.

---

## 8. The single hardest problem — the tasks.py singleton [LIVE-4], §Δ3

`.hpc/tasks.py` is **one file per experiment** (`layout.py:85`), and the source
run's *cluster-side* status reporter reads it over SSH to recover per-task kwargs
(`cluster_status.py:158-159`) — that read is what the source's future S4
validation and the executor-divergence guard depend on. Minting a derived
interview the obvious way **overwrites that singleton** with the 684-item
enumerated list, so the source's next reporter walk would compute against the
wrong task set and the S4 canary-exclusion validation would diverge. This is a
**correctness** hazard, not a nuisance: it silently corrupts the still-live source
run's monitoring.

There is a precedent for per-run-scoped materialization — the shell_command path
writes `.hpc/wrappers/<run_name>.py` per run (`interview.py`, `_ShellCommandEntry`
docstring `_wire/actions/interview.py:494`) — but **deploy and the cluster
reporter are hard-wired to `.hpc/tasks.py`** (`resolve_submit_inputs.py:351`,
`cluster_status.py:158`), so a per-run tasks path is not yet plumbed end-to-end.
The clean resolution is a **per-run tasks.py materialization + deploy/reporter
threading** (the shape the coordinator called the "run-14 finding-4 per-run
materialization fix" — **no such planned unit exists in `docs/plans/` at
c9cab681**, so it is carried GATED / PLAUSIBLE-UNVERIFIED). The v1 fallback is an
**explicit, disclosed flip-back**: mint → deploy the derived tasks.py to the
target → **restore** the source's `.hpc/tasks.py` before the source's next
reporter read — correct but fragile under concurrent monitoring, which is exactly
why it must be **sequenced and disclosed in the brief**, never silent. Whichever
path lands, the derived run's identity is safe (its `node_sha` is computed at mint
from the source sidecar, `runs.py:672`, independent of the on-disk tasks.py once
its own `cmd_sha` is frozen at `interview.py:430`).

---

## 9. Wave-0 claim source of truth — parked files (FORBIDDEN)

`git status` at dispatch is the authority. Dirty at spec time — every unit that
nears one **rebases first** and treats it as owned by in-flight work:

- **Parked wave-1 aggregate/transport + submit train:** `ops/aggregate_flow.py`,
  `ops/monitor_flow.py`, `ops/submit_and_verify.py`, `ops/aggregate/combine.py`,
  `infra/transport/_combiner.py`, `infra/cluster_logs.py`, `infra/ssh_slots.py`,
  `state/describe_cache.py`, `ops/recover/doctor.py`.
- **CLI/regen surface:** `cli/parser.py`, `cli/setup.py`, `operations.json`,
  `docs/generated/operations.md`, `scripts/regen_all.py`, `agent_assets.py`.
- **WS-DAEMON D-FSYNC / D-CONTEXT + relay/D-FSYNC agents' scopes:**
  `infra/io.py`, `state/journal.py`, `state/index.py`, `state/decision_journal.py`
  (and the state writers), `_kernel/hooks/relay_audit_stop/` internals,
  `_kernel/hooks/utterance_capture.py`.

**All new work lands under the new `src/hpc_agent/ops/migrate/` package + new wire
models + new tests, plus the non-parked backend cancel seam
(`infra/backends/{__init__,sge,slurm}.py`, `ops/monitor/kill.py`,
`_wire/actions/kill.py`) and the non-parked `ops/monitor/announce.py` id-sibling.**
The ownership map is a migrate-scoped artifact, not a sidecar write, precisely to
stay off the forbidden state-writer seam. M-BRIEF's verb registration hard-gates
on the parked `parser.py` land (the Tier hook, if any, rebases onto it).
