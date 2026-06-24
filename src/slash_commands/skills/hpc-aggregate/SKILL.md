---
name: hpc-aggregate
description: "Aggregate finished HPC runs into a final metrics envelope. Walks resolution steps, accumulates ambiguities into a single envelope, refuses to auto-mask integrity issues. Composes the aggregate-flow worker for the combiner + reducer pipeline."
allowed-tools: Bash Read Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[aggregate-flow](../../../../docs/primitives/aggregate-flow.md) workflow**.

## Execution style

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in a single message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block.
- **Be terse.** Lead with the action or result; skip filler and trailing restatements of tool output.
- **Don't preemptively override the invoker default — but DO auto-retry inline on a real spawn failure.** Hand off with the plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker. When the spawn fails and the returned `internal` error includes the `Fallback: …HPC_AGENT_INVOKER=inline…` hint, AUTOMATICALLY set `HPC_AGENT_INVOKER=inline` and retry — do NOT pause to ask. PowerShell `$env:HPC_AGENT_INVOKER = "inline"`, bash `export HPC_AGENT_INVOKER=inline`.
- **No narration at sub-skill boundaries.** When a composed sub-skill returns control, IMMEDIATELY chain to the next resolution step. Summary messages like "X returned" / "Now resolving Y" read as end-of-turn and yield control to the user — just continue tool-calling. The user sees only the final envelope.
- **Return via the emit-skill-return file primitive — never via chat.** The parent (`hpc-campaign`) reads `<experiment_dir>/.hpc/_returns/hpc-aggregate.json`, not chat. Step 6 stages the envelope and invokes `hpc-agent emit-skill-return` as the LAST tool call. Schema: `hpc_agent/schemas/skill_returns/hpc-aggregate.json`.
- **Inspect files with `Read`/`Grep`/`Glob` — never shell `python -c`, `bash -c`, `jq`, `cat`, `head`, `grep`, or `find`.** The permission classifier hard-blocks arbitrary-code patterns regardless of `allow` rules and stalls the workflow on a non-bypassable prompt. For JSON (sidecar, `_combiner/wave_*.json`, `runs/<id>.json`, anything under `.hpc/` or `_aggregated/`): `Read`. Filenames: `Glob`. Contents: `Grep`. For cluster or framework state, call the specific `hpc-agent <verb>` (`describe`, `discover-runs`, `load-context`, `verify-aggregation-complete`, `cluster-reduce`). The ONLY Bash this skill issues is the `hpc-agent` calls in the Steps below (plus `git` if committing a scaffolded file).

> **NEVER compute aggregate metrics yourself, and NEVER write `metrics.json` (or any aggregated-metrics artifact) from your own arithmetic, prose, or a `Read`-then-mean-in-your-head shortcut.** Aggregation is the framework reducer's job — the `aggregate-flow` worker runs deterministic code (cluster-reduce when an `aggregate_cmd` is configured, the cluster combiner, or — when neither ran — a per-task `metrics.json` weighted-mean). That reducer is the SoT for every aggregate number; an LLM in the compute loop is the exact failure this skill exists to prevent (it gets the arithmetic wrong AND returns `ok: true`, which is worse than failing). You ALWAYS route the final metrics through Step 5's `hpc-agent run --workflow aggregate` handoff. If the reducer cannot run — `verify-aggregation-complete` failed, the partials/sidecars are missing, the worker returned a typed failure — return that typed failure (`spec_invalid` / `integrity_violation`) or park with the anomaly in the envelope. Do NOT fabricate a number, do NOT "fill in" a missing `metrics.json`, do NOT report `ok: true` on a run whose reducer never produced an aggregate.

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

### 0. Top-of-skill preflight (install + load + optional reconcile)

```bash
hpc-agent aggregate-preflight --experiment-dir <experiment_dir> [--reconcile-scheduler <sge|slurm|pbspro|torque>]
```

Composite verb that runs `install-commands` → `load-context` → (conditionally) `reconcile` as one deterministic state machine — replaces the prior Step 0 (`install-commands`) + Step 1 (`load-context`) + Step 1b (`reconcile`) sequence this skill carried through 0.10.6. Sequential by design: install must succeed before `load-context` can resolve framework paths. The composite's `data` carries each sub-envelope verbatim under `data.install_commands.envelope`, `data.load_context.envelope`, and `data.reconcile.envelope` — same shapes the steps had individually, so downstream branching on `data.load_context.envelope.data.in_flight` / `next_step_hint` is unchanged. Skipped or not-applicable (`null`) slots are visible per-call so a re-run can target only the failing piece.

The install-commands sub-call exists because the handoff at the end of this skill dispatches the rendered procedure to the named subagent `hpc-worker` under `~/.claude/agents/hpc-worker.md`; if install-commands never ran, that file is missing and the dispatch fails. Idempotent: a no-op when assets are installed (~50ms). A pre-existing 0-byte sentinel at `~/.claude/{commands,skills,agents}` is auto-cleared (reported under `data.install_commands.envelope.data.cleared_collisions`); a non-empty collision raises `FileExistsError` with a clear remediation — surface that and stop.

