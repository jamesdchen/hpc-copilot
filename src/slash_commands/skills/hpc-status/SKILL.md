---
name: hpc-status
description: "Poll an in-flight HPC run's status and decide what to do about it — wait, resubmit failed tasks, mark terminal. Walks resolution steps, accumulates ambiguities into a single envelope. Branches on wait_terminal: snapshot calls the status primitive directly (no worker spawn); blocking poll hands off to the bare worker for context-isolated polling."
allowed-tools: Bash Read Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[monitor-flow](../../../../docs/primitives/monitor-flow.md) workflow**. Resolves which run to poll, what cadence, and how to handle failures.

## Execution style

- **Batch independent tool calls** into one parallel message — multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should not run serially.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `run_id` | Caller, or auto-resolve from `load-context.data.in_flight` |
| `wait_terminal` | Caller (default `false` for snapshot; `true` for blocking poll) |
| `resubmit_failed_threshold` | Caller (default `0.10`) |

## The resolution contract

Same as `hpc-submit`: walk every step, accumulate ambiguities (no early-return), return them all in one envelope OR proceed to execution if none.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

### 2. Resolve run_id

- Caller supplied → use.
- Else exactly one in-flight run → use.
- Else multiple in-flight, no pick → add to ambiguities:
  ```json
  {"field": "run_id", "candidates": [<run_id list>], "depends_on": [], "safe_default": "<most recent by submitted_at_iso>"}
  ```
- Else zero in-flight → return `spec_invalid: no_in_flight_run` (this isn't an ambiguity — there's literally nothing to monitor).

### 3. Return ambiguities if any

If accumulated, return `needs_resolution` envelope per the standard shape. Caller resolves and re-invokes.

### 4. Branch on wait_terminal

The worker spawn is only justified when the workflow has more than one LLM-driven step — i.e., when there's a poll loop or lifecycle dispatch that would otherwise accumulate intermediate state in the caller's context.

**If `wait_terminal == false` (snapshot)**:

```bash
hpc-agent status --run-id <id>
```

Single primitive call. Returns one envelope. No worker spawn. The caller's context grows by ~1 KB (the envelope).

**If `wait_terminal == true` (blocking poll)**:

```bash
hpc-agent run --workflow status --fields-json '{"run_id": "<id>", "wait_terminal": true}'
```

Spawns a fresh-context bare worker that reads `worker_prompts/status.md`. The worker contains the poll loop (sacct queries every 60s, lifecycle transitions, sidecar updates) in its private context. Returns the final terminal envelope. The caller's context grows by ~1 KB regardless of how long the poll ran.

**Inline mode (`HPC_AGENT_INVOKER=inline`).** When set, `hpc-agent run` does NOT spawn: its envelope carries `data.mode == "inline"` and `data.prompt`, the canonical `worker_prompts/status.md` procedure. Run that poll loop yourself, in this session (do not spawn a worker), then return the spawn-shaped envelope: `data.report` = the procedure's `{result, decisions, anomalies}` JSON, `data.worker_exit_code` = 0, `data.mode` = "inline". The poll transcript then lands in your context rather than a worker's — the trade the caller opted into. When `data.mode == "spawn"` (the default), consume `data.report` as before.

This split saves the worker-spawn overhead on the common single-call case while preserving context isolation on the multi-step case. The principle: **a workflow skill hands off to a bare worker when (and only when) the workflow has more than one LLM-driven step.**

### 5. Handle the result

Branch on the envelope's `data.lifecycle_state` (or `data.report.result.lifecycle_state` on the worker path):

| State | Skill behaviour |
|---|---|
| `running`, `pending` | Return envelope as-is. (Snapshot mode; the caller polls again later.) |
| `complete` | Return envelope. Add `next_step_hint: "aggregate"` in decisions. |
| `terminal_with_failures` | Apply resubmit policy below. |
| `terminal_no_progress` | Return `spec_invalid: terminal_no_progress`. The run is stuck — caller decides whether to resubmit-from-scratch or investigate. |

### 6. Resubmit policy (terminal_with_failures)

`failed_fraction = failed_task_ids.length / total_tasks`

- `failed_fraction == 0` → lifecycle is actually `complete`.
- `failed_fraction ≤ resubmit_failed_threshold` (default 10%) → auto-invoke `hpc-agent resubmit --run-id <id> --task-ids <failed-list>`. Record in decisions; return the new resubmit run_id.
- `failed_fraction > resubmit_failed_threshold` → add to ambiguities (decision needs caller resolution):
  ```json
  {
    "field": "high_failure_rate_action",
    "candidates": ["resubmit", "investigate", "abandon"],
    "depends_on": [],
    "safe_default": "investigate",
    "context": {"failed_count": N, "total": M, "sample_errors": [...]}
  }
  ```
  At >10% failure, auto-resubmitting usually wastes more cluster time on the same bug. The safe_default is `investigate` — don't auto-resubmit.

### 7. Return envelope

Surface to caller verbatim.

## Notes

- **Snapshot vs blocking is the worker-spawn boundary.** Single-step → primitive. Multi-step (the poll loop) → worker. This matches the general rule.
- **MARs polling pattern**: invoke with `wait_terminal: true` ONCE; let the worker block; receive the terminal envelope. Avoids accumulating ~N poll-envelopes in experiment-runner's context.
- **No `[Y/n]`. No mode flag.** Caller-supplied authoritative; ambiguities returned in one envelope.
