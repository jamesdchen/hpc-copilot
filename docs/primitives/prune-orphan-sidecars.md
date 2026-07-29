---
name: prune-orphan-sidecars
verb: mutate
side_effects:
- removes-files: <experiment>/.hpc/runs/*.json (orphans only)
idempotent: true
idempotency_key: experiment_dir
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.state.runs.prune_orphan_sidecars
---
# prune-orphan-sidecars

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `submit-flow`'s batch path composes it. The agent
> surface for the same effect is re-running `submit-flow`.

Delete every orphan sidecar under `<experiment>/.hpc/runs/`. An
*orphan* is a sidecar with no journal record (the run was never
recorded in `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`),
typically left behind by a `submit-flow-batch` invocation that
crashed mid-loop after writing the per-spec sidecar but before the
journal record. Returns the list of pruned `run_ids` for caller
logging.

## Composers

- **`submit-flow-batch`** — auto-invokes it at start-up when it detects
  half-baked sidecars.
- The `/submit-hpc` slash command *references* the primitive when
  `find-prior-run` reports `is_orphan=true`: it surfaces the cleanup hint to
  the user rather than silently mutating disk. That is a prose reference, not
  an invocation — there is no verb for the agent to call.

## Contract surface

Python-only signature — no wire spec, not exposed via `--spec`. Takes
`experiment_dir` (path, the experiment root) and returns `list[str]`: the
run_ids whose sidecars were removed, empty in the common case.

## Invariants

- **Orphans only.** A sidecar is removed only when no corresponding journal
  record exists. A sidecar with a journal record is never touched.
- **Sidecar tree only.** The primitive does NOT touch the journal. Journal
  entries with no surviving sidecar are a separate consistency class, handled
  by `reconcile-journal`.
- **Repeatable.** Safe to invoke at any time; a second call after a successful
  one returns `[]`. Idempotency key: `experiment_dir`.

## Coupling

- Depends on the journal-record location contract
  (`~/.claude/hpc/<repo_hash>/runs/<run_id>.json`) to decide what counts as an
  orphan. A change to where journal records live must land here too, or every
  sidecar starts looking orphaned.
- Paired with `reconcile-journal`, which handles the mirror-image
  inconsistency. Neither should grow into the other's half.

## Failure modes

- **A run mid-submit looks orphaned.** The sidecar is written before the
  journal record, so a concurrent submit is briefly indistinguishable from a
  crashed one. The caller guards this with a minimum-age threshold
  (`_PRUNE_ORPHAN_MIN_AGE_SECONDS`) and an `exclude` set — `submit-flow` never
  prunes the `run_id` it is currently submitting.
- **Journal store unreadable** → every sidecar classifies as an orphan. The
  composer, not this primitive, owns establishing that the journal is readable
  before pruning.
