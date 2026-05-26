---
name: hpc-submit
description: "Decide all HPC submission inputs (cluster, entry_point, data_axis, homogeneous_axes, frozen_configs, task_generator, walltime, gpu_type) and hand off to the submit-flow worker. Autonomous — every decision auto-resolves; ambiguous cases fall back to the conservative default and proceed. Composes hpc-classify-axis / hpc-wrap-entry-point / hpc-build-executor for sub-decisions. Callers that pre-resolved a field pass it in; the skill skips that resolution step. The /submit-hpc slash invokes this skill after collecting human intent; an external autonomous agent (MARs experiment-runner, notebook driver) invokes it directly with whatever it knows."
allowed-tools: Bash Read Write Skill
execution: inline
category: agent-autonomous
---

Agent-facing decision layer over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow**. This skill is the *decisions* surface: it walks the choice points an HPC submission requires (which cluster? which executor? which axis classification? what walltime?) and resolves each one — autonomously by default, or accepting caller-supplied values when present. Once everything's resolved, it shells out to `hpc-agent run submit`, which spawns a fresh-context worker that runs `worker_prompts/submit.md` — the *execution* layer that does the actual rsync / qsub / canary / record sequence.

The slash command `/submit-hpc` is the human-interview wrapper around this skill: it conducts the propose-then-confirm dialog with the user, then invokes this skill with the resolved fields. An external autonomous caller invokes this skill directly, supplies whatever it pre-resolved, and relies on the autonomous defaults for the rest.

## Inputs

Caller-supplied (skill refuses with `spec_invalid` if absent):

| Field | Why the caller has to supply it |
|---|---|
| `experiment_dir` | Absolute path to the experiment repo. Cannot be inferred. |

Caller may pre-resolve to skip the corresponding sub-skill or auto-decision:

| Field | Skill's default behaviour if absent |
|---|---|
| `cluster` | Auto-resolve from `clusters.yaml` (single configured cluster → use it; multiple → `spec_invalid` with `ambiguous_cluster` + candidates) |
| `entry_point` (kind + path + run_name) | If no `@register_run` on disk and no `interview.json`, invoke `hpc-wrap-entry-point` skill |
| `data_axis` | If no `DataAxis` in `axes.yaml` for the current run_signature_sha, invoke `hpc-classify-axis` skill |
| `homogeneous_axes` | If no `.hpc/axes.yaml`, invoke `hpc-build-executor` skill (axes-init companion) |
| `frozen_configs` | Detected by convention (`configs/*.yaml`); caller's list overrides |
| `task_generator` | If absent and no `tasks.py` exists, refuse with `spec_invalid: task_generator_required` (cannot be invented) |
| `walltime_sec` | Auto-resolve from runtime priors (p95 × safety_mult); cold-start fallback to cluster default |
| `gpu_type` | Auto-resolve from cluster `gpu_types` list; pass-through to worker which scores candidates |
| `no_canary` | Default `false` (canary is the safety net) |
| `campaign_id` | Pass-through if supplied; otherwise omitted |

## Mode

Two interaction modes, signalled by the caller:

- **`mode: "interview"`** — the caller (typically `/submit-hpc`) has already conducted a human-facing dialog and is passing in user-resolved values. The skill treats every supplied field as authoritative and only auto-resolves what's missing.
- **`mode: "autonomous"`** (default) — the caller is an external agent (MARs experiment-runner, notebook driver, etc.). The skill auto-resolves every missing field and **never returns `needs_human` envelopes**. If a decision truly can't auto-resolve, the skill picks the most conservative interpretation and proceeds, recording the choice in `decisions` for the caller to inspect.

The default is `autonomous` so that an agent caller that just hands over a minimal `{experiment_dir}` gets a working submission.

## Steps

### 1. Load context

Run [`load-context`](../../docs/primitives/load-context.md) and treat its `data` as the only source of truth for run / campaign / cluster state:

```bash
hpc-agent load-context --experiment-dir <experiment_dir>
```

Key fields: `data.latest_run`, `data.in_flight`, `data.campaigns`, `data.next_step_hint`. If `next_step_hint == "monitor"`, the experiment has an in-flight run — return `spec_invalid: already_in_flight` with the run_id; the caller switches to `hpc-status`.

### 2. Resolve cluster

If caller supplied `cluster`, use it. Otherwise read `clusters.yaml`:

- Exactly one configured cluster → use it
- Multiple, no caller pick → `spec_invalid` with `error_code: ambiguous_cluster` and `candidates: [list]`. (Interview mode: slash re-asks user. Autonomous mode: caller picks the first lexicographically and records a warning — the user can pin via spec next time.)

### 3. Resolve entry point

Check for `@register_run` on disk and for `interview.json`:

```bash
grep -rln '@register_run' notebooks/ src/ *.py 2>/dev/null | head
test -f interview.json && cat interview.json | jq '._materialized.entry_point // empty'
```

If either is present, the entry point is resolved — proceed.

If neither, the experiment needs onboarding. Invoke the `hpc-wrap-entry-point` skill (Skill tool) with `{goal, task_generator, experiment_dir}`. The sub-skill materializes `tasks.py` + `interview.json` (and, on the wrapper path, `.hpc/wrappers/<run_name>.py`) and returns the resolved entry_point block.

