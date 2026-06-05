---
name: prepare-followup-specs
verb: scaffold
side_effects:
- writes-followup-specs: <experiment_dir>/monitor_spec.json + aggregate_spec.json
idempotent: true
idempotency_key: run_id
error_codes: []
backed_by:
  cli: hpc-agent prepare-followup-specs --experiment-dir <experiment_dir> --run-id
    <run_id> [--cmd-sha <cmd_sha>] [--profile <profile>]
  python: hpc_agent.ops.prepare_followup_specs.prepare_followup_specs
---
# prepare-followup-specs

Pre-stage the two followup specs at submit time so a later `/monitor-hpc`
and `/aggregate-hpc` can skip the operator interview. From the state
already known when a run is submitted, this writes two small JSON files
into the experiment dir:

- `monitor_spec.json`
- `aggregate_spec.json`

## Why the specs are small

`monitor-flow` and `aggregate-flow` only strictly require `run_id` — they
derive cluster, ssh_target, remote_path, and the rest from the run
sidecar on disk. So each pre-staged spec carries only the handful of
fields that can't be re-derived later:

`monitor_spec.json`:

```json
{
  "run_id": "<run_id>",
  "cmd_sha": "<cmd_sha or null>",
  "wait_terminal": null,
  "prepared_by": "prepare-followup-specs",
  "prepared_at": "<utc iso>"
}
```

`aggregate_spec.json`:

```json
{
  "run_id": "<run_id>",
  "profile": "<profile or null>",
  "cmd_sha": "<cmd_sha or null>",
  "stage": null,
  "allow_partial": null,
  "prepared_by": "prepare-followup-specs",
  "prepared_at": "<utc iso>"
}
```

## The operator-choice sentinel nulls

The fields left `null` are the ones that are genuinely an **operator
choice** at followup time, not a fact known at submit:

- monitor `wait_terminal` — block until the run reaches a terminal state
  vs. take a one-shot snapshot.
- aggregate `stage` — which stage to aggregate.
- aggregate `allow_partial` — whether a partial result set is acceptable.

`null` means "not decided at submit — the followup skill prompts for it."
They are deliberately NOT defaulted here, so pre-staging never silently
picks a blocking-vs-snapshot monitor or a partial-vs-complete aggregate on
the operator's behalf. The pre-staged spec saves the operator from
re-stating the run identity, not from making the call that's theirs to
make.

## The cmd_sha staleness gate

`cmd_sha` is the staleness gate. The consuming skill validates the
pre-staged `cmd_sha` against the journal before honoring the spec: if the
code changed since submit (the journal's `cmd_sha` no longer matches), the
pre-staged spec is stale and the skill falls back to the interview rather
than monitoring/aggregating against an out-of-date plan. That gate is
wired separately — this primitive only **writes** the files; it does not
read the journal or enforce the gate itself.

## Inputs / outputs

See `hpc_agent/schemas/prepare_followup_specs.{input,output}.json`. Input
requires `experiment_dir` + `run_id`; `cmd_sha` and `profile` are
optional. Output reports the two absolute spec paths plus the `run_id` and
`cmd_sha` echoed back.

## Idempotency

Keyed on `run_id`. A re-run for the same run overwrites both files
atomically (`atomic_write_json`) with equivalent content — only the
`prepared_at` timestamp refreshes. Pure local writes; no SSH.
