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
- **Don't preemptively override the invoker default — but DO auto-retry inline on a real spawn failure.** Hand off with the plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker. The framework refuses an agent-supplied `--inline` flag when worker credentials exist (#155 guard), and that refusal is by design. **However:** when the spawn actually fails and the returned `internal` error message includes the framework's `Fallback: …HPC_AGENT_INVOKER=inline…` remediation hint, AUTOMATICALLY set `HPC_AGENT_INVOKER=inline` in the env and retry — the env var bypasses the #155 guard because it is the documented operator-opt-in form, and the framework's own hint is the signal that inline is the correct recovery path. Do NOT pause to ask the user. PowerShell `$env:HPC_AGENT_INVOKER = "inline"`, bash `export HPC_AGENT_INVOKER=inline`.
- **No narration at sub-skill boundaries.** When a composed sub-skill returns control, IMMEDIATELY chain to the next resolution step without emitting a summary message. Writing "X returned" or "Now resolving Y" reads as an end-of-turn signal to the harness and yields control back to the user — but the procedure has more steps to walk, so just continue tool-calling. The user sees only the final envelope.
- **Inspect files with `Read`/`Grep`/`Glob` — never shell `python -c`, `bash -c`, `jq`, `cat`, `head`, `grep`, or `find`.** Auto-mode's permission classifier hard-blocks arbitrary-code patterns (`python -c`, `bash -c`, command substitution, pipes) **regardless of `allow` rules** — issuing one stalls the workflow on a non-bypassable prompt, breaking the no-narration / no-pause invariants above. To read a JSON file (sidecar, `_combiner/wave_*.json`, `runs/<id>.json`, anything under `.hpc/` or `_aggregated/`): use the `Read` tool. To search filenames: `Glob`. To grep contents: `Grep`. If you need a value computed from cluster or framework state, there is almost always a specific `hpc-agent <verb>` (`describe`, `discover-runs`, `load-context`, `verify-aggregation-complete`, `cluster-reduce`) — call that. The ONLY Bash this skill should issue is the `hpc-agent` calls listed in the Steps below (plus `git` if you commit a scaffolded file).

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

### 1b. Reconcile a journal-only "in-flight" run against the cluster

`load-context` reports run state **from the journal**, which can lag the cluster: the scheduler may have completed, failed, killed, or purged the job after the last poll, yet the journal still records `in_flight` / `next_step_hint == "monitor"`. Trusting it blindly makes aggregation refuse with "nothing to aggregate yet" on a run that has actually finished — with no escape. This is the **symmetric** recovery to `hpc-submit`'s `already_in_flight` step, which already reconciles against the cluster.

So when `load-context.data` shows **no terminal run for the target profile but a non-empty `data.in_flight`** (the journal still says `monitor`), do NOT conclude anything from the journal alone. Reconcile against live cluster state first (derive `--scheduler` from the run's cluster in `load-context.data` / the run sidecar):

```bash
hpc-agent reconcile --run-id <in_flight_run_id> --scheduler <sge|slurm|pbspro|torque> --experiment-dir <experiment_dir>
```

`reconcile` polls the cluster once and updates the journal. Branch on `data.lifecycle_state`:

- **terminal** (`completed` / `failed` / `timeout` / …) — the cluster confirms the run finished; the journal is now marked terminal. Treat this run as the terminal `run_id` and continue to Step 2/3.
- **`abandoned`** — recorded `job_ids` exist but none are alive on the scheduler (scratch wiped, job manually cancelled, scheduler retention purged the record). Return `spec_invalid: run_abandoned` naming the `run_id`, with remediation: re-submit the run, or — if the cluster `_combiner/` partials still exist — aggregate them explicitly via `mode: "combiner-only"`. Do NOT silently report "nothing to aggregate".
- **still in-flight** (the cluster confirms work genuinely running) — now *confirmed against the cluster*, return `spec_invalid: nothing_to_aggregate` naming the running job and pointing the caller at `/monitor-hpc` to drive it to terminal.

Skip this step when `load-context` already shows a terminal run for the profile (the normal post-monitor path) — there's nothing to reconcile.

### 2. Resolve profile + run_id + stage

- Caller supplied profile → use.
- Else single profile with terminal runs → use.
- Else multiple profiles → add to ambiguities:
  ```json
  {"field": "profile", "candidates": [<profile list>], "depends_on": [], "safe_default": "<most recent>"}
  ```
- Else zero terminal runs → **first run Step 1b** (reconcile the journal-only in-flight run against the cluster). Only return `spec_invalid: nothing_to_aggregate` once reconcile has *confirmed against the cluster* that there is nothing terminal to aggregate and nothing `abandoned` to surface — never from the journal's `next_step_hint` alone.

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

**Inline mode (`HPC_AGENT_INVOKER=inline`).** **Never select this yourself** — it's a *user* opt-in (see *Execution style*); the default spawn runs this exact procedure *with* context isolation. When set, `hpc-agent run` does NOT spawn a `claude -p` worker: its envelope carries `data.mode == "inline"`, `data.prompt` (the canonical `worker_prompts/aggregate.md` procedure), and `data.instructions`. Produce the procedure's `{result, decisions, anomalies}` JSON, then return the spawn-shaped envelope: `data.report` = that JSON, `data.worker_exit_code` = 0, `data.mode` = "inline". **How you run it is capability-gated:** if you have a subagent-spawning tool (Claude Code's `Agent` tool — formerly `Task` — or equivalent), dispatch exactly ONE subagent with `data.prompt` as its whole task and return its report — the combiner/reducer transcript then lands in the subagent's context, recovering the isolation inline would otherwise trade away. If you have no such tool, run the procedure yourself in this session. Either path stays in-session — don't start another `claude -p` worker or re-invoke `hpc-agent run`; the subagent (when used) is the leaf. When `data.mode == "spawn"` (the default), consume `data.report` as before.

<!-- decision-content:inline-isolation-ceiling start -->
**Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline.
<!-- decision-content:inline-isolation-ceiling end -->

### 6. Return envelope

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics. The caller has to explicitly resolve `allow_partial: true` after understanding what's missing.
- **Reconcile before declaring "nothing to aggregate."** The journal can lag the cluster; an `in_flight` / `next_step_hint == "monitor"` run may have actually terminated, failed, or been purged. Step 1b reconciles against live cluster state before the skill refuses — the symmetric recovery to `hpc-submit`'s `already_in_flight`. An `abandoned` run surfaces as `spec_invalid: run_abandoned` (with a re-submit / combiner-only remediation), never as an indefinite "still in-flight."
- **Integrity violations are not auto-fixable.** A missing sidecar means the per-task metrics never landed — bug needs to be found. Returns `spec_invalid`, not `needs_resolution`.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
- **MARs pattern**: invoke after `hpc-status` returns `complete`. The skill auto-discovers the latest terminal run and aggregates; experiment-runner reads `results/metrics.json`.
