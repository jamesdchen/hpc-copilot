---
name: hpc-status
description: "Poll an in-flight HPC run's status and decide what to do about it — wait, resubmit failed tasks, mark terminal. Autonomous: chooses polling cadence, resubmit thresholds, and lifecycle dispatch deterministically. Callers may pre-resolve run_id and wait_terminal; otherwise the skill auto-resolves the run from on-disk state. The /monitor-hpc slash invokes this skill; an external autonomous agent (MARs experiment-runner) invokes it directly to poll long-running jobs."
allowed-tools: Bash Read Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[monitor-flow](../../docs/primitives/monitor-flow.md) workflow**. This skill resolves the choices a status poll requires — which run, what cadence, whether to escalate a failing task to resubmit — and hands off to `hpc-agent run status` for execution.

## Inputs

Caller may pre-resolve any of these; the skill auto-resolves what's missing:

| Field | Default behaviour if absent |
|---|---|
| `experiment_dir` | Required (caller must supply) |
| `run_id` | Resolve from `load-context.data.in_flight`. Single in-flight → use it. Multiple → `spec_invalid: ambiguous_run` with candidates. Zero → `spec_invalid: no_in_flight_run`. |
| `wait_terminal` | Default `false` (one-shot snapshot). Set `true` to block until the run reaches a terminal lifecycle state. |
| `resubmit_failed_threshold` | Default `0.10` (resubmit if failed-task fraction ≤ 10%; above this, escalate). |
| `cadence_sec` | Default 60s. Only relevant when `wait_terminal=true`. |

## Mode

- **`mode: "interview"`** — caller passes user-resolved values; the skill respects them.
- **`mode: "autonomous"`** (default) — auto-resolve and never return `needs_human`. If multiple in-flight runs exist and the caller didn't pick, default to the most recently submitted; record the choice.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

Use `data.in_flight` to resolve `run_id` if not supplied.

### 2. Resolve run_id

If exactly one in-flight run, use it. If zero, return `spec_invalid: no_in_flight_run`. If multiple:

- Interview mode: return `spec_invalid: ambiguous_run` with `candidates: [run_id, ...]`; slash asks user.
- Autonomous mode: pick the most recently submitted (highest `submitted_at_iso`); record in `decisions`.

### 3. Hand off to the status worker

```bash
hpc-agent run status --fields-json '{"run_id": "<id>", "wait_terminal": <bool>}'
```

Spawns a fresh-context worker that reads `worker_prompts/status.md` and executes the polling loop (sacct/qstat queries, journal updates, lifecycle transitions). Returns the lifecycle state and per-task counts.

### 4. Handle the result

Branch on the worker's `data.report.result.lifecycle_state`:

| `lifecycle_state` | Skill behaviour |
|---|---|
| `running`, `pending` | Return the envelope as-is. Caller polls again later (or set `wait_terminal=true` for a blocking call). |
| `complete` | Return the envelope; next step is aggregation. Record `next_step_hint: "aggregate"` in decisions. |
| `terminal_with_failures` (some failed tasks) | Apply the resubmit policy below. |
| `terminal_no_progress` | Return `spec_invalid: terminal_no_progress`; the run is stuck and the caller decides whether to resubmit-from-scratch or investigate. Autonomous mode does NOT auto-resubmit a no-progress run. |

### 5. Resubmit policy (terminal_with_failures)

Compute `failed_fraction = data.report.result.failed_task_ids.length / data.report.result.total_tasks`.

- If `failed_fraction == 0`: lifecycle is actually `complete`; return aggregation hint.
- If `failed_fraction ≤ resubmit_failed_threshold` (default 10%): auto-invoke `hpc-agent resubmit --run-id <id> --task-ids <failed-list>`. Record the resubmission in decisions. Return the new resubmit run_id.
- If `failed_fraction > resubmit_failed_threshold`:
  - Interview mode: return `spec_invalid: high_failure_rate` with the count + sample errors; slash asks user.
  - Autonomous mode: do NOT auto-resubmit. Return `spec_invalid: high_failure_rate` with the count + sample errors. The autonomous caller (MARs experiment-runner) inspects the failure pattern and decides whether to resubmit, investigate, or abandon. Auto-resubmitting at >10% failure usually wastes more cluster time on the same bug.

### 6. Return the envelope

Surface to the caller:
- `data.report.result.lifecycle_state`
- `data.report.result.complete_count`, `data.report.result.failed_task_ids`
- `data.report.decisions` (cadence, resubmit choice, run_id selection)
- `data.report.anomalies` (anything off-contract — preemption, NODE_FAIL, etc.)

## Notes

- **No background polling.** Each invocation is one round-trip — one snapshot if `wait_terminal=false`, one blocking call if `true`. For ongoing monitoring beyond a single chat session, the caller schedules a `hpc-campaign-driver` cron or `/loop`.
- **Resubmit is conservative in autonomous mode.** A 10% failure rate is usually a transient cluster issue (preempt, scratch full, oneoff NODE_FAIL); >10% usually means a real bug. Autonomous callers shouldn't burn cluster time chasing real bugs.
- **MARs experiment-runner pattern**: invoke with `{experiment_dir, run_id, wait_terminal: true, mode: "autonomous"}`. Block until the run is terminal, get back the lifecycle + failed-task summary, decide next steps in MARs.
