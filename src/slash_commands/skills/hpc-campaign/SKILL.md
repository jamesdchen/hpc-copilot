---
name: hpc-campaign
description: "Drive one tick of a closed-loop campaign — the per-iteration submit-flow → monitor-flow → aggregate-flow loop whose tasks.py adapts to prior results. Composes hpc-submit / hpc-status / hpc-aggregate for the per-phase mechanics; interprets validate-campaign findings; accumulates ambiguities into a single envelope."
allowed-tools: Bash Read Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[campaign](../../../../docs/primitives/campaign-advance.md) workflow**. Drives one tick — submits a new iteration, monitors it, aggregates, decides whether to advance or stop. Composes the per-phase workflow skills.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" here means **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** — the harness runs them concurrently. It does NOT mean shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`), which trips the permission classifier as a compound command and complicates output parsing. Multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should each be their own tool-call block in the same message, not chained inside a single shell invocation.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Each separate Bash tool call costs a round-trip + permission prompt; chaining unconditionally-sequential dependent invocations into one block saves both at no cost. Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block. (The framework's dispatched `hpc-worker` subagent blocks `&&` by a `PreToolUse` hook — one verb per envelope is its decision-boundary contract — but that block applies only to the spawned worker, NOT to this orchestrator skill.)
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Read sub-skill returns from the file primitive, not from the Skill tool result.** The composed workflow skills (`hpc-status`, `hpc-aggregate`) emit their return envelope to `<experiment_dir>/.hpc/_returns/<skill>.json` and intentionally do NOT write a closing chat message — that would fire an end-of-turn signal and stall this skill mid-tick. After every `Skill(<sub>)` tool call returns, the FIRST follow-up action MUST be `hpc-agent fetch-skill-return --skill <sub> --experiment-dir <experiment_dir>` — that verb reads, re-validates, and prints the sub-skill's envelope (and deletes it). Parse the JSON the verb emits to stdout; that's the sub-skill's return value. If `fetch-skill-return` returns `precondition_failed` with `failure_features.error_class_raw == "skill_return_missing"`, the sub-skill never emitted — re-invoke it or surface the missing-envelope error to the caller. (`hpc-submit` does not currently emit a return file — read its result from the Skill tool result as today; that path is unchanged.)
- **Inspect files with `Read`/`Grep`/`Glob` — never shell `python -c`, `bash -c`, `jq`, `cat`, `head`, `grep`, or `find`.** Auto-mode's permission classifier hard-blocks arbitrary-code patterns (`python -c`, `bash -c`, command substitution, pipes) **regardless of `allow` rules** — issuing one stalls the workflow on a non-bypassable prompt. To read a JSON file (sidecar, `runs/<id>.json`, `axes.yaml`, anything under `.hpc/` or `_campaigns/`): use the `Read` tool. To search filenames: `Glob`. To grep contents: `Grep`. If you need a value computed from cluster or framework state, there is almost always a specific `hpc-agent <verb>` (`describe`, `discover-runs`, `load-context`, `inspect-runs`, `validate-campaign`) — call that. The ONLY Bash this skill should issue is the `hpc-agent` calls listed in the Steps below (plus `git` if you commit a scaffolded file).

## Inputs

| Field | Source |
|---|---|
| `experiment_dir` | Required |
| `campaign_id` | Required |
| `path` | Caller (default `"A"` for manual grid; `"B"` for strategy-driven) |
| `allow_warnings` | Caller (default `true` — proceed past validate-campaign warnings; `false` blocks on warnings too) |

## The resolution contract

Same shape: walk every step, accumulate ambiguities, return all in one envelope OR proceed.

## Steps

### 1. Load context

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

If `data.campaigns[campaign_id]` doesn't exist → return `spec_invalid: unknown_campaign` with the list of known campaigns.

### 2. Determine the next step

Read the campaign driver's proposed next step from on-disk state (the driver advances exactly one step per tick).

| Step | Skill behaviour |
|---|---|
| `submit` | Steps 3-4: validate, then compose hpc-submit. |
| `monitor` | Compose `hpc-status` with the campaign's latest in-flight run_id. After `Skill(hpc-status)` returns, read its envelope via `hpc-agent fetch-skill-return --skill hpc-status --experiment-dir <experiment_dir>` — parse `run_id` / `lifecycle_state` / `next_step_hint` / `failed_task_ids?` / `resubmit_run_id?` from stdout. |
| `aggregate` | Compose `hpc-aggregate` with the campaign's latest terminal run_id. After `Skill(hpc-aggregate)` returns, read its envelope via `hpc-agent fetch-skill-return --skill hpc-aggregate --experiment-dir <experiment_dir>` — parse `run_id` / `profile` / `stage` / `metrics_path?` / `allow_partial` from stdout. |
| `decide` | A judgement-call step — add to ambiguities (caller resolves). |

### 3. Validate the next iteration

Before any `submit`, run `validate-campaign`:

```bash
hpc-agent validate-campaign --spec spec.json --experiment-dir <dir>
```

Interpret findings (deterministic):

| Severity | Behaviour |
|---|---|
| `error` | Return `spec_invalid: validate_campaign_failed` with the findings list. Block. |
| `warning` | If `allow_warnings: true`, proceed and record in decisions. Else add to ambiguities (let the caller decide). |
| `info` | Always proceed; record in decisions. |

Path B addendum: missing `_optuna_trial_number` (or equivalent unique marker) trips `missing_stochastic_marker` as an `error` — block.

### 4. Compose hpc-submit for the iteration

Invoke `hpc-submit` via the Skill tool with the campaign-tagged spec:

```json
{
  "experiment_dir": "<dir>",
  "campaign_id": "<id>"
}
```

The submit skill returns either its own ambiguities (propagate them as `entry_point`, `data_axis`, etc. ambiguities up to this skill's list — preserving the depends_on relationships) or the final run_id.

### 5. Record the iteration

```bash
hpc-agent campaign advance --campaign-id <id> --run-id <new-run-id> --experiment-dir <dir>
```

### 6. Handle `decide` steps

The driver may surface decision points (budget gates, convergence gates, early-stop). Each comes with the driver's heuristic `default_decision`. Add to ambiguities:

```json
{
  "field": "decide_response",
  "candidates": ["continue", "stop", "increase_budget"],
  "depends_on": [],
  "safe_default": "<driver's default_decision>",
  "context": {"question": "...", "evidence": {...}}
}
```

If the driver supplies no default → return `spec_invalid: decide_required` (campaign config oversight; this skill shouldn't guess).

### 7. Return ambiguities or final envelope

If ambiguities accumulated, return `needs_resolution`. Else return the tick's result:

```json
{
  "ok": true,
  "data": {
    "step": "submit" | "monitor" | "aggregate" | "decide",
    "run_id": "...",
    "lifecycle_state": "...",
    "cursor_position": N,
    "next_step_hint": "..."
  }
}
```

## Notes

- **One tick per invocation.** The skill does NOT loop. The caller (slash, cron, or MARs experiment-runner) drives ticks.
- **Composes workflow skills.** `hpc-submit`, `hpc-status`, `hpc-aggregate` are invoked per phase. Their ambiguities propagate up — the campaign skill's `needs_resolution` envelope may include ambiguities from any composed skill, plus its own (decide-response, allow-warnings).
- **Path B `_optuna_trial_number` is load-bearing.** Without a unique marker per iteration, `cmd_sha` collides → submit-flow dedupes → campaign silently collapses. `validate-campaign`'s `missing_stochastic_marker` is the hard gate.
- **No `[Y/n]`. No mode flag.**
