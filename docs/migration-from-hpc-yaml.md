# Migrating from `hpc.yaml`

`hpc.yaml` was the optional declarative experiment-config file in
earlier versions of claude-hpc. It is **gone** in the current release —
the agent never reads it, and no command writes one. If you have an
`hpc.yaml` lying around, claude-hpc ignores it; behavior is identical
whether the file is present or absent.

This is a one-shot deletion, not a deprecation cycle. There is no
warning, no fallback read path, no migration helper. If you previously
hand-edited `hpc.yaml`, follow the table below to find the new home for
each field, then re-run `/submit` once — your sidecar will accumulate
the resolved config from then on.

## Field-by-field mapping

Every load-bearing field migrated to one of three places: a per-run
sidecar field (populated by `/submit` at write time), a hardcoded
framework default (with CLI override), or the new `.hpc/stages.py` for
multi-stage DAGs.

| `hpc.yaml` field | New home | Notes |
|---|---|---|
| `profiles[*].run` | sidecar `executor` | Already present in v1; just re-run `/submit`. |
| `profiles[*].resources` (cpus, mem, walltime, gpus, gpu_type) | sidecar `resources` (v2) | First-class field; populated by `/submit`. |
| `profiles[*].env` (modules, conda_env) | sidecar `env` (v2) | First-class field. |
| `profiles[*].env_group` | sidecar `env_group` (v2) | First-class field. |
| `profiles[*].constraints` | sidecar `constraints` (v2) | First-class field; merged with `clusters.yaml` constraints field-wise via `parse_constraints`. |
| `profiles[*].auto_retry` | sidecar `auto_retry` (v2) **OR** `runner.DEFAULT_AUTO_RETRY_POLICY` | If you had a custom policy, restate it on `/submit`. Otherwise the framework defaults (gpu_oom / system_oom / walltime / node_failure with conservative caps) take effect automatically. |
| `profiles[*].results.{require_outputs, expect_output, aggregate_cmd}` | sidecar `aggregate_defaults` (v2) | `cmd_aggregate` reads this; CLI flags `--require-outputs` / `--expect-output` continue to override. |
| `profiles[*].gpu_fallback` | sidecar `gpu_fallback` (v2) | First-class field. |
| `profiles[*].max_retries` | sidecar `max_retries` (v2) | First-class field. |
| `profiles[*].runtime` | sidecar `runtime` (v2) | First-class field; `"uv"` triggers `uv sync` in templates. |
| `profiles[*].name` (the dict key) | sidecar `profile` (v2) | The label distinguishing this submission shape. |
| top-level `cluster` | sidecar `cluster` (v2) | First-class field. |
| top-level `project` | sidecar `project` (v2) | First-class field. |
| top-level `remote_path` | sidecar `remote_path` (v2) | First-class field. |
| top-level `metrics` (sort order) | hardcoded alphabetical default + CLI override | No persisted setting. |
| top-level `cluster_envs` | `clusters.yaml` `conda_envs` | Already in `clusters.yaml`; remaining overrides should fold there. |
| `stages:` (multi-stage DAG) | **`.hpc/stages.py`** exposing `def stages() -> list[dict]` | New file. JSON Schema at `claude_hpc/schemas/stages.input.json`. The only legacy field with no sidecar home. |

## Step-by-step migration

1. **Open your `hpc.yaml` next to this doc.**
2. **For each profile, just re-run `/submit`.** The conversational
   interview asks the same questions the yaml answered (cluster,
   profile, resources, env, etc.) and writes the resolved values to the
   per-run sidecar at `.hpc/runs/<run_id>.json`. Subsequent commands
   (`/aggregate`, `/monitor-hpc`, `/resubmit`, `/campaign`) read context from
   the sidecar rather than the yaml.
3. **For multi-stage `stages:` DAGs, port to `.hpc/stages.py`.** The
   helper `claude_hpc.orchestrator.stages.from_yaml_dict` is *not* shipped —
   convert by hand. The shape is straightforward: the dict-of-stages
   `{name: {run, depends_on, resources, ...}}` becomes a list of dicts
   `[{"name": ..., "run": ..., "depends_on": ..., ...}]`. Validate via
   `claude_hpc.orchestrator.stages.load_stages(experiment_dir)` — schema
   errors (missing `name`/`run`, unknown keys, broken `depends_on`)
   raise immediately.
4. **Delete `hpc.yaml` from the repo.** It is not read; deleting it
   removes a source of confusion for future readers.

## What stays the same

- `clusters.yaml` is unchanged. Cluster infrastructure config — host,
  scheduler, scratch path, modules, conda envs, GPU types, constraints
  — still ships in the package and is overrideable via
  `HPC_CLUSTERS_CONFIG`.
- `.hpc/tasks.py` is unchanged. The user-authored
  `total()` / `resolve(task_id)` convention is the parallelization axis;
  per-task kwargs still come from there.
- The CLI envelopes are unchanged. Subcommands still emit one-line JSON
  on stdout following `docs/cli-spec.md` and `schemas/envelope.json`.

## What's new alongside the deletion

Two additions arrived in the same release as the `hpc.yaml` removal:

1. **Sidecar v2 schema.** `SIDECAR_SCHEMA_VERSION` bumped from 1 to 2
   with the new fields above. v1 sidecars on disk continue to load —
   `read_run_sidecar` backfills missing v2 keys to `None` so callers
   see a uniform shape.
2. **Closed-loop campaigns.** A new `campaign_id` field on the v2
   sidecar tags submits as part of an iterative campaign;
   `hpc-mapreduce campaign status` / `campaign list` and `/campaign`
   make the closed-loop pattern first-class. See `docs/campaign.md` for
   the full feature.

If you hadn't been using `hpc.yaml`, neither of these changes affects
you — open-loop `/submit` works exactly as before.
