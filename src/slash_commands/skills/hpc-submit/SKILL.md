---
name: hpc-submit
description: "Decide all HPC submission inputs (cluster, entry_point, data_axis, homogeneous_axes, frozen_configs, task_generator, walltime, gpu_type) and hand off to the submit-flow worker. Walks every resolution step, accumulates ambiguities into a single envelope, never early-returns on the first miss. Callers (slash for human dialogs, autonomous agent applying safe_defaults) resolve the entire list in one re-invocation. Composes hpc-classify-axis / hpc-wrap-entry-point / hpc-build-executor for sub-decisions."
allowed-tools: Bash Read Write Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[submit-flow](../../../../docs/primitives/submit-flow.md) workflow**. This skill is the *experiment-aware* decision surface: it walks the choice points an HPC submission requires (which cluster? which executor? which axis classification?) and resolves each one — from caller-supplied input, autonomous heuristics, or composed sub-skill. Once everything's resolved, it shells out to `hpc-agent run --workflow submit`, which spawns a fresh-context worker that runs `worker_prompts/submit.md` — the experiment-agnostic execution layer (rsync, qsub, canary, journal, scheduler verify).

The slash `/submit-hpc` is the human-interview wrapper around this skill; an external autonomous agent (MARs experiment-runner, notebook driver) invokes this skill directly with whatever it pre-resolved.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required (absolute path) |
| `cluster` | Caller, or auto-resolve from `clusters.yaml` (single configured → use it; multiple → ambiguous) |
| `entry_point` (kind + path + run_name) | Caller, or invoke `hpc-wrap-entry-point` sub-skill if no `@register_run` on disk |
| `data_axis` | Caller, or invoke `hpc-classify-axis` sub-skill if no classification for current run_signature_sha |
| `homogeneous_axes` | Caller, or invoke `hpc-build-executor` (axes-init companion) if no `.hpc/axes.yaml` |
| `frozen_configs` | Caller, or detect from `configs/*.yaml` |
| `task_generator` | Caller (REQUIRED if no existing `tasks.py`; cannot be auto-invented) |
| `walltime_sec` | Caller, or auto-resolve from runtime priors (p95 × safety_mult) |
| `gpu_type` | Caller, or first GPU in `clusters.<cluster>.gpu_types` |
| `no_canary` | Caller (default `false`) |
| `campaign_id` | Caller (pass-through) |

## The resolution contract

The skill walks every resolution step in dependency order. Each step does ONE of:

- **Resolve from input** — caller supplied the field; accept it as authoritative.
- **Auto-resolve** — apply a deterministic rule or compose a sub-skill that resolves it.
- **Add to ambiguities** — record the unresolved field with candidates + dependency info + safe_default, and continue walking subsequent steps.

When all steps are done, the skill behaves as follows:

- **No ambiguities accumulated** → all fields resolved → proceed to Step 8 (handoff to worker).
- **Ambiguities accumulated** → return `needs_resolution` envelope with the full list (Step 7).

This means **one round-trip per workflow invocation** — the caller resolves every ambiguity in the returned list at once and re-invokes. No N-way escalation loop.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

If `data.next_step_hint == "monitor"`, return `spec_invalid: already_in_flight` with the run_id (different from `needs_resolution` — this isn't an ambiguity, it's a state conflict).

### 2. Resolve cluster

- Caller supplied → use.
- Else single configured cluster in `clusters.yaml` → use.
- Else (multiple, no pick) → add to ambiguities:
  ```json
  {"field": "cluster", "candidates": [...], "depends_on": [], "safe_default": "<first lexicographically>"}
  ```

### 3. Resolve entry point

Check for `@register_run` on disk and for `interview.json`. If either, the entry_point is resolved — continue.

Otherwise, invoke the `hpc-wrap-entry-point` sub-skill with `{goal, task_generator, experiment_dir}`. The sub-skill itself follows the same contract — if it can't resolve (e.g., multiple entry-point candidates), it returns its own ambiguities. Propagate them into this skill's list:

```json
{"field": "entry_point", "candidates": ["train.py", "main.py"], "depends_on": [], "safe_default": "<first match>"}
```

### 4. Resolve data axis

Check `.hpc/axes.yaml` for `executors.<run_name>` matching the current sha. If present, resolved — continue.

Otherwise, invoke `hpc-classify-axis` sub-skill. If it returns ambiguities, propagate. Sub-skill's own safe_default (`Sequential` for ambiguous trees) populates the entry's `safe_default`.

