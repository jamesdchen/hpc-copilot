---
name: hpc-aggregate
description: "Aggregate finished HPC runs into a final metrics envelope. Walks resolution steps, accumulates ambiguities into a single envelope, refuses to auto-mask integrity issues. Composes the aggregate-flow worker for the combiner + reducer pipeline."
allowed-tools: Bash Read Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[aggregate-flow](../../../../docs/primitives/aggregate-flow.md) workflow**.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Don't override the invoker default.** Hand off with the plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker. Reach for `--inline` / `HPC_AGENT_INVOKER=inline` only when the *user* opted in — never to dodge a "worker-auth risk": if `ANTHROPIC_API_KEY` is set (or a Claude Code OAuth creds file exists) the default `--bare` worker authenticates; if neither is present, escalate rather than silently switching modes. Inline trades away the worker's context isolation — that's the user's call, not a default. This is now enforced: `hpc-agent run` **refuses** an agent-supplied `--inline` when a spawning worker can authenticate (only `HPC_AGENT_INVOKER=inline` opts in then), so a refusal there is by design — use the default spawn or escalate, don't work around it.

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

**Inline mode (`HPC_AGENT_INVOKER=inline`).** **Never select this yourself** — it's a *user* opt-in (see *Execution style*); the default spawn runs this exact procedure *with* context isolation. When set, `hpc-agent run` does NOT spawn a `claude -p` worker: its envelope carries `data.mode == "inline"`, `data.prompt` (the canonical `worker_prompts/aggregate.md` procedure), and `data.instructions`. Produce the procedure's `{result, decisions, anomalies}` JSON, then return the spawn-shaped envelope: `data.report` = that JSON, `data.worker_exit_code` = 0, `data.mode` = "inline". **How you run it is capability-gated:** if you have a subagent-spawning tool (Claude Code's `Agent` tool — formerly `Task` — or equivalent), dispatch exactly ONE subagent with `data.prompt` as its whole task and return its report — the combiner/reducer transcript then lands in the subagent's context, recovering the isolation inline would otherwise trade away. If you have no such tool, run the procedure yourself in this session. Either path stays in-session — don't start another `claude -p` worker or re-invoke `hpc-agent run`; the subagent (when used) is the leaf. **Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline. When `data.mode == "spawn"` (the default), consume `data.report` as before.

### 6. Return envelope

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics. The caller has to explicitly resolve `allow_partial: true` after understanding what's missing.
- **Integrity violations are not auto-fixable.** A missing sidecar means the per-task metrics never landed — bug needs to be found. Returns `spec_invalid`, not `needs_resolution`.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
- **MARs pattern**: invoke after `hpc-status` returns `complete`. The skill auto-discovers the latest terminal run and aggregates; experiment-runner reads `results/metrics.json`.