Autonomous-mode contract: if `hpc-wrap-entry-point` returns `spec_invalid: ambiguous_entry_point`, pick the highest-likelihood candidate from the envelope's `candidates` list (probe order encodes likelihood) and record the choice in `decisions`; don't escalate.

### 4. Resolve data axis

Read `.hpc/axes.yaml`:

```bash
test -f .hpc/axes.yaml && python -c "
import yaml
d = yaml.safe_load(open('.hpc/axes.yaml'))
print((d.get('executors') or {}).get('<run_name>', {}))"
```

If `executors.<run_name>` exists and its `run_signature_sha` matches the current sha, the classification is valid — proceed.

If not, invoke the `hpc-classify-axis` skill with `{run_name, experiment_dir}`. The sub-skill walks its decision tree (`axis.py`) and commits the classification. Caller-supplied `data_axis` short-circuits the tree walk.

### 5. Resolve homogeneous axes (cold-start only)

If `.hpc/axes.yaml` doesn't have `homogeneous_axes`, invoke `hpc-build-executor` skill's axes-init companion with `{experiment_dir}`. Walks `tasks.py`, classifies each named dimension by heuristic, writes the file.

Caller-supplied `homogeneous_axes` short-circuits the heuristic.

### 6. Resolve walltime, gpu_type, partition

Auto-resolve from `read-runtime-prior` (the pro plugin's runtime-prior reader if available; falls back to cluster default otherwise):

```bash
hpc-agent read-runtime-prior --experiment-dir <dir> --profile <run_name> --cluster <cluster> --cmd-sha <sha> 2>/dev/null
```

`walltime_sec` = `prior.p95_sec * safety_mult` (default safety_mult=1.30). `gpu_type` = caller's pick or first GPU in `clusters.<cluster>.gpu_types`. `partition` = `recommend-partition` primitive output.

### 7. Build the fields JSON

Assemble every resolved value into the submit fields dict:

```json
{
  "experiment_dir": "<abs path>",
  "cluster": "<resolved>",
  "profile": "<run_name>",
  "data_axis": { ... },
  "homogeneous_axes": [ ... ],
  "task_generator": { ... },
  "walltime_sec": 7200,
  "gpu_type": "a100",
  "no_canary": false,
  "campaign_id": null
}
```

### 8. Hand off to the submit-flow worker

```bash
hpc-agent run submit --fields-json '<fields>'
```

This is the **execution boundary**. `hpc-agent run submit` spawns a fresh-context `claude -p --bare` worker that reads `worker_prompts/submit.md` and runs the deterministic submit sequence (rsync, qsub, canary, journal write, scheduler verify). No further decisions live in the worker — every choice was resolved in Steps 2-7.

The worker returns a JSON envelope on stdout with `data.report.result` (run_id, job_ids, grid dimensions, verified scheduler state), `data.report.decisions` (each choice the worker reached + why), and `data.report.anomalies`.

### 9. Handle worker escalations

The worker may surface escalations its `worker_prompts/submit.md` couldn't auto-resolve mid-flight (e.g., a co-tenant exclusion judgment, a `walltime_split` confirmation). Per mode:

- **Interview mode**: return the escalation envelope to the caller unchanged. The slash walks the user through the matching dialog and re-invokes this skill with the augmented fields.
- **Autonomous mode**: try to auto-resolve once:
  - `co_tenant_exclusion` → exclude any node where a co-tenant has been running >12h AND holds >50% CPU/mem; record the choice
  - `submit_now_vs_wait` → submit now (don't wait for a predicted-better window)
  - `walltime_split_confirm` → decline (don't checkpoint; let the run terminate naturally)
  - Any other escalation → return `spec_invalid` with the original `error_code` and a `reason: "autonomous mode cannot resolve <code>"` field. The caller (experiment-runner) is responsible.

### 10. Return the envelope

Surface to the caller verbatim:
- `data.report.result` (run_id, job_ids, grid dimensions, scheduler state)
- `data.report.decisions` (every resolved choice + source: `caller-supplied` / `cached` / `recall` / `agent` / `autonomous-fallback`)
- `data.report.anomalies` (anything off-contract)

## Notes

- **Two consumers, one execution path.** The slash and autonomous callers both end at Step 8 with a fully-resolved fields dict. They differ only in how the choices got resolved (user dialog vs. autonomous heuristic + sub-skill composition).
- **The execution layer is untouched.** `worker_prompts/submit.md` runs deterministically regardless of which surface invoked this skill — it just reads the resolved fields and executes.
- **Idempotent on `cmd_sha`.** Re-invoking with the same resolved fields produces the same `cmd_sha`; the submit-flow primitive dedupes against the journal and emits no cluster-side side effects.
- **No `[Y/n]` in this skill body.** Every choice point either resolves from caller-supplied input, autonomously, or via a sub-skill (which is also `[Y/n]`-free). The interview prose lives in the `/submit-hpc` slash wrapper.
- **MARs experiment-runner pattern**: invoke this skill with `{experiment_dir, mode: "autonomous"}` plus whatever it pre-resolved. The skill resolves the rest, submits, and returns the envelope. No escalation back to a human.
