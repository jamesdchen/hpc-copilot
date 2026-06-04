---
name: hpc-submit
description: "Decide all HPC submission inputs (cluster, entry_point, data_axis, homogeneous_axes, frozen_configs, task_generator, walltime, gpu_type) and hand off via `hpc-agent run --workflow submit`. Walks every resolution step, accumulates ambiguities into a single envelope, never early-returns on the first miss. Callers (slash for human dialogs, autonomous agent applying safe_defaults) resolve the entire list in one re-invocation. Composes hpc-classify-axis / hpc-wrap-entry-point / hpc-build-executor for sub-decisions."
allowed-tools: Bash Read Write Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer for HPC submission. This skill is the *experiment-aware* decision surface: it walks the choice points an HPC submission requires (which cluster? which executor? which axis classification?) and resolves each one — from caller-supplied input, autonomous heuristics, or composed sub-skill. Once everything's resolved, it shells out to **`hpc-agent run --workflow submit`** (Step 9), which spawns a fresh-context worker that runs `worker_prompts/submit.md` — the experiment-agnostic execution layer (rsync, qsub, canary, journal, scheduler verify). The worker's pipeline is the [submit-flow](../../../../docs/primitives/submit-flow.md) primitive; this skill **never** invokes that primitive directly — `hpc-agent run` is the only handoff verb.

The slash `/submit-hpc` is the human-interview wrapper around this skill; an external autonomous agent (MARs experiment-runner, notebook driver) invokes this skill directly with whatever it pre-resolved.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Don't preemptively override the invoker default — but DO auto-retry inline on a real spawn failure.** Hand off with the plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker. The framework refuses an agent-supplied `--inline` flag when worker credentials exist (#155 guard), and that refusal is by design — don't dodge a *hypothetical* worker-auth risk. **However:** when the spawn actually fails and the returned `internal` error message includes the framework's `Fallback: …HPC_AGENT_INVOKER=inline…` remediation hint (the spawn-worker quota / billing / auth recovery signal added in 0.10.3), AUTOMATICALLY set `HPC_AGENT_INVOKER=inline` in the env and retry — the env var bypasses the #155 guard because it is the documented operator-opt-in form, and the framework's own hint is the signal that inline is the correct recovery path. Do NOT pause to ask the user; this skill delegates the decision when the framework explicitly recommends it. The shell-level set varies by host: PowerShell `$env:HPC_AGENT_INVOKER = "inline"`, bash `export HPC_AGENT_INVOKER=inline`.
- **No narration at sub-skill boundaries.** When a composed sub-skill (`hpc-wrap-entry-point`, `hpc-classify-axis`, `hpc-build-executor`, `axes-init`, etc.) returns control, IMMEDIATELY chain to the next resolution step without emitting a summary message. Writing "X returned" or "Now resolving Y" reads as an end-of-turn signal to the harness and yields control back to the user — but the procedure has more steps to walk, so just continue tool-calling. The user sees only the final envelope (success, `needs_resolution`, or `spec_invalid`).

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
| `on_task_generator_mismatch` | Caller (default `fail`; `refresh` / `prefer-caller` are explicit opt-ins — see Step 3) |
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

### 0. Ensure agent assets installed (idempotent)

Step 9's handoff dispatches the rendered procedure to the named subagent `hpc-worker` discovered under `~/.claude/agents/hpc-worker.md`. If that file is missing — typically because `hpc-agent install-commands` hasn't run on this machine yet — the dispatch fails mid-resolution and the agent falls back to running cluster procedures by hand (a known cause of fabricated cluster commands). Run install-commands first so this never bites:

```bash
hpc-agent install-commands
```

Idempotent: a no-op when assets are already installed (the same byte content lands at the same path). On a fresh machine it writes `~/.claude/{commands,skills,agents}/` and reports what was installed. A pre-existing 0-byte file at any of those three target paths is auto-cleared (see `result.cleared_collisions`); a non-empty file raises `FileExistsError` with a clear remediation — stop and surface that. Skipping this step is fine on a machine you know is already set up; running it twice costs ~50ms and is otherwise harmless.

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

If `data.next_step_hint == "monitor"`, return `spec_invalid: already_in_flight` with the run_id (different from `needs_resolution` — this isn't an ambiguity, it's a state conflict). The error envelope should name three concrete recovery paths in the remediation: (a) `/monitor-hpc` to drive the run to terminal (the normal case — the prior submit really is still running); (b) `hpc-agent reconcile --run-id <id> --scheduler <sge|slurm|pbspro|torque>` when the operator knows the cluster state is gone (scratch wiped, job manually cancelled, cluster bounced) — reconcile polls the cluster, sees the dir/job is missing, and marks the journal `abandoned` so the next submit isn't blocked; (c) `--no-canary` only when the prior run's *canary* is the in-flight one and the operator has independently confirmed it succeeded. Do NOT skip canary as a generic workaround for a journal-cluster mismatch — (b) is the right tool, not (c).

