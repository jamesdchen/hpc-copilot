---
name: hpc-submit
description: "Decide all HPC submission inputs (cluster, entry_point, data_axis, homogeneous_axes, frozen_configs, task_generator, walltime, gpu_type) and hand off via `hpc-agent run --workflow submit`. Walks every resolution step, accumulates ambiguities into a single envelope, never early-returns on the first miss. Callers (slash for human dialogs, autonomous agent applying safe_defaults) resolve the entire list in one re-invocation. Composes hpc-classify-axis / hpc-wrap-entry-point / hpc-build-executor for sub-decisions."
allowed-tools: Bash Read Write Skill Agent
execution: inline
category: agent-autonomous
---

Agent-facing decision layer for HPC submission. Walks the choice points (cluster, executor, axis classification) and resolves each from caller-supplied input, autonomous heuristics, or composed sub-skill. Once resolved, shells out to **`hpc-agent run --workflow submit`** (Step 9), which spawns a fresh-context worker running `worker_prompts/submit.md` (the [submit-flow](../../../../docs/primitives/submit-flow.md) primitive). `hpc-agent run` is the ONLY handoff verb — never invoke `submit-flow` directly.

The slash `/submit-hpc` is the human-interview wrapper; external autonomous agents (MARs experiment-runner, notebook driver) invoke this skill directly with pre-resolved fields.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" = **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** (harness runs them concurrently). NOT shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — that trips the permission classifier as a compound command. Independent reads/greps/`hpc-agent describe` lookups each go in their own tool-call block in the same message.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Saves a round-trip + permission prompt per chained call. Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block. (The `&&` block on the spawned `hpc-worker` subagent's `PreToolUse` hook does NOT apply to this orchestrator skill.)
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Don't preemptively override the invoker default — but DO auto-retry inline on a real spawn failure.** Hand off with plain `hpc-agent run --workflow …` and let `_auto_select_invoker` pick the worker (the framework refuses an agent-supplied `--inline` when worker credentials exist, #155). **However:** when the spawn fails and the returned `internal` error includes the framework's `Fallback: …HPC_AGENT_INVOKER=inline…` remediation hint, AUTOMATICALLY set `HPC_AGENT_INVOKER=inline` in the env and retry — the env var is the documented operator-opt-in form and the framework's hint is the signal. Do NOT pause to ask. Shell-level set: PowerShell `$env:HPC_AGENT_INVOKER = "inline"`, bash `export HPC_AGENT_INVOKER=inline`.
- **No narration at sub-skill boundaries.** When a composed sub-skill (`hpc-wrap-entry-point`, `hpc-classify-axis`, `hpc-build-executor`, `axes-init`, etc.) returns control, IMMEDIATELY chain to the next resolution step without emitting a summary message. Writing "X returned" or "Now resolving Y" reads as an end-of-turn signal to the harness and yields control back to the user — but the procedure has more steps to walk, so just continue tool-calling. The user sees only the final envelope (success, `needs_resolution`, or `spec_invalid`).
- **Read sub-skill returns from the file primitive, not from the Skill tool result.** Sub-skills (`hpc-wrap-entry-point`, `hpc-classify-axis`, `hpc-build-executor`) emit their envelope to `<experiment_dir>/.hpc/_returns/<skill>.json` and write no chat message. After every `Skill(<sub>)` returns, the FIRST follow-up MUST be `hpc-agent fetch-skill-return --skill <sub> --experiment-dir <experiment_dir>` — reads, re-validates, prints to stdout, deletes. Parse the JSON stdout as the sub-skill's return value. If `fetch-skill-return` returns `precondition_failed` with `failure_features.error_class_raw == "skill_return_missing"`, the sub-skill never emitted — re-invoke or surface to caller.
- **Inspect files with `Read`/`Grep`/`Glob` — never shell `python -c`, `bash -c`, `jq`, `cat`, `head`, `grep`, or `find`.** Auto-mode's permission classifier hard-blocks arbitrary-code patterns regardless of `allow` rules. To read a JSON file (submit_spec, sidecar, `interview.json`, `axes.yaml`, anything under `.hpc/`): `Read`. To search filenames: `Glob`. To grep contents: `Grep`. For cluster/framework state, call a specific `hpc-agent <verb>` (`describe`, `discover-runs`, `load-context`, `inspect-runs`, `verify-canary`, `reconcile`). **Unsure of the exact verb name?** Run `hpc-agent find "<intent>"` first — it returns a thin candidate list (`{name, verb, cli, summary}`) to pick from, then `hpc-agent describe <name>` for the one full contract. This is a *sequential* `find → describe` pair (the second call needs the first's result), distinct from the parallel independent-lookup batching above. The ONLY Bash this skill issues is the `hpc-agent` calls in the Steps (plus `git` if committing a scaffolded file).

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

