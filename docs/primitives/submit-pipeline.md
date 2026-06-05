---
name: submit-pipeline
verb: workflow
side_effects:
- scheduler-submit: <cluster>
- ssh: <cluster> (canary poll + post-qsub state)
- writes-followup-specs: <experiment_dir>/{monitor,aggregate}_spec.json
idempotent: true
idempotency_key: submit.submit.run_id
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
  cli: hpc-agent submit-pipeline --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_pipeline.submit_pipeline
---
# submit-pipeline

The deterministic post-resolution submit **spine** as one call. Folds
`worker_prompts/submit.md` Steps 7-8 → 9-10 — the canary-gated submit, the
post-qsub health check, and the follow-up-spec pre-staging — into a single
workflow primitive that runs the branch logic in code and returns one typed
outcome.

## Why this exists

Those steps are mechanical: each is a verb call followed by a deterministic
branch on its envelope (`deduped` → switch to status; canary fail → stop;
`verify-submitted` not ok → stop; else report + pre-stage). That is control
flow the agent was hand-walking. `submit-pipeline` runs it, so the agent's
role shrinks from "walk four verbs and branch on each" to "call one verb and
read `stage_reached`".

It is the pattern [submit-and-verify](submit-and-verify.md) started, one ring
out: `submit-and-verify` absorbed the submit→verify→submit canary sub-loop;
`submit-pipeline` absorbs the spine around it.

## Composition

```
submit-and-verify  →  verify-submitted  →  prepare-followup-specs
```

All three are `ops`-subject verbs, so the composite needs no cross-subject
import. The campaign-only `validate-campaign` gate is deliberately left out
(it lives in the `meta` subject and is campaign-specific).

## Inputs / outputs

See `hpc_agent/schemas/submit_pipeline.{input,output}.json`. The input embeds a
full `SubmitAndVerifySpec` under `submit` (which itself embeds the
`submit-flow` spec under `submit.submit`), plus an optional `profile` forwarded
to `prepare-followup-specs`.

The output carries a single `stage_reached` ∈ `{deduped, canary_failed,
verify_submitted_failed, complete}` and a `needs_decision` flag. This is
escalation-as-data (#231): the pipeline runs every deterministic branch and
sets `needs_decision=True` only on the genuine gate failures (a canary that
failed verification, or submitted jobs that did not land clean). `deduped` and
`complete` are terminals the caller just reports.

## What stays in the LLM

The judgement points UPSTREAM of this spine — axis classification, entry-point
resolution, environment selection — are NOT in the pipeline. They escalate
before it runs, once every input is resolved. `submit-pipeline` is the
deterministic remainder.

## Additive

This primitive does not replace the per-verb worker-prompt path; it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in — which is
why it can ship before the prompt is restructured to call it.