### 2. Resolve cluster

- Caller supplied → use.
- Else single configured cluster in `clusters.yaml` → use.
- Else (multiple, no pick) → add to ambiguities:
  ```json
  {"field": "cluster", "candidates": [...], "depends_on": [], "safe_default": "<first lexicographically>"}
  ```

### 3. Resolve entry point

Check for `@register_run` on disk and for `interview.json`. If either, the entry_point is resolved.

**Before short-circuiting on a cached `interview.json`, reconcile its `task_generator` against the caller-supplied one.** A stale `interview.json` left in `experiment_dir` from earlier dev work encodes its *own* `task_generator` (e.g. 8 seeds). If the caller passed a *different* `task_generator` this invocation (e.g. 100 seeds), the cached one would silently win and `build-submit-spec` would compute the wrong `total` — an 8-task submission for a 100-task request, with no warning. That violates this skill's own "caller-supplied fields are always authoritative" contract. Guard it:

1. **Caller did NOT supply a `task_generator` this invocation** → the cached interview is authoritative; continue.
2. **Both exist** → compare by canonical content (sort keys, then compare — or sha256 of the canonicalized JSON):
   - **Equal** → short-circuit as before; continue.
   - **Different** → do NOT silently use the cached one. Branch on `on_task_generator_mismatch`:
     - `fail` (**default**) → return `spec_invalid: task_generator_mismatch`, surfacing BOTH shapes (`cached` from `interview.json`, `caller` from this invocation) and their resulting task counts, with remediation: re-invoke with `on_task_generator_mismatch=refresh` or `=prefer-caller`, or clear `.hpc/` to start fresh.
     - `refresh` → rewrite `interview.json` (and regenerate `.hpc/tasks.py`) from the caller's `task_generator` via `hpc-wrap-entry-point`, then continue with the caller's.
     - `prefer-caller` → use the caller's `task_generator` for this submission without rewriting the interview (the previous unconditional behavior, now an explicit opt-in).

   The silent "cached wins" behavior is removed — a divergent count must be surfaced, not dropped on the floor.

Otherwise (no `@register_run` and no `interview.json`), invoke the `hpc-wrap-entry-point` sub-skill with `{goal, task_generator, experiment_dir}`. The sub-skill itself follows the same contract — if it can't resolve (e.g., multiple entry-point candidates), it returns its own ambiguities. Propagate them into this skill's list:

```json
{"field": "entry_point", "candidates": ["train.py", "main.py"], "depends_on": [], "safe_default": "<first match>"}
```

### 3b. Cover non-axis required executor params