### 0. Top-of-skill preflight (install + load + cluster-connectivity)

```bash
hpc-agent submit-preflight --experiment-dir <experiment_dir> [--cluster <cluster>]
```

Composite verb that runs `install-commands` → `load-context` → (when `--cluster` is supplied) `check-preflight` in sequence. Folds the production-ssh-path check the bare TCP probe missed (TCP :22 open but `rsync push` failing mid-submit with `getsockname failed: Not a socket`).

The composite's `data` carries:

- `data.install_commands.envelope` — same shape the standalone Step 0 produced; `data.cleared_collisions` lists any 0-byte sentinels the install-commands cleaned up at `~/.claude/{commands,skills,agents}/`.
- `data.load_context.envelope` — same shape the standalone Step 1 produced; **branch on `data.load_context.envelope.data.next_step_hint` exactly as the prior prose described** (see Step 1b).
- `data.check_preflight.envelope` — `{all_ok, checks}` shape. With `--cluster`, includes cluster reachability probes (`cluster_tcp_22` + `cluster_ssh_echo`, an actual `ssh <host> echo ok` round-trip through the production ssh path). Without `--cluster`, only local-env checks fire (ssh agent, ssh/rsync on PATH, clusters.yaml parses). A non-green `cluster_ssh_echo` means the submit path will fail — surface and stop before assembling the spec.

On `overall: "fail"`, surface the failing sub-envelope's `error_code` + `remediation` (preserved under `data.<subcall>.envelope`) and stop — the parallel siblings' results are kept, so a re-run can target only the failing piece via `--skip`. On `overall: "pass"`, proceed to Step 1b when `data.load_context.envelope.data.next_step_hint == "monitor"` (otherwise jump to Step 2).

