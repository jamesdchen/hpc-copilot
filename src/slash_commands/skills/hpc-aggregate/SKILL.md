---
name: hpc-aggregate
description: "Aggregate finished HPC runs into a final metrics envelope. Autonomous: picks profile/stage from on-disk state, handles partial-aggregation conservatively (refuse rather than mask incomplete waves), surfaces integrity violations as structured spec_invalid. Callers may pre-resolve profile + stage; otherwise the skill auto-discovers. The /aggregate-hpc slash invokes this skill after the user confirms what to aggregate; an external autonomous agent (MARs experiment-runner) invokes it directly after monitor reports terminal."
allowed-tools: Bash Read Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[aggregate-flow](../../docs/primitives/aggregate-flow.md) workflow**. This skill resolves the choices an aggregation requires — which profile + stage, whether to proceed on partial data, how to handle integrity violations — and hands off to `hpc-agent run aggregate` for the actual combiner + reducer pipeline.

## Inputs

| Field | Default behaviour if absent |
|---|---|
| `experiment_dir` | Required |
| `profile` | Auto-discover via `load-context`. Single profile with terminal runs → use it. Multiple → `spec_invalid: ambiguous_profile` with candidates. |
| `stage` | Default to the latest stage in the multi-stage DAG. Single-stage runs (the common case) omit it. |
| `run_id` | Auto-resolve from the latest terminal run for the profile. Caller may pin a specific run. |
| `allow_partial` | Default `false`. When `true`, aggregate even if some waves are incomplete; mark the envelope `partial: true`. |

## Mode

- **`mode: "interview"`** — caller passes user-resolved values.
- **`mode: "autonomous"`** (default) — auto-resolve and never return `needs_human`. On ambiguity, pick the latest terminal run and record the choice; do not auto-set `allow_partial=true` (that decision needs the operator's judgment).

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

Examine `data.runs` (terminal runs grouped by profile) and `data.next_step_hint`.

### 2. Resolve profile + stage + run_id

- Single profile with terminal runs → use it. Multiple → `spec_invalid: ambiguous_profile` with `candidates` (interview mode), or pick the most recent + record (autonomous mode).
- For the chosen profile, pick the latest terminal `run_id` unless the caller pinned one.
- `stage`: default to the run's final stage from its sidecar.

### 3. Verify aggregation readiness

Run [`verify-aggregation-complete`](../../docs/primitives/verify-aggregation-complete.md) before invoking the combiner:

```bash
hpc-agent verify-aggregation-complete --run-id <id> --experiment-dir <dir>
```

Branch on the result:

| Result | Skill behaviour |
|---|---|
| `complete: true` | Proceed to Step 4. |
| `complete: false, missing_waves: [...]` (some waves not yet done) | If `allow_partial=true`: record the missing waves in decisions and proceed. Otherwise: return `spec_invalid: incomplete_aggregation` with `missing_waves` and the count. Autonomous mode refuses partial — partial aggregation usually masks real cluster issues. |
| `integrity_violation: <code>` | Return `spec_invalid: integrity_violation` with the code + evidence. Autonomous mode does NOT auto-fix integrity violations (missing sidecars, duplicate task results, NaN gates) — these need human investigation. |

### 4. Hand off to the aggregate-flow worker

```bash
hpc-agent run aggregate --fields-json '{"run_id": "<id>", "profile": "<p>", "stage": "<s>", "allow_partial": <bool>}'
```

Spawns a fresh-context worker that reads `worker_prompts/aggregate.md` and runs the combiner + reducer pipeline (rsync_pull _combiner/, reduce_partials, optional summary pull, ingest runtime samples). Returns the aggregated metrics envelope.

### 5. Return the envelope

Surface to the caller:
- `data.report.result` (aggregated metrics + summary + ingested runtime samples)
- `data.report.decisions` (which profile/stage/run_id chosen, partial flag, integrity status)
- `data.report.anomalies` (any reducer warnings — missing waves treated as informational when allow_partial, NaN inputs, etc.)

## Notes

- **Refuse rather than mask.** Aggregating on incomplete waves silently produces wrong final metrics. Autonomous mode refuses by default; the user has to opt in via `allow_partial=true` after understanding what's missing.
- **Integrity violations are not auto-fixable.** A missing sidecar means the per-task metrics never landed — the underlying bug needs to be found. Autonomous mode surfaces these and stops.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output; the combiner is content-addressed.
- **MARs experiment-runner pattern**: invoke with `{experiment_dir, run_id, mode: "autonomous"}` after `hpc-status` returns `lifecycle_state: complete`. Receives the aggregated metrics envelope; reads `results/metrics.json` into MARs's experiment journal.
