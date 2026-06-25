---
name: scaffold-strategy
verb: scaffold
side_effects:
- writes-file: <output_dir>/.hpc/tasks.py (refuses to overwrite without --force)
idempotent: true
idempotency_key: output_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent scaffold-strategy --name <name> [--output-dir <output_dir>] [--force]
  python: hpc_agent.incorporation.scaffold_strategy.scaffold_strategy
exit_codes:
- 0: ok
- 1: spec_invalid
---

# scaffold-strategy

Materialize a **correctly-wired closed-loop campaign strategy** into the
experiment repo. Sibling of [build-executor](build-executor.md): it copies a
bundled template (`optuna_strategy.py` or `pbt_strategy.py`) byte-for-byte to
`<output_dir>/.hpc/tasks.py`. The agent then customizes **only the search
space** — never the ask/tell plumbing, the `trial_token` round-trip, or the
`_propose`/`resolve` split.

Reach for this whenever the sweep is **adaptive** — ask/tell, Bayesian
optimization (Optuna/Ax), population-based training, walk-forward — i.e. when a
later iteration's parameters depend on earlier iterations' results. That is the
[hpc-campaign](../../src/slash_commands/skills/hpc-campaign/SKILL.md) territory;
do not hand-roll an `sbatch` controller and do not `Read` the framework's
strategy source to infer the contract — it is stated, load-bearing, in that
skill's **Strategy authoring contract** section.

## Inputs

- `name` (enum, required): `optuna` (scalar-objective ask/tell) or `pbt`
  (artifact-carrying population-based training).
- `output_dir` (path): experiment repo root. Defaults to cwd. The strategy
  lands at `<output_dir>/.hpc/tasks.py`.
- `force` (bool): overwrite an existing `.hpc/tasks.py`. Default `false`.

## Outputs

`{path, name, source, output_dir}` — the absolute path written, the strategy
name, the absolute template path it was copied from, and the resolved repo
root.

## Errors

- `spec_invalid`: unknown `--name`; `output_dir` missing / not a directory;
  template missing on disk (packaging bug — surface to caller); destination
  `.hpc/tasks.py` already exists and `--force` was not passed.

## Idempotency

Keyed on `output_dir`. A re-run with the same `output_dir` and `--force` is a
byte-faithful re-materialization (the template is the single source); without
`--force` it refuses-to-clobber, so a customized strategy is never silently
overwritten.

## What the template already wires (do not reinvent)

The materialized `tasks.py` is the strategy. It already encodes the campaign
authoring contract end-to-end — see the **Strategy authoring contract** section
of the `hpc-campaign` SKILL for the full statement:

1. **ask/tell run ONLY on the orchestrator.** `_propose` tells finished trials,
   asks the next, and persists strategy state (e.g. the Optuna SQLite under
   `.hpc/campaigns/<cid>/`). Compute nodes call **only** `resolve(task_id)`,
   which reads the already-decided proposal. The optimizer import is local to
   `_propose`, so a compute node never imports it.
2. **`trial_token` is reserved bookkeeping.** It is round-tripped through
   `resolve()` to the sidecar, stripped from `cmd_sha` (parameter identity), but
   **still exported** as an `HPC_KW_*` env var. It is the out-of-order tell key.
   Everything else `resolve(i)` returns reaches the executor as `HPC_KW_*`.
3. **Per-trial metrics flow back via the reduce.** A batch iteration's reduce
   emits **per-trial** metrics keyed by `trial_token`, not one scalar — that is
   what lets the next `_propose` tell each trial individually.
4. **The campaign loop is synchronous-batched by design.** One step per driver
   tick; concurrency within a batch comes from `decide-concurrency`, not from an
   async refill loop. Continuous-async refill is a separate, deferred feature
   (issue #362) — not implemented here.

## Compose with

- Predecessors: `build-template` / `wrap-entry-point` (the executor + repo
  scaffold the strategy's `resolve()` kwargs feed).
- Successors: `validate-campaign`, then the `hpc-campaign` tick loop
  (submit → monitor → aggregate → decide).
