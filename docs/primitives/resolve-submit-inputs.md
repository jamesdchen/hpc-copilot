---
name: resolve-submit-inputs
verb: workflow
side_effects:
- writes-sidecar: <experiment>/.hpc/tasks.py (when scaffolded)
- writes-sidecar: <experiment>/.hpc/cli.py (when scaffolded)
- writes-sidecar: <experiment>/.hpc/runs/<run_id>.json (the per-run sidecar)
idempotent: true
idempotency_key: submit.resolve.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent resolve-submit-inputs --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.resolve_submit_inputs.resolve_submit_inputs
---
# resolve-submit-inputs

The deterministic submit **input-resolution** chain as one call. Folds
`worker_prompts/submit.md` Steps 6a-6d — scaffold-or-reuse `.hpc/tasks.py`,
compute the run_id, detect a resumable prior run, assemble the validated
submit-flow spec, and write the per-run sidecar — into a single workflow
primitive that runs the branch logic in code and returns one typed outcome plus
a **fully submit-ready** context (spec built **and** sidecar on disk).

## Why this exists

The input-resolution chain is mechanical: each step is a verb call followed by
a deterministic branch on its result. Ensure [build-tasks-py](build-tasks-py.md)
has scaffolded `.hpc/tasks.py` (or reuse it), run
[compute-run-id](compute-run-id.md) to derive `run_id` + `cmd_sha`, run
[find-prior-run](find-prior-run.md) and branch on the resume contract, then run
[build-submit-spec](build-submit-spec.md) to assemble the submit-flow spec. That
is control flow the agent was hand-walking. `resolve-submit-inputs` runs it, so
the agent's role shrinks from "call four verbs and branch on each" to "call one
verb and read `stage_reached`". It is the [submit-pipeline](submit-pipeline.md)
pattern applied one ring earlier — the laptop-side input spine rather than the
cluster-side submit spine.

## Composition

```
compute-run-id  →  find-prior-run  →  (build-tasks-py if tasks.py absent)
                →  build-submit-spec  →  write-run-sidecar
```

The whole chain runs **on the laptop** — no cluster, no SSH (`requires_ssh:
false`). Ordering note: `compute-run-id` hashes `.hpc/tasks.py`, so tasks.py
must exist before it runs; the composite therefore ensures tasks.py first
(scaffold via `build-tasks-py`, or escalate) and then computes the run_id —
the submit.md Step 6a/6b-before-6c order. The computed `run_id` / `cmd_sha`
are injected into the `build-submit-spec` **and** `write-run-sidecar` inputs
(whose own `run_id` / `cmd_sha` are placeholders), so the built spec and the
written sidecar always match the reported run_id. `write-run-sidecar` runs only
on the `resolved` path, after the resume check clears — never for a prior /
escalation outcome.

## Inputs / outputs

See `hpc_agent/schemas/resolve_submit_inputs.{input,output}.json`. The input
carries the already-resolved values: a `run_name` (drives `compute-run-id`), the
full `BuildSubmitSpecInput` under `submit` and the `WriteRunSidecarInput` under
`sidecar` (re-used verbatim so cluster / profile / backend / job_env / the real
per-task executor are not re-enumerated; their `run_id` / `cmd_sha` are
placeholders the composite overrides), and an optional `BuildTasksPyInput` under
`build_tasks` used only when `.hpc/tasks.py` is absent.

The output carries a single `stage_reached` ∈ `{resolved, prior_run_found,
needs_scaffold_interview}` and a `needs_decision` flag, plus `run_id`,
`cmd_sha`, the built `submit_spec` + the written `sidecar_path` (on `resolved`),
and the `prior_run_id` / `prior_status` resume context (on `prior_run_found`).
This is escalation-as-data (#231): the composite runs every deterministic branch
and sets `needs_decision=True` only on `prior_run_found` (the user picks
resume-vs-fresh) and `needs_scaffold_interview` (the headless worker can't run
the scaffold sub-interview). `resolved` is the clean terminal — spec built and
sidecar written (#171), so it hands straight to
[submit-pipeline](submit-pipeline.md).

A `find-prior-run` hit that is a terminal-but-not-`complete` record
(`failed` / `abandoned`, #276) is forensic, not a live prior — the chain
proceeds as fresh and the spec is built over it, mirroring submit.md Step 6c.

## What stays in the LLM

The genuine judgement that *precedes* this spine is NOT folded in: parsing the
user's natural-language intent (Step 2), classifying the data-axis when
unresolved (Step 3, via [classify-axis](classify-axis.md)), and environment
selection (Step 4). Those stay upstream as escalations. `resolve-submit-inputs`
runs only once they are resolved and the caller hands it the resolved values.

## Additive

This primitive does not replace the per-verb worker-prompt path; it is a new
verb the prompt may adopt. Nothing breaks if it is not yet wired in — which is
why it can ship before the prompt is restructured to call it.
