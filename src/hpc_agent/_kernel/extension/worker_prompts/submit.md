Agent-facing composition over the **[submit-flow](../../docs/primitives/submit-flow.md) workflow atom** (full pre-flight + rsync + deploy + qsub + record pipeline in one CLI call). For just the journal-write half (when the agent has already qsubbed), use the [submit-spec](../../docs/primitives/submit-spec.md) primitive directly. Both are idempotent on `run_id`: a replay returns `data.deduped: true` and emits no cluster-side side effects.

Throughout this procedure, "invoke <primitive>" means call the primitive's `backed_by.cli` or `backed_by.python` entry point; see `docs/primitives/<name>.md` for the full contract. For envelope/exit-code shapes see `docs/reference/cli-spec.md`.

## Reporting conventions

Two fields on the worker report carry observations back to the caller — they are NOT interchangeable:

- **`decisions`** is the **strict enumerated record** of which judgement points this workflow reached. For the **submit** workflow there are exactly seven allowed `point` IDs — any other value is rejected by `parse_worker_report`:
  - `entry_path` (backed by `suggest-setup-action`)
  - `prior_run` (backed by `find-prior-run`)
  - `axis_class` (backed by `classify-axis`)
  - `throughput_plan` (backed by `plan-throughput`)
  - `canary` (backed by `verify-canary`)
  - `preflight` (backed by `check-preflight`)
  - `validate_campaign` (backed by `validate-campaign`)

  Each entry is `{point, outcome, why, chosen?, rejected?}` — `outcome` is a short tag describing what happened at that point (e.g. `unclassified`, `setup_required`, `fail`, `dispatcher_failed`). At a **judgement** point (a genuine control-flow branch the deterministic layer could not decide for you — here `axis_class`), `why` is **required** (`parse_worker_report` rejects an empty one), and you should set `chosen` (the branch taken) and `rejected` (the alternatives you weighed and discarded). At a deterministic point `why` is a free-form one-liner.

- **`anomalies`** is a **free-form multi-line string** for everything else: stop conditions that don't correspond to an enumerated point, magnitude warnings, deduped notices, raw `stderr` tails, held-job details, missing-flag callouts. Anything you'd want the caller to see that isn't one of the seven points goes here.

When in doubt, prefer `anomalies`. **Do not invent new `decisions` point IDs** (`setup_action`, `environment`, `setup_required`, `mature_repo_needs_interview`, etc.) — the envelope is rejected and the run reports as broken even when the cluster work succeeded.

## Setup

**Load context first.** Run `hpc-agent load-context --experiment-dir .` and treat its `data` as the ONLY source of truth for run / campaign / cluster state. Never rely on conversational memory or shell variables — a context compaction or a session restart erases them; the on-disk state does not.

- `data.latest_run` — cluster, profile, resources, env, remote_path, campaign_id, run_id, cmd_sha, job_ids. On a `reuse`/`interview` action, read these instead of re-interviewing the user.
- `data.in_flight` — active runs (run_id, stage, ssh_target, job_ids).
- `data.campaigns` — campaign ids + cursor iteration.
- `data.next_step_hint` — `submit` / `monitor` / `aggregate`.

If a value you need later is absent here, derive it from the run sidecar on disk — never from memory.

