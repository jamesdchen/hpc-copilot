---
name: hpc-aggregate
description: "Aggregate finished HPC runs into a final metrics envelope. Walks resolution steps, accumulates ambiguities into a single envelope, refuses to auto-mask integrity issues. Composes the aggregate-flow worker for the combiner + reducer pipeline."
allowed-tools: Bash Read Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[aggregate-flow](../../../../docs/primitives/aggregate-flow.md) workflow**.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `profile` | Caller, or auto-discover from `load-context.data.runs` |
| `stage` | Caller, or default to the latest stage in the run's multi-stage DAG |
| `run_id` | Caller, or auto-resolve to the latest terminal run for the profile |
| `allow_partial` | Caller (default `false`) |

## The resolution contract

Same as `hpc-submit`: walk every step, accumulate ambiguities, return all in one envelope.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

### 2. Resolve profile + run_id + stage

- Caller supplied profile → use.
- Else single profile with terminal runs → use.
- Else multiple profiles → add to ambiguities:
  ```json
  {"field": "profile", "candidates": [<profile list>], "depends_on": [], "safe_default": "<most recent>"}
  ```
- Else zero terminal runs → return `spec_invalid: nothing_to_aggregate`.

For the chosen profile, pick the latest terminal `run_id` unless caller pinned one. `stage` defaults to the run's final stage.

### 3. Verify aggregation readiness

```bash
hpc-agent verify-aggregation-complete --run-id <id> --experiment-dir <dir>
```

Branch on result:

- `complete: true` → continue to Step 5.
- `complete: false, missing_waves: [...]`:
  - If `allow_partial: true` (caller) → proceed; record in decisions.
  - Else add to ambiguities:
    ```json
    {
      "field": "allow_partial",
      "candidates": [true, false],
      "depends_on": [],
      "safe_default": false,
      "context": {"missing_waves": [...], "complete_waves": N, "total_waves": M}
    }
    ```
    Safe_default is `false` — refuse partial aggregation by default; partial usually masks real cluster issues.
- `integrity_violation: <code>` → return `spec_invalid: integrity_violation` with the code + evidence (NOT an ambiguity — these need human investigation, not a default).

### 4. Return ambiguities if any

If accumulated, return `needs_resolution` envelope. Caller resolves; re-invokes.

### 5. Hand off to the aggregate-flow worker

```bash
hpc-agent run --workflow aggregate --fields-json '{"run_id": "<id>", "profile": "<p>", "stage": "<s>", "allow_partial": <bool>}'
```

Spawns a fresh-context bare worker that reads `worker_prompts/aggregate.md` — runs the combiner + reducer + summary pull + runtime-sample ingestion. Multi-step LLM-driven workflow, so worker is justified.

### 6. Return envelope

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics. The caller has to explicitly resolve `allow_partial: true` after understanding what's missing.
- **Integrity violations are not auto-fixable.** A missing sidecar means the per-task metrics never landed — bug needs to be found. Returns `spec_invalid`, not `needs_resolution`.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
- **MARs pattern**: invoke after `hpc-status` returns `complete`. The skill auto-discovers the latest terminal run and aggregates; experiment-runner reads `results/metrics.json`.
