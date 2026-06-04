---
name: hpc-status
description: "Poll an in-flight HPC run's status and decide what to do about it — wait, resubmit failed tasks, mark terminal. Walks resolution steps, accumulates ambiguities into a single envelope. Branches on wait_terminal: snapshot calls the status primitive directly (no worker spawn); blocking poll hands off to the bare worker for context-isolated polling."
allowed-tools: Bash Read Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[monitor-flow](../../../../docs/primitives/monitor-flow.md) workflow**. Resolves which run to poll, what cadence, and how to handle failures.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" here means **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** — the harness runs them concurrently. It does NOT mean shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`), which trips the permission classifier as a compound command and complicates output parsing. Multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should each be their own tool-call block in the same message, not chained inside a single shell invocation.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Don't preemptively override the invoker default — but DO auto-retry inline on a real spawn failure.** Hand off with the plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker. The framework refuses an agent-supplied `--inline` flag when worker credentials exist (#155 guard), and that refusal is by design. **However:** when the spawn actually fails and the returned `internal` error message includes the framework's `Fallback: …HPC_AGENT_INVOKER=inline…` remediation hint, AUTOMATICALLY set `HPC_AGENT_INVOKER=inline` in the env and retry — the env var bypasses the #155 guard because it is the documented operator-opt-in form, and the framework's own hint is the signal that inline is the correct recovery path. Do NOT pause to ask the user. PowerShell `$env:HPC_AGENT_INVOKER = "inline"`, bash `export HPC_AGENT_INVOKER=inline`.
- **No narration at sub-skill boundaries.** When a composed sub-skill returns control, IMMEDIATELY chain to the next resolution step without emitting a summary message. Writing "X returned" or "Now resolving Y" reads as an end-of-turn signal to the harness and yields control back to the user — but the procedure has more steps to walk, so just continue tool-calling. The user sees only the final envelope.
- **Inspect files with `Read`/`Grep`/`Glob` — never shell `python -c`, `bash -c`, `jq`, `cat`, `head`, `grep`, or `find`.** Auto-mode's permission classifier hard-blocks arbitrary-code patterns (`python -c`, `bash -c`, command substitution, pipes) **regardless of `allow` rules** — issuing one stalls the workflow on a non-bypassable prompt, breaking the no-narration / no-pause invariants above. To read a JSON file (sidecar, `runs/<id>.json`, `axes.yaml`, anything under `.hpc/`): use the `Read` tool. To search filenames: `Glob`. To grep contents: `Grep`. If you need a value computed from cluster or framework state, there is almost always a specific `hpc-agent <verb>` (`describe`, `discover-runs`, `load-context`, `inspect-runs`, `verify-canary`, `reconcile`) — call that. The ONLY Bash this skill should issue is the `hpc-agent` calls listed in the Steps below (plus `git` if you commit a scaffolded file).

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

### 0. Ensure agent assets installed (idempotent)

The handoff at the end of this skill dispatches the rendered procedure to the named subagent `hpc-worker` discovered under `~/.claude/agents/hpc-worker.md`. If that file is missing — typically because `hpc-agent install-commands` hasn't run on this machine yet — the dispatch fails. Run install-commands first so this never bites:

```bash
hpc-agent install-commands
```

Idempotent: a no-op when assets are already installed. A pre-existing 0-byte file at `~/.claude/{commands,skills,agents}` is auto-cleared (see `result.cleared_collisions`); a non-empty file raises `FileExistsError` with a clear remediation — stop and surface that. Costs ~50ms when re-run.

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

**Inline mode (`HPC_AGENT_INVOKER=inline`).** **Never select this yourself** — it's a *user* opt-in (see *Execution style*); the default spawn runs this exact procedure *with* context isolation. When set, `hpc-agent run` does NOT spawn a `claude -p` worker: its envelope carries `data.mode == "inline"`, `data.prompt` (the canonical `worker_prompts/status.md` procedure), and `data.instructions`. Produce the procedure's `{result, decisions, anomalies}` JSON, then return the spawn-shaped envelope: `data.report` = that JSON, `data.worker_exit_code` = 0, `data.mode` = "inline". **How you run it is capability-gated:** if you have a subagent-spawning tool (Claude Code's `Agent` tool — formerly `Task` — or equivalent), dispatch exactly ONE subagent with `data.prompt` as its whole task and return its report — the poll loop's transcript then lands in the subagent's context, recovering the isolation inline would otherwise trade away. If you have no such tool, run the poll loop yourself in this session. Either path stays in-session — don't start another `claude -p` worker or re-invoke `hpc-agent run`; the subagent (when used) is the leaf. When `data.mode == "spawn"` (the default), consume `data.report` as before.

<!-- decision-content:inline-isolation-ceiling start -->
**Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline.
<!-- decision-content:inline-isolation-ceiling end -->

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