If `--cluster` is not yet known at this point (caller's input is ambiguous), invoke the composite without it now and re-run `hpc-agent preflight --cluster <name>` after Step 2 — the local-env checks pass first, the SSH probe is the only thing deferred.

Replaces the prose-discipline contract where the agent had to remember Step 0 (whose omission motivated the entire 0.10.2 release).

#### 1b. Reconcile the in-flight run against the cluster

```bash
hpc-agent reconcile --run-id <in_flight_run_id> --scheduler <sge|slurm|pbspro|torque> --experiment-dir <experiment_dir>
```

`reconcile` polls the cluster once and updates the journal — a single call also settles the run's paired `-canary` sibling, so you never need a second reconcile with the `-canary` suffix (#258). Branch on `data.lifecycle_state`:

- **terminal** (`complete` / `failed` / `timeout`) — the prior run actually finished; the journal is now marked terminal and the `already_in_flight` blocker is gone. **Proceed with this submit.**
- **`abandoned`** — recorded `job_ids` exist but none are alive on the scheduler (scratch wiped, job manually cancelled, scheduler retention purged the record). The journal is now marked `abandoned`, freeing the `cmd_sha` to claim. **Proceed with this submit.**
- **still in-flight** (cluster confirms work running) — return `spec_invalid: already_in_flight` with the run_id (state conflict, not ambiguity). Remediation lists three recovery paths: (a) `/monitor-hpc` to drive to terminal (**primary** — the prior submit is still running); (b) `hpc-agent reconcile --run-id <id> --scheduler <sge|slurm|pbspro|torque>` for a later manual recheck; (c) `--no-canary` ONLY when the in-flight one is the prior run's *canary* and the operator confirmed it succeeded. Do NOT skip canary as a generic workaround — reconcile is the right tool.
- **`unable_to_verify`** (#258) — the cluster alive-check itself failed (SSH / auth / network — e.g. an expired Duo cache), so the run's true state is unknown. Do NOT proceed (you cannot confirm the blocker is gone) and do NOT claim it abandoned. Return `spec_invalid: already_in_flight` but point the remediation at the connectivity failure (surface `data.last_status`): fix SSH/auth and re-run reconcile. This is distinct from "still in-flight" — there the cluster *answered*; here it didn't.

The skill **never** refuses `already_in_flight` from `next_step_hint` alone — only after reconcile has *confirmed against the cluster* that the run is genuinely still in-flight. Skip Step 1b when `load-context` already shows a terminal run for the profile (the normal post-monitor path) — there's nothing to reconcile.

### 2. Resolve cluster

- Caller supplied → use.
- Else single configured cluster in `clusters.yaml` → use.
- Else (multiple, no pick) → add to ambiguities:
  ```json
  {"field": "cluster", "candidates": [...], "depends_on": [], "safe_default": "<first lexicographically>"}
  ```

### 3. Resolve entry point

Check for `@register_run` on disk and for `interview.json`. If either, the entry_point is resolved.

**Before short-circuiting on a cached `interview.json`, reconcile its `task_generator` against the caller-supplied one.** A stale cached generator (e.g. 8 seeds) would silently win over a caller-passed one (e.g. 100 seeds), violating "caller-supplied fields are always authoritative." Guard it:

1. **Caller did NOT supply a `task_generator` this invocation** → the cached interview is authoritative; continue.
2. **Both exist** → compare by canonical content with one verb:
   ```bash
   hpc-agent check-task-generator-mismatch --caller-task-generator '<caller JSON>' --cached-task-generator '<interview.json task_generator JSON>'
   ```
   The verb canonicalizes both (key-sorted, whitespace-free) and returns `data.match` plus both shapes' `canonical` + `sha256`. When the caller has no cached generator to compare against, omit `--cached-task-generator` (the verb returns `match: true`, `reason: no_cached_generator`).
   - **`data.match: true`** (`reason: identical`) → short-circuit as before; continue.
   - **`data.match: false`** (`reason: divergent`) → do NOT silently use the cached one. Branch on `on_task_generator_mismatch`:
     - `fail` (**default**) → return `spec_invalid: task_generator_mismatch`, surfacing BOTH shapes (the verb's `data.cached` from `interview.json`, `data.caller` from this invocation) and their resulting task counts, with remediation: re-invoke with `on_task_generator_mismatch=refresh` or `=prefer-caller`, or clear `.hpc/` to start fresh.
     - `refresh` → rewrite `interview.json` (and regenerate `.hpc/tasks.py`) from the caller's `task_generator` via `hpc-wrap-entry-point`, then continue with the caller's.
     - `prefer-caller` → use the caller's `task_generator` for this submission without rewriting the interview (the previous unconditional behavior, now an explicit opt-in).

   The silent "cached wins" behavior is removed — a divergent count must be surfaced, not dropped on the floor.

Otherwise (no `@register_run` and no `interview.json`), invoke the `hpc-wrap-entry-point` sub-skill with `{goal, task_generator, experiment_dir}`. The sub-skill itself follows the same contract — if it can't resolve (e.g., multiple entry-point candidates), it returns its own ambiguities. Propagate them into this skill's list:

```json
{"field": "entry_point", "candidates": ["train.py", "main.py"], "depends_on": [], "safe_default": "<first match>"}
```

Immediately after `Skill(hpc-wrap-entry-point)` returns, read its envelope from the file primitive:

```bash
hpc-agent fetch-skill-return --skill hpc-wrap-entry-point --experiment-dir <experiment_dir>
```

The verb prints the sub-skill's return envelope to stdout (and deletes it). Parse the JSON to pick up `entry_point_kind`, `run_name`, `tasks_py_path`, `interview_json_path`, `wrapper_path?`, `total_tasks`, `cmd_sha`. A `precondition_failed` / `skill_return_missing` envelope means the sub-skill never emitted — surface as `spec_invalid: skill_return_missing` or re-invoke.

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

Otherwise, invoke `hpc-classify-axis` sub-skill. After `Skill(hpc-classify-axis)` returns, fetch its envelope via `hpc-agent fetch-skill-return --skill hpc-classify-axis --experiment-dir <experiment_dir>` and parse `run_name` / `run_signature_sha` / `data_axis` / `classified_by` from stdout. If it returns ambiguities (Error branch), propagate. Sub-skill's own safe_default (`Sequential` for ambiguous trees) populates the entry's `safe_default`.

```json
{"field": "data_axis", "candidates": null, "depends_on": ["entry_point"], "safe_default": {"kind": "sequential"}}
```

Note the `depends_on: ["entry_point"]` — the data_axis dialog needs to know which `@register_run` function is being classified, which depends on the entry_point being resolved first.

### 5. Resolve homogeneous axes (cold-start only)

Check `.hpc/axes.yaml` for `homogeneous_axes`. If present, resolved. Otherwise, invoke `hpc-build-executor` sub-skill (axes-init companion). After `Skill(hpc-build-executor)` returns, fetch its envelope via `hpc-agent fetch-skill-return --skill hpc-build-executor --experiment-dir <experiment_dir>` and parse `executor_path` / `axes_path` / `homogeneous_axes` from stdout. Propagate ambiguities.

### 6. Resolve walltime, gpu_type, partition

One verb resolves all three resources — `walltime_sec`, `gpu_type`, and `partition` — each from a caller override first, then an auto-resolution rule:

```bash
hpc-agent resolve-resources --cluster <cluster> --experiment-dir <experiment_dir> [--profile <run_name>] [--cmd-sha <sha>] [--walltime-sec <caller_override>] [--gpu-type <caller_override>] [--partition <caller_override>] [--user-preferred-partition <pref>]
```

`data` carries the resolved `{walltime_sec, gpu_type, partition}` plus a `provenance` map recording how each was resolved:

- **`walltime_sec`** — caller (`--walltime-sec`), else the optional `read-runtime-prior` verb's `p95 × 1.30` (default safety_mult). `read-runtime-prior` is an **optional-plugin-only** verb — on a core install (e.g. plain PyPI `hpc-agent`) it does not exist, and on the very first submit there is no prior anyway. `resolve-resources` treats a missing/erroring verb (and a verb that reports `needs_canary`) as **cold-start**: `data.walltime_sec` comes back `null` with `provenance.walltime_sec` one of `cold_start_no_profile` / `cold_start_no_samples` / `cold_start_prior_verb_unavailable`. A missing prior is **never** an error.
- **`gpu_type`** — caller (`--gpu-type`), else `clusters.<cluster>.gpu_types[0]` (`provenance` `cluster_default`), or `null` when the cluster declares none.
- **`partition`** — caller (`--partition`), else the `recommend-partition` primitive (reused, not reimplemented), carried under `provenance.partition` as `recommend_partition:<rationale>`. `null` (`no_partitions_supplied`) when no partition config is available.

**On cold-start `walltime_sec: null`, fall back to the cluster cold-start walltime**, which **always resolves** — `clusters.<cluster>.default_walltime_sec` when the operator set it, otherwise a conservative built-in default (4h) clamped to `max_walltime_sec`. The `get_default_walltime_sec` resolver (`hpc_agent.infra.clusters`) guarantees a value, so a core install never stalls on the optional verb.

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

If the envelope you got back has `data.mode == "inline"` (instead of `data.mode == "spawn"` and `data.report`), the user opted into inline mode. The envelope carries the same canonical procedure the spawned worker would have run as EITHER `data.prompt` (embedded inline, for a small procedure) OR `data.prompt_path` (an absolute path to the procedure on disk, used when it is large — #262B); exactly one is present. It also carries `data.instructions`. Your job is to produce the worker report `{result, decisions, anomalies}` and return an envelope shaped like the spawn path: `data.report` = that JSON, `data.worker_exit_code` = 0 (non-zero only if a step blocked you), `data.mode` = "inline".

**How you run it is capability-gated — check in this order:**

1. **You have a subagent-spawning tool (Claude Code's `Agent` tool — formerly `Task` — or your harness's equivalent).** → **Dispatch exactly ONE subagent** with the procedure as its whole task; return the report it produces. **If `data.prompt_path` is present, pass that PATH to the subagent and instruct it to `Read` the file as its FIRST action — do NOT `Read` the prompt file into THIS context** (that re-floods the orchestrator the path-forwarding was built to spare, #262B). Else pass `data.prompt` verbatim. This is the default inline path in Claude Code and **not optional when the `Agent` tool exists** — running the procedure in your own context defeats the whole point of inline mode (it floods this session with the rsync/qsub transcript that spawn-mode isolation would have kept out). If you find yourself about to execute step 1 of the procedure in this session while the `Agent` tool is listed in your toolset, stop and dispatch instead.
2. **You have no such tool.** → Run the procedure yourself in this session (full tools + credentials). This is the harness-fallback path, not the default.

Either path stays in-session: do NOT start another `claude -p` worker, do NOT re-invoke `hpc-agent run`, and the subagent (when used) is the leaf — it runs every step and spawns nothing further.

**Never shell out to extract or relay `data.prompt`, and never go to disk for it (#262).** Do NOT pipe the envelope through `python -c`, `bash -c`, `jq`, `powershell -Command`, `pwsh -Command`, `cmd /c`, or **any** shell-via-flag that takes a code string as an argument — the auto-mode classifier denies these as compound / code-injection commands, costing a rejected round-trip before you even start. And NEVER read the harness's internal `.claude/projects/…/tool-results/*.txt` files to recover the prompt: that path is harness-managed, undocumented, and unstable. `data.prompt` is the **subagent's** task, not something the orchestrator must hold, transform, or reconstruct — pass it (or the envelope reference you already have) to the subagent and let IT read what it needs. If the prompt seems "too large to read directly," that is precisely the signal to forward it to the subagent, not to improvise a shell extraction.

<!-- decision-content:inline-isolation-ceiling start -->
**Isolation ceiling:** a subagent recovers *context* isolation but not *environment* isolation — it shares this session's sandbox posture and auto-loads project CLAUDE.md, unlike the default `--bare` spawn (sandbox forced off, CLAUDE.md stripped). If a sandboxed session would block the cluster SSH, or project memory must not color the run, that's a sign the *user* wants the default spawn, not inline.

**Sandbox-blocks-SSH is structural, so detect it FIRST (#265).** When inline mode runs in a sandboxed session AND the workflow needs cluster SSH (submit / status / aggregate), the SSH is blocked no matter how cleanly the local prep runs — and the worker would otherwise burn all the local prep (interview, classify, build-submit-spec) before hitting the wall, then return a misleading near-success. The worker's procedure runs a one-shot cluster-SSH preflight (`hpc-agent check-preflight --cluster <cluster>`) as its Step 0 and, on a sandbox-consistent block, returns `spec_invalid: sandbox_blocks_cluster_ssh` immediately (`ok: false`, no prep wasted), with remediation: *re-run as the default `--bare` spawn (set `ANTHROPIC_API_KEY`) or disable the session sandbox*. Treat that `sandbox_blocks_cluster_ssh` envelope as a hard stop, NOT a partial success — nothing was submitted.
<!-- decision-content:inline-isolation-ceiling end -->

### 10. Propagate worker ambiguities (if any)

The worker may surface its own mid-flight needs_resolution — e.g., `co_tenant_exclusion`, `submit_now_vs_wait`, `walltime_split_confirm`. The worker's envelope carries them in the same shape (`{error_code: "needs_resolution", data: {resolved, ambiguities}}`), each ambiguity with its own `safe_default`. Surface verbatim to this skill's caller; the same one-round-trip resolution contract applies.

## Notes

- **One round-trip per call** (when ambiguities have no dependencies among each other). At most 2-3 round-trips if dependencies cascade (e.g., entry_point must be resolved before data_axis dialog can be asked). The depth is bounded by the dependency DAG, not by the number of ambiguities.
- **The worker is the experiment-agnostic execution layer.** Decisions about *this experiment* (executor, axis, walltime) live in this skill. Decisions about *workflow plumbing* (is there an in-flight run? is the spec cached?) live in the worker. The seam is clean.
- **Idempotent on `cmd_sha`.** Re-invoking with the same resolved fields produces the same `cmd_sha`; the submit-flow primitive dedupes against the journal.
- **Caller-supplied fields are always authoritative.** This skill's auto-resolution never overrides a value the caller passed in. The slash uses this to pass user-confirmed values; MARs's experiment-runner uses this to pass pre-resolved values. **This includes `task_generator` vs. a cached `interview.json`** (Step 3): a divergent cached generator must surface as `spec_invalid: task_generator_mismatch` (default), never silently shrink a 100-task request to a stale 8-task submission.
- **No `[Y/n]` in this skill body.** Every choice point either resolves from caller-supplied input, autonomously, or via a sub-skill (which is also `[Y/n]`-free). The dialog prose lives in the `/submit-hpc` slash wrapper.