**Step 0 — cluster-SSH preflight, before any heavy local prep (#265).** This procedure ends in cluster SSH (rsync push, scp deploy, qsub). If you are running INLINE inside a *sandboxed* session, that SSH is structurally blocked — and you would only discover it AFTER all the local prep (interview, classify, build-submit-spec) is done, returning a misleading near-success. Fail fast instead: as your first cluster-touching action — right after `load-context` resolves the cluster, before any interview/classify/build — run `hpc-agent check-preflight --cluster <cluster>`. If it is blocked in a way consistent with a sandboxed network (the probe cannot leave the sandbox, as opposed to a genuine cluster-down), STOP and return `spec_invalid: sandbox_blocks_cluster_ssh` immediately — `ok: false`, no local prep wasted — with remediation: *"this workflow needs un-sandboxed cluster SSH; re-run as the default `--bare` spawn (set `ANTHROPIC_API_KEY` so it can authenticate) instead of inline, or disable the session sandbox."* Do **NOT** report `ok: true` with a buried "SSH was blocked" note — that reads as success when nothing was actually submitted (the #265 failure).

Read cluster definitions with the [clusters-describe](../../docs/primitives/clusters-describe.md) primitive — never resolve and parse `clusters.yaml` by hand:

```bash
hpc-agent clusters describe <cluster>   # resolved block: host, user, scheduler, scratch, modules, conda_source, gpu_types
```

Run `hpc-agent clusters list` first if you don't yet know the cluster key.

Call [suggest-setup-action](../../docs/primitives/suggest-setup-action.md) to figure out where in the priority ladder the experiment sits — it returns `{priority, action, run_id, candidates, reason}`:

```bash
hpc-agent suggest-setup-action --experiment-dir .
```

Branch on `action`:

| `action` | Priority | Meaning | Procedure behavior |
|---|---|---|---|
| `monitor` | 0 | At least one in-flight run on the journal | Stop and report; the caller switches to the status workflow. |
| `reuse` | 1 | Per-experiment sidecars exist | Each sidecar carries the full v2 config snapshot — resources/env/constraints/runtime. Reuse keeps `tasks.py` byte-identical so `cmd_sha` matches. |
| `interview` | 2 | `.hpc/tasks.py` exists, no run history | Skip executor-discovery + axes interview (tasks.py already encodes the axis); jump to Step 4b. |
| `fresh` | 3 | Nothing exists | Full interview from Step 1. |

## Step 0: Build the `src/` package

The experiment repo commits **nothing generated** — `src/` is `.gitignore`d. Build it from the notebooks before anything else, so discovery, the elision gate, and the deploy bundle all see a current package:

```bash
hpc-agent export-package --experiment-dir .
```

`export-package` globs `notebooks/{pipeline,executors,scripts}/*.ipynb`, exports each to `src/<module>.py` (strict-AST for `@register_run` executors, `# export`-marker for pipeline libraries), and content-hash-caches against `.hpc/.build-cache.json` — a no-op when nothing changed. The built `src/` rides the `submit-flow` rsync into the deploy bundle; **the cluster node never builds** (it stays stdlib-only). On a `spec_invalid` envelope (an output-path collision, a bad module name), surface it and stop — the notebooks need a rename.

## Step 0b: Honor a `shell_command` wrapper (fallback path only)

**The default mature-repo path is `@register_run` on the user's function** — same as the greenfield notebook case. Step 1's existing `discover_runs` scan finds the decorated function regardless of whether it lives in `notebooks/`, `train.py`, or `main.py`. No special branch is needed for that case.

This step exists for the **wrapper fallback**: when direct decoration wasn't possible (non-Python entry point, decorator conflict, vendor code), `/wrap-entry-point-hpc` materializes a `@register_run` wrapper at `.hpc/wrappers/<run_name>.py` and writes `interview.json` declaring a `shell_command` entry_point that points at it. **Read `interview.json` with the Read tool** (do not shell `python -c "json.load(...)"`) and look at `_materialized.entry_point`.

If that block is present, branch on its `kind`:

| `kind` | Procedure behavior |
|---|---|
| `shell_command` | **The fallback path.** A wrapper has been materialized at `<wrapper_path>` (`.hpc/wrappers/<run_name>.py`) — it satisfies the `@register_run` contract for an entry point the framework can't decorate directly. **Skip Step 1's discover scan**; treat `<run_name>` as the picked run. **Use `<executor_cmd>` as the `EXECUTOR` in Step 6's `job_env`** instead of synthesizing one from `discover_executors`. If `data_axis` is on the block, **skip the Step 3b classification interview** — the user pre-declared the axis; just feed `data_axis` into the `axes.yaml` write that classify-axis would have done. `tasks.py` is already on disk (the interview materialized it from `task_generator`) — Step 6's reuse branch (omit `build_tasks`) picks it up. |
| `register_run` | A pointer to a `@register_run`-decorated function the user has on disk — the canonical Python path. No wrapper to honor; fall through to Step 1's discovery, optionally scoped to `<run_name>` rather than enumerating. |
| `python_module` | A pointer to an importable Python module. No wrapper to honor; fall through to Step 1's discovery, scoped to `<module>:<function>`. The `EXECUTOR` at Step 6 is `python3 -m <module>` (or a one-liner that imports `<function>`) if Step 1 doesn't otherwise resolve it. |

Both `register_run` and `python_module` are **pointers, not wrappers** — they declare which function the worker should target but don't materialize anything. They get resolved through the normal Step 1 discovery flow; only `shell_command` short-circuits it.

If `interview.json` doesn't exist, or `_materialized.entry_point` is absent, **probe whether this is a mature-repo case** that needs an interview the headless worker can't conduct, using the Read/Glob/Grep tools (not shell `grep`/`test`):

- `HAS_MAIN`: use **Glob** for `main.py` and `src/main.py`.
- `HAS_REGISTER_RUN`: use **Grep** for `@register_run` across `notebooks/` and `src/`.

If `main.py` is present and `@register_run` is found nowhere: the experiment has a shell entry point but no `@register_run` declaration the worker can pick up. Record this in `anomalies` (prefix the line `mature_repo_needs_interview: main.py present, no @register_run; ask the user to add @register_run to their entry-point function (two-line edit: import + decorator), or run /wrap-entry-point-hpc for guided setup, then re-invoke`) and stop. Direct decoration is the cheap path — a two-line edit on the function `main.py` ultimately calls; `/wrap-entry-point-hpc` is the guided path that walks the user through that edit and falls back to wrapper materialization only when direct decoration isn't possible.

Otherwise (no mature-repo signals): the rest of the procedure runs unchanged (the notebook-discovery default).

This step is what makes the wrapper-fallback path end-to-end usable: when the `interview` primitive (invoked by `/wrap-entry-point-hpc`) chose to materialize a wrapper rather than direct-decorate, this step reads the wrapper pointer + executor command and threads them into the rest of the submit pipeline. Direct decoration needs no special handling here — Step 1's discovery already finds it.

## Step 1: Discover runs

**Function-first.** The researcher's contract is a `@register_run def run(...)` — a typed-kwarg Python function with no axis declaration, no `tasks.py`, no CLI glue. The function may live in a notebook (`.ipynb`), a script (`.py`), or a package module. Invoke the [discover-runs](../../docs/primitives/discover-runs.md) primitive — it AST-walks all three (skipping `.hpc/`) — instead of shelling `python .hpc/scaffold.py`:

```bash
hpc-agent discover-runs --experiment-dir .
```

The envelope's `data.runs` is a list of `{path, name, gpu, run_signature_sha, flags}`.

- **Bare `/submit-hpc`** (the default) — list every `@register_run` and let the user pick one.
- **`/submit-hpc <file>`** — scope discovery to that one file (a notebook or a script).

Record the picked run's `name`, `gpu`, `flags`, and `run_signature_sha` — the signature hash is the cache key for Step 3's classification lookup.

For environment classification (Step 4) you still need the run's imports; invoke [discover-executors](../../docs/primitives/discover-executors.md) for the matching module's `info.imports` / `info.has_compute_function`, or read the notebook's import cells directly.

### Step 1b: Discover Executors (legacy / env detail)

Invoke [discover-executors](../../docs/primitives/discover-executors.md). The primitive scans `executors/`, `scripts/`, `src/` (in order, falling back to repo root), filters utilities, and classifies each executor by contract.

Map flag set per contract:
- **New-contract** (`info.has_compute_function == true`): if `.hpc/tasks.py` exists, read `FLAGS[<module>]` for the per-executor flag list. If first submit, capture intended flags for Step 6's `build_tasks` scaffold spec.
- **Old-contract** (`info.has_main_guard` only): run `python3 <info.path> --help` to map the CLI interface.

If `discover_executors` returns empty, scaffolding requires an interactive sub-interview which a headless worker cannot run — record the boundary in `anomalies` and stop for the caller to handle.

## Step 2: Parse user intent

The caller has already parsed the user's natural-language request into a list of `(executor_id, axis_shape)` tuples; the result arrives via the invocation `fields`. Flags `--no-canary` and `campaign_id=<slug>` thread through verbatim.

For multi-executor submissions sharing `(ssh_target, remote_path)`, build a **batch spec** — `{"specs": [<per-spec>...], "rsync_excludes": [...]}`; `submit-flow` auto-routes it to the batched path (one rsync + one deploy + N qsubs). Heterogeneous batches raise `spec_invalid`. Why batch rather than N parallel submits: see [submit-flow.md](../../docs/primitives/submit-flow.md). (There is no `skip_preflight` key — preflight is operator-gated via `HPC_AGENT_SKIP_PREFLIGHT`, #275.)

## Step 3: Consume the recorded parallelization verdict (never infer it)

The task list lives in user-written `.hpc/tasks.py` (`total()` + `resolve(task_id)`). Step 6 scaffolds it once per experiment; from then on it is committed and reused on every submit. There are two shapes:

- **Cartesian grid** — each task is one independent cell of a parameter grid. `tasks_example.py` Pattern 1; scaffolded deterministically by [build-tasks-py](../../docs/primitives/build-tasks-py.md) (inside Step 6's resolve-submit-inputs) with **no** `data_axis`. The 80% case.
- **Planner-driven** — the executor iterates a *totally-ordered series* (a walk-forward backtest, an online-learning scan) fanned out across chunks. Splitting a *stateful* series is only correct if each chunk replays the right warm-up; hpc-agent owns that via `hpc_agent.experiment_kit.plan_tasks`, emitted by [build-tasks-py](../../docs/primitives/build-tasks-py.md) when the spec carries a `data_axis`.

**Which shape is not the worker's call.** The classification is resolved *upstream* by the caller — the `hpc-classify-axis` skill (a deterministic AST matcher for the common shapes; the human/LLM decision tree for the long tail) — and recorded in `<experiment>/.hpc/axes.yaml`'s `executors.<run_name>` block, keyed by run name and stamped with the `run_signature_sha` it was classified against. Read it and branch:

- **`executors.<run_name>` present AND its `run_signature_sha` matches the picked run's current `run_signature_sha`** (Step 1) → the verdict is valid:
  - `data_axis.kind == "cartesian"` → no ordered series to split; build a **plain cartesian** `tasks.py` (Step 6, **omit** `data_axis` from `build_tasks`).
  - `independent` / `associative` / `bounded_halo` / `sequential` → planner-driven; thread the `data_axis` block into Step 6's `build_tasks` spec verbatim.
- **No entry, or the `run_signature_sha` drifted** → unresolved. **Do NOT read the executor's code to infer an axis, and do NOT default to a cartesian grid** — a wrong "no series" guess silently mishandles a stateful series and returns plausible-but-wrong numbers. Record an `axis_class` decision with outcome `unclassified` (put `run_name=<name>, run_signature_sha=<sha>` in `why`) and **stop**; the caller runs `hpc-classify-axis`, writes the verdict to `axes.yaml`, and re-invokes this workflow.

The distinction that makes this safe: a *recorded* `cartesian` verdict means the caller's matcher **confidently** found no ordered series; an *absent* verdict means "not yet resolved → escalate." The worker never conflates the two.

> **`DataAxis` ≠ scheduling axes.** `axes.yaml` holds two unrelated things: the `executors.<run>.data_axis` block (this step — *how to split the series correctly*) and `homogeneous_axes` / `axes` (Step 4b / `hpc-axes-init` — *which sweep dimension goes on the task array*). They are orthogonal; classifying the `DataAxis` never touches the scheduling axes.

### 3c: Serial-elision gate (mandatory for a splittable axis — `independent` / `associative` / `bounded_halo`)

Before scaffolding a planner-driven `tasks.py`, prove the classification on a fixture: `hpc_agent.experiment_kit.check_elision` (or `assert_elision_equivalent`) runs the experiment once whole and once split N ways and asserts the results agree. If it fails, the axis is misclassified — widen the halo or fall back to `Sequential()`. This gate is what makes the inference safe: a misclassified axis produces a job that runs fine and returns plausible-but-wrong numbers, and nothing else catches it. Do not skip it, and recommend the experiment repo wire `assert_elision_equivalent` into its CI as a required check.

If the projected task count exceeds `constraints.max_tasks` or ~1000, record a `magnitude_warning` in `anomalies` so the caller can confirm with the user before proceeding.

## Step 4: Auto-Configure Environment

Resolve in order: cluster (from `fields` or `data.latest_run`); `SSH_TARGET` + `REMOTE_PATH` from cluster config; environment classification from `info.imports`:

| Imports detected | Classification | Environment |
|---|---|---|
| `torch`/`tensorflow`/`cuda` | GPU/DL | Load CUDA modules + activate conda env |
| `sklearn`/`xgboost`/`lightgbm` | CPU/ML | Load python modules |
| `numpy`/`pandas` only | CPU/lightweight | Load python modules |

For DL executors with `conda_envs` listed in `clusters.yaml` → record the candidates in `anomalies` for the caller to confirm with the user; the caller re-invokes with the picked env in `fields`. Resource defaults: CPU/ML 1×16G×4h; GPU/DL 4×16G×6h×2gpu (gpu_type=first in cluster's `gpu_types`).

Build rsync excludes from `.gitignore` patterns + the standard set (`__pycache__/`, `*.pyc`, `.git/`, `.claude/`, `.mypy_cache/`) + result directories. You don't need to special-case the generated package: `submit-flow` carves `src/`, `.hpc/tasks.py`, and `.hpc/cli.py` back out of the exclude list itself — the cluster node needs them (`src/` is the executor package built at Step 0; `tasks.py`/`cli.py` are the dispatch contract) — while keeping `.hpc/.build-cache.json` excluded. `.hpc/` otherwise rides rsync (the cluster also needs the in-flight `runs/<run_id>.json`); `submit-flow` protects the framework-deployed `.hpc/` files from `--delete` (see [submit-flow.md](../../docs/primitives/submit-flow.md)).

## Step 4b: Compute Throughput Plan

After grid expansion produces `total_tasks`, invoke [plan-throughput](../../docs/primitives/plan-throughput.md):

```bash
hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s <s>]
```

It reads the cluster's scheduler constraints from `clusters.yaml`, packs the grid into concurrency-bounded waves, and returns `{strategy, total_batches, n_waves, est_total_wall_s, wave_map, ...}`. Thread the returned `wave_map` into `write_run_sidecar(..., wave_map=wave_map)` at Step 6 — the cluster-side combiner reads it from the sidecar. A cluster with no `constraints:` block falls back to scheduler defaults (a single array for a grid under the default `max_array_size`).

## Step 5: Confirm Run Plan (via summarize-submit-plan)

Don't hand-author the summary. Once Step 6 emits the resolved spec via [resolve-submit-inputs](../../docs/primitives/resolve-submit-inputs.md) (`data.submit_spec`), render the canonical confirmation via [summarize-submit-plan](../../docs/primitives/summarize-submit-plan.md):

```bash
hpc-agent summarize-submit-plan --spec /tmp/submit_spec.json
```

The envelope's `data` carries `{headline, body, confirm_prompt}`. Surface `headline`, `body`, and `confirm_prompt` in the worker `result` so the caller can show them to the user. For multi-job submissions, call once per spec and concatenate bodies under one combined header. The primitive flips to a magnitude-warning prompt automatically when `total_tasks > 1000`.

## Step 6: Resolve submit inputs (one call)

The deterministic input-resolution chain — ensure `.hpc/tasks.py` (reuse or scaffold) → compute `run_id`/`cmd_sha` → detect a resumable prior run → build + validate the submit-flow spec → write the per-run sidecar — runs as ONE call to [resolve-submit-inputs](../../docs/primitives/resolve-submit-inputs.md). It folds the former Steps 6a-6d so you read one typed `stage_reached` instead of hand-walking five verbs, and its `resolved` outcome is **fully submit-ready** (spec built AND the per-run sidecar written, so the #171 write-first precondition is already satisfied before Step 7-10).

Assemble the spec from the values Steps 0-5 already resolved:

```json
{
  "run_name": "<run_name>",
  "submit": { "...the build-submit-spec input: profile / cluster / ssh_target / remote_path / total_tasks / backend / job_env knobs..." },
  "sidecar": {
    "executor": "python train.py --seed $SEED",
    "result_dir_template": "results/{run_id}",
    "task_count": "<tasks.total()>",
    "cluster": "<cluster>", "profile": "<run_name>", "campaign_id": "<slug>", "runtime": "uv"
  },
  "build_tasks": { "...ONLY when .hpc/tasks.py is absent: axes + flags_by_executor + (data_axis from Step 3's classification)..." }
}
```

`submit.run_id` / `submit.cmd_sha` and `sidecar.run_id` / `sidecar.cmd_sha` are **placeholders** — resolve-submit-inputs overrides them with the values `compute-run-id` derives (it hashes the materialized `.hpc/tasks.py`), so you never hand-compute the run_id, and the built spec + written sidecar always match the reported one.

**`sidecar.executor` MUST be the real per-task command** (e.g. `python train.py --seed $SEED`), NOT the job-script dispatcher (`python3 .hpc/_hpc_dispatch.py`), which would make the array self-recurse (#162). The dispatcher command belongs in the submit-flow spec's `job_env["EXECUTOR"]` (Step 7-10), not the sidecar's `executor`; `write-run-sidecar` refuses dispatcher-shaped values at intake.

When `.hpc/tasks.py` is **absent**, supply `build_tasks` — the pre-classified `axes` + `flags_by_executor`, with `data_axis` (`{kind, chunks, series_length, halo_expr?, monoid?}`) sourced verbatim from Step 3's `axes.yaml` `executors.<run_name>.data_axis` block when the run is planner-driven (the serial-elision gate, Step 3c, must have passed first; prefer experiment-prefixed axis names so a bare name like `home` can't shadow `$HOME`). When `.hpc/tasks.py` **exists**, OMIT `build_tasks` — it is reused byte-identical so `cmd_sha` matches. The composite scaffolds `.hpc/tasks.py` + the sibling `.hpc/cli.py` via build-tasks-py; commit both with `git` after a `resolved` outcome.

```bash
hpc-agent resolve-submit-inputs --spec spec.json --experiment-dir .
```

Branch on `stage_reached` (read `needs_decision`):

- `needs_scaffold_interview` (`needs_decision=true`) → `.hpc/tasks.py` is absent and no `build_tasks` scaffold spec was supplied — scaffolding needs an executor-discovery + axes interview the headless worker can't run. Record it in `anomalies` (prefix `mature_repo_needs_interview:`) and stop; the user adds `@register_run` / runs `/wrap-entry-point-hpc`, or resolves the axes upstream, then re-invokes.
- `prior_run_found` (`needs_decision=true`) → a live prior run (`complete` / `in_flight`) matches this `cmd_sha`. Record a `prior_run` decision with outcome `found` (`data.prior_run_id` in `why`) and surface to the caller — only the user can choose resume-vs-fresh. (A terminal-but-not-`complete` prior — `failed` / `abandoned`, #276 — is forensic, not live; the composite proceeds to `resolved` and re-submits over it, the canary refiring rather than reusing the dead one.)
- `resolved` (`needs_decision=false`) → inputs resolved: `data.submit_spec` is the built + validated submit-flow spec, `data.sidecar_path` confirms the per-run sidecar is written (#171), and `data.run_id` / `data.cmd_sha` are set. Carry `data.submit_spec` into the submit-pipeline spec's inner `submit.submit` block (Step 7-10) and continue to Step 6b.
- Error envelopes: branch by `error_code` per the primitive's contract.

## Step 6b: Pre-flight Gate (cached per cluster)

Invoke [check-preflight](../../docs/primitives/check-preflight.md) with `--cluster <name>` **and `--spec <the submit-flow spec built in Step 6>`**. Passing the spec lets check-preflight run the same `command -v uv` runtime probe `submit-flow` runs (a `runtime_uv` check), so a `runtime: "uv"` spec against a cluster without `uv` is refused **here**, before any qsub — the gap #275 closed (there is no longer a `skip_preflight` spec field that could silence it).

Cache marker: `~/.claude/hpc/<repo_hash>/preflight-<cluster>.json` (TTL 24h) caches the cluster-environment checks (ssh agent, ssh/rsync on PATH, cluster reachability). If the marker is fresh **and the spec does not set `runtime: "uv"`** → log `preflight: cached <N>m ago — OK` and skip to Step 7. When the spec sets `runtime: "uv"`, run check-preflight `--spec` regardless of the marker — the marker does not cover per-spec `uv` availability (the conda env can change without the cluster env changing).

On `data.all_ok == true`: write/update marker, continue. On any check failure (including `runtime_uv`): do NOT write marker, record a `preflight` decision with outcome `setup_required`, put the failing checks verbatim in `anomalies`, and stop — the user fixes their environment (`hpc-agent setup --cluster <name>`, or installs uv into the cluster env, e.g. `~/.conda/envs/<env>/bin/pip install uv`) and the caller re-invokes.

## Step 6c: Pre-submit campaign validation

Invoke `validate-campaign`:

```bash
hpc-agent validate-campaign --spec validate_campaign.input.json --experiment-dir .
```

Branch on `data.overall`:
- `pass` → proceed.
- `warn` → record warnings in `anomalies`; proceed.
- `fail` → do NOT proceed. Record a `validate_campaign` decision with outcome `fail`, put the `error`-severity findings (`code`/`message`/`suggested_fix` verbatim) in `anomalies`, and stop. **No `--force` flag by design** — the caller edits `.hpc/playbook.yaml` if a rule is wrong, then re-invokes.

## Step 7-10: Invoke `submit-pipeline` (the submit spine, one envelope)

The deterministic post-resolution submit spine — canary-gated submit → post-qsub health check → follow-up-spec pre-staging — runs as ONE call to [submit-pipeline](../../docs/primitives/submit-pipeline.md). It folds what used to be three hand-walked steps — `submit-and-verify` (Steps 7-8) → `verify-submitted` (Step 8b) → `prepare-followup-specs` (Steps 9-10) — so you read one typed `stage_reached` instead of branching each envelope by hand.

The canary is still a GATE (#160): the 1-task canary is submitted, verified to land and produce output, and the main array launches ONLY on success — all INSIDE the primitive, so the agent is never in the submit→poll→submit loop. The deterministic Phase-2 flips (`canary`/`canary_only` off, `skip_rsync_deploy` on — #185/#279) are applied inside it; you never rebuild a second spec by hand.

The spec embeds the canary-gated submit under `submit` (a `submit-and-verify` spec, which itself embeds the `submit-flow` spec under its own `submit`), plus an optional `profile` forwarded to follow-up staging. It matches `schemas/submit_pipeline.input.json`:

```json
{
  "submit": {
    "submit": {
      "profile": "<job_name>", "cluster": "<cluster>", "ssh_target": "user@host",
      "remote_path": "<remote_path>", "job_name": "<job_name>",
      "run_id": "<run_id from 6d>", "total_tasks": <tasks.total()>,
      "backend": "sge", "script": ".hpc/templates/cpu_array.sh",
      "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_RUN_ID": "...", ...},
      "pass_env_keys": null,
      "canary": true, "campaign_id": "<slug>", "runtime": "uv"
    },
    "expect_output": "results/seed_42/metrics.json"
  },
  "profile": "<run_name>"
}
```

`submit.submit.job_env["EXECUTOR"]` is **mandatory and non-empty** — it is the dispatcher command (`python3 .hpc/_hpc_dispatch.py`). Never ship `""` or omit it: the cluster would run `time` with no command and exit 0 in milliseconds, the canary would "succeed", and the main array would fire the same no-op qsub (#191). `build-submit-spec` defaults it; if you hand-craft the fields-file, set it explicitly.

`submit.submit.pass_env_keys` is `null` (or omit it) to forward **every** `job_env` key via `qsub -v` — that is what you almost always want. A **non-empty** list restricts to those keys. **Never `[]`** — an empty list forwards *zero* vars (every `$EXECUTOR`/`$CONDA_ENV`/`$REPO_DIR` unset), producing the same broken job; submit-flow refuses `[]` at intake (#192).

There is **no `skip_preflight`** field (#275 Fix 2): preflight — including the cluster-side `command -v uv` check — runs inside `submit-flow` and is skippable only by the operator env var `HPC_AGENT_SKIP_PREFLIGHT`, never by a field on the spec the agent authors, so the uv guard can't be silenced into letting a uv-less run reach qsub. For GPU jobs: `submit.submit.script: ".hpc/templates/gpu_array.sh"` (SGE) or `gpu_array.slurm` (SLURM).

```bash
hpc-agent submit-pipeline --spec spec.json --experiment-dir .
```

Branch on the single envelope's `stage_reached` (`needs_decision` tells you whether a genuine decision is handed back):

- `deduped` (`needs_decision=false`) → the main run already ran; original jobs are live. Record `deduped: <run_id>` in `anomalies`; switch to the status workflow. Do NOT re-submit.
- `canary_failed` (`needs_decision=true`) → the canary failed the gate. Record a `canary` decision with outcome = `data.failure_kind` (`dispatcher_failed`/`import_error`/`oom_killed`/`missing_output`/`timeout`), put the stderr tail verbatim in `anomalies`, then **stop. The main array never launched** — `data.job_ids` is empty.
- `verify_submitted_failed` (`needs_decision=true`) → the array launched but did not all land queued/running — an SGE array can land in `Eqw`, a SLURM job can be held, both of which a plain alive-check reports as "present." Record the offending job ids + states from `data.verify_submitted_result` verbatim in `anomalies`, then stop. (See the state taxonomy in [scheduler-states.md](../../docs/reference/scheduler-states.md); the failure-mode table below maps a bad state to its fix.)
- `complete` (`needs_decision=false`) → the canary passed, the main array is live and healthy, and the follow-up specs are pre-staged (`data.monitor_spec_path` / `data.aggregate_spec_path`, #278 — so `/monitor-hpc` and `/aggregate-hpc` skip their interview round-trip, each `cmd_sha`-gated against the journal). Capture `data.run_id` / `data.job_ids` and report.
- Error envelopes: branch by `error_code` per the primitive's contract.

Do not cache run config in conversational memory. `submit-flow` persists the full v2 config snapshot (executor, cluster, remote_path, env, resources) to the run sidecar; any later step recovers it with `hpc-agent load-context`. Conversational memory is lost on context compaction or a session restart — the sidecar is not.

Report after a `complete` stage: job ID, executor(s), grid dimensions, total tasks, cluster, verified scheduler state. The caller suggests `/monitor-hpc` to track progress.

The journal write happens inside `submit-flow` (which `submit-pipeline` calls) via `runner.submit_and_record`. For multi-executor submissions (one sidecar per executor), invoke `submit-pipeline` once per submitted job — each call writes its own sidecar.

## Common failure modes

When the `verify_submitted_failed` stage surfaces a job in a failed state, or a later check surfaces task failures, map the symptom:

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) for >30 min | Resource unavailable | Check `sinfo`; try a different partition |
| Memory exceeded | Exceeded the memory limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded the time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Environment not set up | Check the modules / conda_env |
| rsync / scp transfer failure | SSH key issue | Verify `ssh $SSH_TARGET hostname` first |
| `--<flag>` not recognized | The executor does not accept that argument | Check `--help`; the flag must be in the executor's `FLAGS` / CLI |

If the requested run names a CLI flag the executor does not accept, record it in `anomalies` and stop before submitting — a missing flag fails every task in the array.

## Notes

- **SSH env passthrough**: caller must forward `SSH_AUTH_SOCK` and `SSH_AGENT_PID` or every cluster call hangs on auth. The user runs `hpc-agent setup --cluster <name>` once per machine to probe the environment and populate the 24h cache marker Step 6b reads.
- **Scheduler rate limits**: serialize submits to a single cluster; most schedulers cap at ~1/sec. Sleep 1s between back-to-back calls or expect `scheduler_throttled`.
- **Idempotency**: `submit-flow` is replay-safe on `run_id`. If `data.deduped: true`, original cluster jobs are running — do NOT re-invoke.
- **No cancel/abort**: hpc-agent has no kill primitive. If the user decides an experiment is bad, the caller stops monitoring; cluster jobs run to walltime.
- `--dry-run` never touches the cluster and never writes to the journal — safe to run repeatedly.
- The cluster-side template translates the scheduler's per-task index (`SGE_TASK_ID` / `SLURM_ARRAY_TASK_ID`) into `HPC_TASK_ID` (0-based) before exec'ing `$EXECUTOR`, which then imports `.hpc/tasks.py`, calls `tasks.resolve(HPC_TASK_ID)`, and runs the executor command from the sidecar with kwargs merged into the env.
