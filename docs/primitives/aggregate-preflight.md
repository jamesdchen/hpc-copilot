---
name: aggregate-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent aggregate-preflight --experiment-dir <experiment_dir> [--reconcile-scheduler
    <reconcile_scheduler>]
  python: hpc_agent.ops.aggregate_preflight.aggregate_preflight
---
# aggregate-preflight

Composite preflight at the top of every `hpc-aggregate` invocation: runs
`install-commands` → `load-context` → (conditionally) `reconcile` as one
CLI call. Third and final member of the `<skill>-preflight` family,
after `submit-preflight` and `status-preflight`.

## Inputs / outputs

See `hpc_agent/schemas/aggregate_preflight.{input,output}.json`. Input
requires only `experiment_dir`; `reconcile_scheduler` is optional. Output
carries a `SubResult` per fanned-out sub-call under
`data.install_commands`, `data.load_context`, `data.reconcile`.

## Internal composition

Sequential, plain `subprocess.run`. `install-commands` must succeed
before `load-context` can resolve framework paths reliably; `reconcile`
runs last (and only conditionally) because it's the SSH-touching call and
we want the cheap local checks to fail-fast.

## The reconcile branch

This is the structural twist that distinguishes aggregate from its two
siblings. Where `submit-preflight`'s `check-preflight` argv is known
statically from `--cluster`, the reconcile sub-call is assembled from
`load-context`'s *runtime output* — it can't be pre-composed up front.

reconcile fires only when ALL hold:

- `--reconcile-scheduler` was supplied (reconcile needs the scheduler
  family to query alive job IDs), and
- `load-context` returned `ok` with `data.next_step_hint == "monitor"`
  (the journal still says a run is in flight), and
- `data.in_flight` carries at least one `run_id` to target.

When it fires, the verb runs
`hpc-agent reconcile --run-id <first in_flight run_id> --scheduler
<reconcile_scheduler> --experiment-dir <dir>`. A single reconcile call
also settles that run's paired `-canary` sibling (#258), so one sub-call
clears both journal entries. When the branch does not fire, the
`data.reconcile` slot stays `null`.

This mirrors `hpc-aggregate` SKILL.md Step 1b: the journal lags the
cluster, so a run the journal still marks `monitor` may have actually
terminated, failed, or been purged. Reconciling against live cluster
state before the skill trusts the journal is the symmetric recovery to
`hpc-submit`'s `already_in_flight` step.

## Failure semantics

`overall: "pass"` iff every sub-call that actually ran returned
`ok: true`. Any sub-call returning `ok: false` flips `overall: "fail"`.
A reconcile branch that never fired does not affect the verdict (it is
`null`, not a failure). The composite itself returns `ok: true` at the
outer envelope; the failing sub-call's verbatim envelope is preserved
under `data.<subcall>.envelope` so the caller can read its `error_code`
+ `remediation` without re-running.

Sibling work is preserved on failure — a `reconcile` failure doesn't lose
the install-commands or load-context results.

## Why this exists

The agent's prose-discipline at the top of every `hpc-aggregate` used to
be: "Step 0: install-commands. Step 1: load-context. Step 1b: reconcile
if the journal-only run looks in-flight." Folding all three into one verb
makes the Step 0 omission structurally impossible and removes the
prose-discipline seam where the agent had to decide whether to reconcile.
