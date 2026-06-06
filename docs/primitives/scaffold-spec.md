---
name: scaffold-spec
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent scaffold-spec [--experiment-dir <dir>] --verb <verb> [--cluster <cluster>]
    [--run-name <run_name>] [--from-context]
  python: hpc_agent.ops.scaffold_spec.scaffold_spec
---
# scaffold-spec

Emit a populated, **schema-valid** `--spec` skeleton for another verb,
pulling values from the read-only context sources so the agent stops
divining the target schema one `spec_invalid` at a time (#287).

When an agent needs to invoke a verb that takes a `--spec` JSON, it has no
way to get a valid skeleton: each missing field, wrong type, or stray
`extra=forbid` key surfaces ONE at a time as a `spec_invalid` envelope, and
the agent walks the schema by failed-validation feedback (the 2026-06-05
demo burned 11 rounds on `resolve-submit-inputs` alone). `scaffold-spec`
collapses that loop to **one scaffold + one edit + one invoke**.

## How it populates

It composes four read-only sources into the target verb's input shape:

| source | supplies |
| --- | --- |
| `clusters.yaml` (`--cluster`) | `ssh_target`, `backend` (scheduler), `remote_path` (from `scratch`), the COHERENT `conda_source`+`conda_env` pair |
| `compute-run-id` (`--run-name`) | real `run_id` + `cmd_sha` hashes (not placeholders) — needs `.hpc/tasks.py` |
| `load-context` | the latest run's `profile`, `cluster`, `remote_path`, `task_count`, `resources`, `env`, `runtime`, `result_dir_template` |
| `discover-executors` | the run name when a single executor is present and none was given |

It emits only the coherent conda pair — a `conda_env` without a
`conda_source` (#281) crashes the cluster preamble, so it is never emitted.

## Inputs

Flags only — there is no `--spec` (and so no input schema):

- `--verb` (required) — the target verb to scaffold. Supported:
  `build-submit-spec`, `resolve-submit-inputs`, `validate-campaign`, and
  `campaign-run` (which nests three workflow specs — submit-pipeline →
  submit-and-verify → submit-flow, status-pipeline → monitor-flow,
  aggregate-flow).
- `--cluster` — clusters.yaml entry (default: the latest run's cluster, or
  the only configured one).
- `--run-name` — fed to `compute-run-id` (default: the latest run's
  profile, or the only discovered executor).
- `--from-context` — populate from context. The default and only mode
  today; accepted for forward-compatibility.

## Outputs

Matches `hpc_agent/schemas/scaffold_spec.output.json`
(`ScaffoldSpecResult`):

- `spec` — the populated skeleton, already validated against the target
  verb's input model. Pass it (after filling `unresolved_fields`) to
  `hpc-agent <verb> --spec`.
- `unresolved_fields` — dotted paths (e.g. `submit.ssh_target`,
  `sidecar.executor`) whose values are schema-valid PLACEHOLDERS context
  could not supply. The caller fills/overrides these — they validate, but
  they are not real.
- `sources` — per-field provenance (which source populated each value, or
  a `placeholder — ...` marker).
- `supported_verbs` — the verbs scaffold-spec populates today.
- `warnings` — context-gathering degradations (clusters.yaml unreadable,
  `.hpc/tasks.py` absent so run_id/cmd_sha are placeholders, …).

## Errors

- `spec_invalid` — an unsupported `--verb` (the message names the supported
  set). Also raised — and not expected in practice — if a scaffolder ever
  emits a structurally invalid skeleton, since every skeleton is validated
  against the target model before it is returned.

## Idempotency

Read-only (`verb: query`, no side effects): it never writes the spec to
disk — the skeleton rides the envelope `data`. Two calls with the same
inputs and unchanged on-disk context return the same skeleton.

## Usage

```bash
hpc-agent scaffold-spec --verb resolve-submit-inputs --from-context \
  --experiment-dir . --cluster hoffman2 --run-name monte_carlo_pi
# → read data.spec, override data.unresolved_fields, then:
hpc-agent resolve-submit-inputs --spec .hpc/resolve_spec.json --experiment-dir .
```
