---
name: migrate-remainder
verb: workflow
side_effects:
- writes-derived-run: <experiment>/.hpc/migrate/<derived_run_id>/ (the derived tasks.py
    + ownership.json artifact); backs up the source's shared .hpc/tasks.py
- ssh: <source-cluster> (best-effort per-task census read; non-blocking)
idempotent: true
idempotency_key: source_run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: precondition_failed
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent migrate-remainder --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.migrate.migrate_remainder.migrate_remainder
---
# migrate-remainder

Move a run's **undone** tasks to another cluster as ONE gated verb (USER DIRECTIVE
2026-07-16: *"migrate-remainder must be possible"*). The live case is xgb
`causal_tune_tree_xgb-0b5ef197` with 216/900 done on hoffman2 — move the 684
remaining to carc without re-running the 216 already finished, and without losing
the source's queue position if the migration fails. Done by hand that is "an hour of
careful surgery"; this verb composes the already-built pieces
(`ops/migrate/derive`, `ops/migrate/ownership`, `ops/migrate/cost`, the announce
census, range-aware `kill`) into one journaled decision.

Given `{source_run_id, target_cluster}` it:

1. **censuses** the source's per-task done-set from its cluster-side announce
   markers (`ops/monitor/announce.read_announced_task_ids` — the id-carrying sibling
   of the counts reader) and computes `undone = range(total) − done`. A missing
   per-task census **refuses** ("no per-task census present") — absence is never read
   as "all undone" (that would re-run every already-finished task);
2. **mints** a derived enumerated run over exactly the undone cells
   (`derive_enumerated_run`): a **per-run-scoped** `tasks.py` at
   `.hpc/migrate/<derived_run_id>/tasks.py` — NEVER the shared `.hpc/tasks.py`
   singleton the source's cluster-side reporter reads (the LIVE-4 hazard) —
   `parents=[source]` so its `node_sha` records the lineage, and a **cell-ownership
   map** for the eventual two-parent harvest (so a qdel-race-duplicated cell is
   counted exactly once);
3. **estimates** the migration's footprint over the **undone count** from the
   **source-observed** canary runtime (`estimate_migration_cost`:
   `read_canary_elapsed_sec` → `calibrate_array_walltime` → `estimate_core_hours`),
   with `footprint_unknown` honesty — the brief says "unknown core-hours" rather than
   a false "0" (proving run #6);
4. **returns** a persisted migration brief (`needs_decision=True`,
   `next_block=submit-s2`, `resolved["next_block"]="submit-s2"` stamped so
   `assert_greenlit_target` reads it) the human `y`s through the existing
   `append-decision` path.

**It actuates nothing itself and returns in seconds** — the `retarget-run` MCP-safe
contract. The census read is best-effort, no canary runs inline, and the source
array is **not** killed here.

## The ordering invariant (canary-first, not supersede-first)

The `y` greenlights **submit-s2** to stage & canary the DERIVED run on the target.
Only when that canary is verified **GREEN** does the migration proceed to kill the
source remainder (range-aware `kill` over the undone `task_range`) and launch the
derived main array. This **inverts `retarget-run`'s supersede-first order**
([LIVE-3]): `retarget-run` re-runs the WHOLE grid, so superseding the old attempt
first is safe; a remainder-migration must not sacrifice partial progress, so a
failed migration leaves the source's queue position intact on **both** clusters. The
brief's `what_dies` block carries `killed_only_after_derived_canary_green: true` to
make the ordering auditable.

## What is NOT this verb

- **`retarget-run`** re-runs the *whole* grid on a new cluster and supersedes the old
  attempt first — for a genuine cluster *move* of a fresh grid. `migrate-remainder`
  moves only the *undone* tasks and kills nothing until the derived canary is green.
- **`revise-resolved`** changes resources on the *same* cluster. A same-cluster
  `target_cluster` is REFUSED here (nothing to migrate).

## The tasks.py singleton hazard (LIVE-4) and the flip-back

`.hpc/tasks.py` is one file per experiment, and the source run's cluster-side status
reporter reads it over SSH to recover per-task kwargs. Minting the derived interview
the obvious way would overwrite that singleton with the 684-item list and silently
corrupt the still-live source's monitoring. So `derive` materializes the derived
`tasks.py` to a **per-run path** and backs up the shared singleton executably; the
brief discloses a **flip-back sequence** (mint → deploy derived → restore the
source's `.hpc/tasks.py` before its next reporter read) because deploy + reporter are
not yet plumbed for a per-run tasks path. The clean resolution (per-run
materialization threaded through deploy + reporter) is carried
**GATED / PLAUSIBLE-UNVERIFIED** — no such planned unit exists at baseline.

## Inputs

- `source_run_id` (str) — the in-flight run whose UNDONE tasks migrate. Its sidecar
  supplies `task_count` / `cluster` / `resources` / `wave_map`; its announce markers
  are the per-task done-set.
- `target_cluster` (str) — the cluster the derived remainder run lands on. MUST
  differ from the source's cluster.
- `produced_by` (dict, optional) — authorship stamp threaded onto the minted derived
  `InterviewSpec`; defaults to the migrating operator.

## Refusals (guards that can fire)

- a missing source sidecar (no run to migrate);
- a same-cluster / clusterless `target_cluster` (nothing to migrate);
- a source with no per-task census (reconcile the source first);
- a source with an empty undone set (all tasks done — route to aggregate/harvest).

## It does not bypass the gates

The returned brief carries `needs_decision=True`; the human `y`s it through the
EXISTING `append-decision` path (the authorship + brief-provenance gates run on the
commit), the canary (#160) runs in submit-s2's detached worker, the source range-kill
waits for a GREEN canary, and the derived main array stays behind the S3 greenlight.
`migrate-remainder` only censuses, mints the derived run's spec + files + ownership
map, estimates the cost, and hands off to `submit-s2`.