Once the entry point is resolved, cross-check its signature against the `task_generator`'s axes (this is what `validate-executor-signatures` gates on at submit — `uncovered_required_param`). A param that is **required** (no default in the executor's CLI surface) and **not a swept axis** must be given a constant, or every cluster task crashes at argparse (#195).

- Resolved automatically when `wrap-entry-point` already wrote `fixed_params` (Step 5b), or the param has an argparse default.
- Else add to ambiguities — the value can't be invented:
  ```json
  {"field": "uncovered_param", "candidates": ["samples"], "depends_on": ["entry_point"], "safe_default": {"samples": <argparse default if any, else null>}, "context": {"executor": "<run_name>", "required_no_default": ["samples"]}}
  ```
  The caller resolves to a `{param: value}` map; it's threaded into `entry_point.fixed_params` and baked into every `tasks.resolve(i)`. `depends_on: ["entry_point"]` — the signature isn't known until the entry point is.

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

Auto-resolve `walltime_sec` from runtime priors **when available**. `read-runtime-prior` is an **optional-plugin-only** verb — on a core install (e.g. plain PyPI `hpc-agent`) it does not exist, and on the very first submit there is no prior anyway. So treat a missing/erroring `read-runtime-prior` as **cold-start**, NOT as a problem to surface:

```bash
# Optional: only an installed plugin registers this verb. Missing verb (argparse
# "invalid choice", exit 2) or no prior yet → cold-start; do NOT report it.
hpc-agent read-runtime-prior --experiment-dir <dir> --profile <run_name> --cluster <cluster> --cmd-sha <sha> 2>/dev/null || true
```

- **Prior found** (verb present AND ≥1 sample): `walltime_sec = prior.p95_sec * 1.30` (default safety_mult).
- **Cold-start** (verb absent, or present with no prior): fall back to the cluster cold-start walltime, which **always resolves** — `clusters.<cluster>.default_walltime_sec` when the operator set it, otherwise a conservative built-in default (4h) clamped to `max_walltime_sec`. The `get_default_walltime_sec` resolver (`hpc_agent.infra.clusters`) guarantees a value, so a core install never stalls on the optional verb.

`gpu_type` from caller or `clusters.<cluster>.gpu_types[0]`. `partition` from `recommend-partition` primitive.

These never go into the ambiguities list and the missing optional verb is never an ambiguity — they always auto-resolve to a conservative default.

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

### 9. Hand off — invoke `hpc-agent run --workflow submit`

```bash
hpc-agent run --workflow submit --fields-json '<fields>'
```

Or, when the JSON contains Windows paths or other shell-hostile escapes, write it to a tempfile and pass `--fields-file <path>` instead — `hpc-agent run` accepts either.

**This is the ONLY handoff verb for this skill.** `hpc-agent run` spawns a fresh-context bare worker that runs `worker_prompts/submit.md` and returns its envelope. You consume `data.report` from that envelope. Done.

**⚠ Do NOT call `hpc-agent submit-flow` from this skill.** That primitive is the low-level pipeline atom the *worker* invokes internally (after building its own `run_id`, sidecar, and spec). The catalog lists it as `agent_facing: true` because the worker is also an agent — not because this decision-layer skill calls it. Calling `submit-flow` here skips the worker's pre-flight, context loading, and report shaping, and forces you to fabricate inputs (`run_id`, `script`, full submit spec) the worker would have built for you.

**⚠ Do NOT pass `--inline` or set `HPC_AGENT_INVOKER=inline` yourself.** Inline is a *user* opt-in (see *Execution style*). `hpc-agent run` refuses an agent-supplied `--inline` when a spawning worker can authenticate (#155). If the default spawn errors with `worker authentication unavailable` or a worker-launch `internal` error, **escalate to the caller** — return a `needs_resolution`-style anomaly. Do NOT retry with `HPC_AGENT_INVOKER=inline` to "work around" it; that flips an explicit user-only knob behind the user's back.

#### Inline branch — only reached when the *user* set `HPC_AGENT_INVOKER=inline`

If the envelope you got back has `data.mode == "inline"` (instead of `data.mode == "spawn"` and `data.report`), the user opted into inline mode. The envelope carries `data.prompt` (the same canonical procedure the spawned worker would have run) and `data.instructions`. Your job is to produce the worker report `{result, decisions, anomalies}` and return an envelope shaped like the spawn path: `data.report` = that JSON, `data.worker_exit_code` = 0 (non-zero only if a step blocked you), `data.mode` = "inline".

**How you run it is capability-gated — check in this order:**

1. **You have a subagent-spawning tool (Claude Code's `Agent` tool — formerly `Task` — or your harness's equivalent).** → **Dispatch exactly ONE subagent** with `data.prompt` as its whole task; return the report it produces. This is the default inline path in Claude Code and **not optional when the `Agent` tool exists** — running `data.prompt` in your own context defeats the whole point of inline mode (it floods this session with the rsync/qsub transcript that spawn-mode isolation would have kept out). If you find yourself about to execute step 1 of `data.prompt` in this session while the `Agent` tool is listed in your toolset, stop and dispatch instead.
2. **You have no such tool.** → Run the procedure yourself in this session (full tools + credentials). This is the harness-fallback path, not the default.

Either path stays in-session: do NOT start another `claude -p` worker, do NOT re-invoke `hpc-agent run`, and the subagent (when used) is the leaf — it runs every step and spawns nothing further.

<!-- decision-content:inline-isolation-ceiling start -->
**Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline.
<!-- decision-content:inline-isolation-ceiling end -->

### 10. Propagate worker ambiguities (if any)

The worker may surface its own mid-flight needs_resolution — e.g., `co_tenant_exclusion`, `submit_now_vs_wait`, `walltime_split_confirm`. The worker's envelope carries them in the same shape (`{error_code: "needs_resolution", data: {resolved, ambiguities}}`), each ambiguity with its own `safe_default`. Surface verbatim to this skill's caller; the same one-round-trip resolution contract applies.

## Notes

- **One round-trip per call** (when ambiguities have no dependencies among each other). At most 2-3 round-trips if dependencies cascade (e.g., entry_point must be resolved before data_axis dialog can be asked). The depth is bounded by the dependency DAG, not by the number of ambiguities.
- **The worker is the experiment-agnostic execution layer.** Decisions about *this experiment* (executor, axis, walltime) live in this skill. Decisions about *workflow plumbing* (is there an in-flight run? is the spec cached?) live in the worker. The seam is clean.
- **Idempotent on `cmd_sha`.** Re-invoking with the same resolved fields produces the same `cmd_sha`; the submit-flow primitive dedupes against the journal.
- **Caller-supplied fields are always authoritative.** This skill's auto-resolution never overrides a value the caller passed in. The slash uses this to pass user-confirmed values; MARs's experiment-runner uses this to pass pre-resolved values. **This includes `task_generator` vs. a cached `interview.json`** (Step 3): a divergent cached generator must surface as `spec_invalid: task_generator_mismatch` (default), never silently shrink a 100-task request to a stale 8-task submission.
- **No `[Y/n]` in this skill body.** Every choice point either resolves from caller-supplied input, autonomously, or via a sub-skill (which is also `[Y/n]`-free). The dialog prose lives in the `/submit-hpc` slash wrapper.
