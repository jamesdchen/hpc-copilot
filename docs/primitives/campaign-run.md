---
name: campaign-run
verb: workflow
side_effects:
- scheduler-submit: <cluster>
- ssh: <cluster> (canary poll + status polls + aggregate pull)
- writes-aggregate-output: <experiment_dir>/_aggregated/<run_id>/ (+ follow-up specs,
    tick log)
idempotent: true
idempotency_key: campaign.run.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent campaign-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.campaign_run.campaign_run
---
# campaign-run

One campaign iteration's deterministic **spine** as a single call. Folds the
three-stage iteration spine — submit, then monitor, then aggregate — into one
composite-of-composites that runs the branch logic in code and returns one
typed outcome.

## Why this exists

A campaign iteration's mechanical remainder is the same every time: run the
submit spine, branch on whether it landed; if it landed, monitor to terminal,
branch on the lifecycle; if it completed, aggregate, branch on whether the
results came back clean. That is control flow the driver was hand-walking
across three separate composites. `campaign-run` runs it, so the driver's role
per iteration shrinks from "call three composites and branch on each" to "call
one verb and read `stage_reached`". It is the
[submit-pipeline](submit-pipeline.md) / [status-pipeline](status-pipeline.md)
pattern applied one ring further out — those each fold a single workflow's
spine; `campaign-run` chains all three.

## Composition

```
submit-pipeline  →  status-pipeline  →  aggregate-flow
```

All three are `ops`-subject verbs, so the composite needs no cross-subject
import. The stages run in order and short-circuit: a submit-gate failure never
monitors; only a `complete` lifecycle proceeds to aggregate. A `deduped`
submit still proceeds to monitor — dedup means the run already exists / is
live, so there is an existing run to watch.

## Inputs / outputs

See `hpc_agent/schemas/campaign_run.{input,output}.json`. The input embeds the
three sub-composite specs verbatim — `submit` (a full `SubmitPipelineSpec`),
`status` (a full `StatusPipelineSpec`), and `aggregate` (a full
`AggregateFlowSpec`) — plus an optional `campaign_id` iteration tag carried
through to the result.

The output carries a single `stage_reached` ∈ `{submit_failed, run_failed,
run_timeout, run_abandoned, aggregate_failed, complete}` and a `needs_decision`
flag, plus
pass-through context: `run_id`, `job_ids`, `lifecycle_state`, and the
aggregate summary under `aggregate_result`. This is escalation-as-data (#231):
the composite runs every deterministic branch and sets `needs_decision=True`
only on the failure / budget stages. `complete` is the clean terminal.

The branch table the composite implements:

| sub-stage outcome | `stage_reached` | `needs_decision` | next move |
| --- | --- | --- | --- |
| submit `canary_failed` / `verify_submitted_failed` | `submit_failed` | true | fix dispatch, re-invoke (never monitor) |
| submit `deduped` / `complete` | _(proceeds to monitor)_ | — | run the status spine |
| status `failed` | `run_failed` | true | classify failed tasks, resubmit / reconcile |
| status `abandoned` | `run_abandoned` | true | reconcile-journal before re-submitting |
| status `timeout` | `run_timeout` | true | budget elapsed, jobs live — re-invoke to keep watching |
| status `complete` | _(proceeds to aggregate)_ | — | run the aggregate spine |
| aggregate raises / partial (`escalation_reason`) | `aggregate_failed` | true | inspect failed_waves, re-invoke aggregate |
| aggregate clean | `complete` | false | hand back to the driver |

`timeout` gets its own `stage_reached="run_timeout"` — distinct from
`run_failed`, because nothing failed: the wall-clock budget elapsed with the
jobs still live, so the run simply hasn't reached `complete` yet and can't
aggregate. `needs_decision=True` (the driver decides whether to re-invoke and
keep watching, extend the budget, or stop), and the `reason` makes the
budget-only nature explicit so it is never treated as a genuine failure.

## What stays in the LLM

The campaign CURSOR / manifest advancement — advance vs. converge, budget
accounting, target checks — is NOT in this composite. `campaign-run` runs ONE
iteration's spine and returns; the advance/converge judgement that consumes a
`complete` outcome stays a driver escalation. So do the per-stage judgements it
flags with `needs_decision=True`: classifying a failed run, reconciling an
abandoned one, deciding whether a partial aggregate is acceptable.
`campaign-run` hands these back as data (`lifecycle_state`, `aggregate_result`,
`reason`) but does not itself decide them.

## Additive

This primitive does not replace the per-composite path; it is a new verb the
driver may adopt. Nothing breaks if it is not yet wired in — which is why it
can ship before the campaign driver is restructured to call it.