```json
{"field": "data_axis", "candidates": null, "depends_on": ["entry_point"], "safe_default": {"kind": "sequential"}}
```

Note the `depends_on: ["entry_point"]` — the data_axis dialog needs to know which `@register_run` function is being classified, which depends on the entry_point being resolved first.

### 5. Resolve homogeneous axes (cold-start only)

Check `.hpc/axes.yaml` for `homogeneous_axes`. If present, resolved. Otherwise, invoke `hpc-build-executor` sub-skill (axes-init companion). Propagate ambiguities.

### 6. Resolve walltime, gpu_type, partition

Auto-resolve from runtime priors:

```bash
hpc-agent read-runtime-prior --experiment-dir <dir> --profile <run_name> --cluster <cluster> --cmd-sha <sha> 2>/dev/null
```

`walltime_sec` = `prior.p95_sec * 1.30` (default safety_mult); cold-start fallback to `clusters.<cluster>.default_walltime_sec`. `gpu_type` from caller or `clusters.<cluster>.gpu_types[0]`. `partition` from `recommend-partition` primitive.

These never go into the ambiguities list — they always auto-resolve to a conservative default.

### 7. Return ambiguities if any

If the ambiguities list is non-empty:

```json
{
  "ok": false,
  "error_code": "needs_resolution",
  "data": {
    "resolved": {
      "experiment_dir": "/path/to/exp",
      "cluster": "hoffman2",
      "task_generator": {"kind": "items_x_seeds", "params": {...}},
      "walltime_sec": 7200,
      "gpu_type": "a100"
    },
    "ambiguities": [
      {"field": "entry_point", "candidates": [...], "depends_on": [], "safe_default": "..."},
      {"field": "data_axis", "candidates": null, "depends_on": ["entry_point"], "safe_default": "..."}
    ]
  }
}
```

The caller resolves every entry (slash walks user dialogs; autonomous caller applies safe_defaults) and re-invokes this skill with the augmented spec. The skill walks the same resolution steps; the caller-supplied fields now make Steps 2-5 short-circuit; the ambiguities list comes back empty; the skill proceeds to Step 8.

### 8. Build the fields JSON

Assemble every resolved value:

```json
{
  "experiment_dir": "<abs path>",
  "cluster": "<resolved>",
  "profile": "<run_name>",
  "data_axis": {...},
  "homogeneous_axes": [...],
  "task_generator": {...},
  "walltime_sec": 7200,
  "gpu_type": "a100",
  "no_canary": false,
  "campaign_id": null
}
```

### 9. Hand off to the submit-flow worker

```bash
hpc-agent run --workflow submit --fields-json '<fields>'
```

Spawns a fresh-context bare worker that runs `worker_prompts/submit.md` (experiment-agnostic execution: rsync, qsub, canary, journal, scheduler verify). Returns its envelope.

### 10. Propagate worker ambiguities (if any)

The worker may surface its own mid-flight needs_resolution — e.g., `co_tenant_exclusion`, `submit_now_vs_wait`, `walltime_split_confirm`. The worker's envelope carries them in the same shape (`{error_code: "needs_resolution", data: {resolved, ambiguities}}`), each ambiguity with its own `safe_default`. Surface verbatim to this skill's caller; the same one-round-trip resolution contract applies.

## Notes

- **One round-trip per call** (when ambiguities have no dependencies among each other). At most 2-3 round-trips if dependencies cascade (e.g., entry_point must be resolved before data_axis dialog can be asked). The depth is bounded by the dependency DAG, not by the number of ambiguities.
- **The worker is the experiment-agnostic execution layer.** Decisions about *this experiment* (executor, axis, walltime) live in this skill. Decisions about *workflow plumbing* (is there an in-flight run? is the spec cached?) live in the worker. The seam is clean.
- **Idempotent on `cmd_sha`.** Re-invoking with the same resolved fields produces the same `cmd_sha`; the submit-flow primitive dedupes against the journal.
- **Caller-supplied fields are always authoritative.** This skill's auto-resolution never overrides a value the caller passed in. The slash uses this to pass user-confirmed values; MARs's experiment-runner uses this to pass pre-resolved values.
- **No `[Y/n]` in this skill body.** Every choice point either resolves from caller-supplied input, autonomously, or via a sub-skill (which is also `[Y/n]`-free). The dialog prose lives in the `/submit-hpc` slash wrapper.