On `overall: "fail"`, surface the failing sub-envelope's `error_code` + `remediation` (preserved under `data.<subcall>.envelope`) and stop. On `overall: "pass"`, proceed to Step 2.

#### When to supply `--reconcile-scheduler`

`load-context` reports run state **from the journal**, which can lag the cluster: the scheduler may have terminated the job after the last poll while the journal still records `in_flight` / `next_step_hint == "monitor"`. Trusting it blindly makes aggregation refuse with "nothing to aggregate yet" on a finished run.

The reconcile sub-call fires only when **the journal's `next_step_hint == "monitor"` AND you supplied `--reconcile-scheduler`**. Supply it when you can resolve the scheduler up front — a single configured cluster in `clusters.yaml`, or a caller-pinned cluster. The composite targets the first `data.in_flight` run_id; a single reconcile also settles its paired `-canary` sibling, clearing both journal entries.

When you **cannot** resolve the scheduler ahead of time (the in-flight run's cluster is only knowable from `data.load_context` after the call), omit `--reconcile-scheduler`. The composite then returns with `data.reconcile == null` and `data.load_context.envelope.data.next_step_hint == "monitor"`; derive `--scheduler` from the run's cluster in `data.load_context` and issue the reconcile as a follow-up:

```bash
hpc-agent reconcile --run-id <in_flight_run_id> --scheduler <sge|slurm|pbspro|torque> --experiment-dir <experiment_dir>
```

Either path produces a reconcile envelope. Branch on `data.lifecycle_state` (under `data.reconcile.envelope.data` when folded into the composite, or the standalone reconcile envelope when run as a follow-up):

- **terminal** (`completed` / `failed` / `timeout` / …) — the cluster confirms the run finished; the journal is now marked terminal. Treat this run as the terminal `run_id` and continue to Step 2/3.
- **`abandoned`** — recorded `job_ids` exist but none are alive on the scheduler (scratch wiped, job manually cancelled, scheduler retention purged the record). Return `spec_invalid: run_abandoned` naming the `run_id`, with remediation: re-submit the run, or — if the cluster `_combiner/` partials still exist — aggregate them explicitly via `mode: "combiner-only"`. Do NOT silently report "nothing to aggregate".
- **still in-flight** (the cluster confirms work genuinely running) — now *confirmed against the cluster*, return `spec_invalid: nothing_to_aggregate` naming the running job and pointing the caller at `/monitor-hpc` to drive it to terminal.
- **`unable_to_verify`** (#258) — the cluster alive-check itself failed (SSH / auth / network), so the run's true state is unknown. Do NOT conclude `abandoned` or `nothing_to_aggregate`; surface the SSH error from `data.last_status` and have the operator fix connectivity/auth and re-run. Distinct from "still in-flight" — there the cluster answered; here it didn't.

When `load-context` already shows a terminal run for the profile (the normal post-monitor path), `next_step_hint` is not `monitor`, the reconcile branch never fires, and there's nothing to reconcile — proceed straight to Step 2.

#### 0b. Honor a pre-staged spec (skip the interview)

`hpc-submit` pre-stages `<experiment_dir>/aggregate_spec.json` (`prepare-followup-specs`, #278) so a submit→aggregate handoff skips the profile/run_id round-trip. Use the `Read` tool on `<experiment_dir>/aggregate_spec.json`, then branch on a **`cmd_sha` staleness gate** computed against the Step-0 preflight / `load-context` output — that gate is the only thing that makes adopting a pre-staged run_id safe (a re-submit must NOT silently inherit the old run):

- **absent →** no pre-staged spec; fall through to *2. Resolve profile + run_id + stage* (today's path).
- **stale →** the spec's `cmd_sha` matches no current journal run for its `run_id` (a re-submit landed a new `cmd_sha`, or it targets a different run): treat the file as stale, ignore it, and fall through to *2. Resolve profile + run_id + stage* — including its reconcile / `spec_invalid` / `needs_resolution` outcomes.
- **fresh →** the spec's `cmd_sha` matches the current journal run for that `run_id` (compare against `data.load_context.envelope.data.latest_run.cmd_sha`): adopt the spec's `run_id` + `profile` directly. *2. Resolve profile + run_id + stage* is satisfied for those two — do NOT raise a profile or run_id ambiguity. Leave `stage` and `allow_partial` to the operator/caller: they are sentinel `null` in the spec, so `stage` still defaults to the run's final stage and `allow_partial` defaults to `false` (resolved at *3. Verify aggregation readiness*).

### 2. Resolve profile + run_id + stage

- Caller supplied profile → use.
- Else single profile with terminal runs → use.
- Else multiple profiles → add to ambiguities:
  ```json
  {"field": "profile", "candidates": [<profile list>], "depends_on": [], "safe_default": "<most recent>"}
  ```
- Else zero terminal runs → **first run the Step 0 reconcile branch** (reconcile the journal-only in-flight run against the cluster — folded into `aggregate-preflight` via `--reconcile-scheduler`, or the standalone `hpc-agent reconcile` follow-up). Only return `spec_invalid: nothing_to_aggregate` once reconcile has *confirmed against the cluster* that there is nothing terminal to aggregate and nothing `abandoned` to surface — never from the journal's `next_step_hint` alone.

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

Spawns a fresh-context bare worker that reads `worker_prompts/aggregate.md` — runs the combiner + reducer + summary pull + runtime-sample ingestion. **This handoff is the ONLY path that may produce the aggregated metrics.** The worker's reducer is deterministic code regardless of how the run was configured: cluster-reduce when an `aggregate_cmd` is set, the cluster combiner's per-wave partials otherwise, and — for a `@register_run` SSH sweep submitted with NO reducer and NO combiner — a per-task `metrics.json` weighted-mean (the SSH analogue of the local/pure-API default). You never substitute your own computation for any of these, even when "it's just a mean of ten numbers."

**Inline mode (`HPC_AGENT_INVOKER=inline`).** **Never select this yourself** — it's a *user* opt-in. When set, `hpc-agent run` does NOT spawn a `claude -p` worker: its envelope carries `data.mode == "inline"`, `data.prompt` (the canonical `worker_prompts/aggregate.md` procedure), and `data.instructions`. Produce the procedure's `{result, decisions, anomalies}` JSON, then return the spawn-shaped envelope: `data.report` = that JSON, `data.worker_exit_code` = 0, `data.mode` = "inline". **Capability-gated:** if you have a subagent-spawning tool (`Agent`/`Task` or equivalent), dispatch exactly ONE subagent with `data.prompt` as its whole task and return its report. Otherwise, run the procedure yourself in this session. Either path stays in-session — don't start another `claude -p` worker or re-invoke `hpc-agent run`. When `data.mode == "spawn"` (default), consume `data.report` as before.

<!-- decision-content:inline-isolation-ceiling start -->
**Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline.
<!-- decision-content:inline-isolation-ceiling end -->

### 6. Emit the return envelope (final tool call)

The parent skill reads the return envelope from `<experiment_dir>/.hpc/_returns/hpc-aggregate.json`. Stage it, then emit:

1. Use the `Write` tool to write the envelope to `<experiment_dir>/.hpc/_returns/hpc-aggregate.staged.json`. Required fields on the Success branch: `ok: true`, `skill: "hpc-aggregate"`, `run_id`, `profile`, `stage`. Optional: `metrics_path` (the local path of the aggregated metrics artifact, e.g. `results/metrics.json`), `allow_partial` (the resolved flag), `missing_waves` (when `allow_partial: true`), `decisions` (accumulated decisions list). On a fatal error, write the standard `ErrorEnvelope` shape.

2. Invoke as your FINAL tool call:

   ```bash
   hpc-agent emit-skill-return --skill hpc-aggregate --experiment-dir <experiment_dir>
   ```

   The verb validates against `hpc_agent/schemas/skill_returns/hpc-aggregate.json` and atomically renames `.staged.json` → `.json`. Then **hand control back to the parent without ending your turn** — emit no summary or closing message. The parent's next action is `hpc-agent fetch-skill-return --skill hpc-aggregate`.

## Notes

- **The reducer computes every aggregate number; you never do.** No hand-computed means, no prose arithmetic, no model-authored `metrics.json`. The `aggregate-flow` worker always reduces with deterministic code — even a no-reducer `@register_run` SSH sweep falls back to a per-task `metrics.json` weighted-mean, so there is no scenario where you are "forced" to compute it yourself. If the reducer genuinely cannot run, return a typed failure or park the anomaly; never fabricate.
- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics. The caller has to explicitly resolve `allow_partial: true` after understanding what's missing.
- **Reconcile before declaring "nothing to aggregate."** The journal can lag the cluster; an `in_flight` / `next_step_hint == "monitor"` run may have actually terminated, failed, or been purged. The Step 0 reconcile branch (folded into `aggregate-preflight`, or a standalone `reconcile` follow-up) reconciles against live cluster state before the skill refuses — the symmetric recovery to `hpc-submit`'s `already_in_flight`. An `abandoned` run surfaces as `spec_invalid: run_abandoned` (with a re-submit / combiner-only remediation), never as an indefinite "still in-flight."
- **Integrity violations are not auto-fixable.** A missing sidecar means the per-task metrics never landed — bug needs to be found. Returns `spec_invalid`, not `needs_resolution`.
- **Idempotent.** Re-aggregating the same `(run_id, profile, stage)` produces byte-identical output.
- **MARs pattern**: invoke after `hpc-status` returns `complete`. The skill auto-discovers the latest terminal run and aggregates; experiment-runner reads `results/metrics.json`.
