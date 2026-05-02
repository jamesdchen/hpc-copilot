# `hpc-mapreduce` CLI Specification

Authoritative contract for the shell CLI shipped at
`hpc_mapreduce/agent_cli.py` (entry point `hpc-mapreduce`). This is the
agent-facing surface — designed to be invoked by Bash from MARs
orchestrators, scripts, and cron. The slash-command surface in
`slash_commands/commands/` is documented elsewhere; both surfaces share
the atomic-ops layer at `slash_commands/runner.py`.

This document is the human-readable surface. The machine-readable
contract lives under `hpc_mapreduce/schemas/`:

- `envelope.json` — universal stdout envelope (success / error).
- `submit.input.json`, `submit.output.json` — `submit --spec` shape.
- `status.output.json` — `status` data block.
- `capabilities.output.json` — `capabilities` data block.
- `preflight.output.json` — `preflight` data block.
- `resubmit.input.json` — `resubmit --spec` shape.
- `campaign.output.json` — `campaign status` / `campaign list` data block.
- `stages.input.json` — output shape of `.hpc/stages.py::stages()` (loaded by `hpc_mapreduce.job.stages.load_stages`; not a CLI input but agents authoring `stages.py` should validate against this).

Agents constructing/validating envelopes should validate against the
JSON Schema, not parse this markdown.

## Conventions

- Stdout is exactly one line: a single JSON envelope. No banners, no logs.
- Stderr carries JSON-per-line log records (debug for humans).
- Every subcommand accepts `--experiment-dir` (defaults to CWD) unless
  the operation is global (e.g. `clusters list`, `capabilities`).
- Subcommands with non-trivial inputs accept `--spec path/to/spec.json`.
- Idempotent subcommands set `"idempotent": true` on the success envelope.
- `hpc-mapreduce --version` prints the package version and exits 0.

## Universal envelope

### Success

```json
{"ok": true, "idempotent": <bool>, "data": {<subcommand-specific>}}
```

### Error

```json
{
  "ok": false,
  "error_code": "<one of 12>",
  "message": "<human-readable>",
  "category": "user|cluster|network|internal",
  "retry_safe": <bool>,
  "remediation": "<optional>"
}
```

Source of truth: `hpc_mapreduce/schemas/envelope.json` and the
`HpcError` hierarchy in `slash_commands/errors.py`.

## Exit code → error_code mapping

Wired in `hpc_mapreduce/agent_cli.py` (`_EXIT_CODE_BY_CATEGORY`).

| Exit | Category | Meaning | error_codes that map here |
|---|---|---|---|
| 0 | — | success | (no error envelope) |
| 1 | `user` | caller-fixable | `spec_invalid`, `executor_not_found`, `cluster_unknown`, `config_invalid` |
| 2 | `cluster`, `network` | remote/cluster issue | `ssh_unreachable`, `scheduler_throttled`, `remote_command_failed`, `combiner_failed`, `cluster_timeout`, `outputs_missing` |
| 3 | `internal` | bug in framework or corrupt state | `journal_corrupt`, `internal` |

`preflight` returns 2 when any check fails (it is a `cluster`-class
diagnostic, even though the envelope is `ok=true`).

## Subcommands

### `capabilities`

Purpose: machine-readable feature flags. No side effects.

Args: none.

`data` shape:

```json
{
  "version": "0.2.0",
  "subcommands": ["submit", "status", "...", "build-executor"],
  "supported_schedulers": ["sge", "slurm"],
  "schemas_dir": "/abs/path/hpc_mapreduce/schemas",
  "journal_dir": "/abs/path/.claude/hpc",
  "ssh_multiplexing": true
}
```

Idempotent: yes. Error codes: none in normal use; `internal` on bug.
Exit: 0.

Example:

```bash
hpc-mapreduce capabilities
```

```json
{"ok": true, "idempotent": true, "data": {"version": "0.2.0", "...": "..."}}
```

### `preflight`

Purpose: health check for the local environment. SSH agent, `ssh`/`rsync`
on PATH, `clusters.yaml` parses, optionally TCP-probe one cluster on :22.

Args:

| Flag | Required | Meaning |
|---|---|---|
| `--cluster <name>` | no | TCP-probe `clusters.yaml`'s host on port 22. |

`data` shape:

```json
{
  "all_ok": false,
  "checks": [
    {"name": "ssh_auth_sock", "ok": true,  "detail": "agent at /tmp/..."},
    {"name": "ssh_on_path",   "ok": true,  "detail": "/usr/bin/ssh"},
    {"name": "rsync_on_path", "ok": true,  "detail": "/usr/bin/rsync"},
    {"name": "clusters_yaml_parses", "ok": true, "detail": "3 clusters defined"},
    {"name": "cluster_tcp_22", "ok": false, "detail": "host:22 — refused"}
  ]
}
```

Idempotent: yes. Error codes: none expected; failures are reported as
`checks[].ok = false`. Exit: 0 if `all_ok`, else 2.

Example:

```bash
hpc-mapreduce preflight --cluster hoffman2
```

### `clusters list`

Purpose: list all clusters in `clusters.yaml`.

Args: none.

`data` shape:

```json
{"clusters": [{"name": "hoffman2", "host": "hoffman2.idre.ucla.edu", "scheduler": "sge"}]}
```

Idempotent: yes. Error codes: `config_invalid` (yaml malformed). Exit: 0 / 1.

### `clusters describe <name>`

Purpose: print one cluster's full config block.

Args: positional `name`.

`data` shape: `{"name": "<name>", "config": {<full cluster block>}}`.

Idempotent: yes. Error codes: `cluster_unknown`, `config_invalid`. Exit: 0 / 1.

### `discover`

Purpose: list executor scripts under `--experiment-dir` (any `.py` file
that looks like a CLI per `hpc_mapreduce.job.discover.is_executor_source`).

Args: `--experiment-dir`.

`data` shape (validated against `schemas/discover.output.json`):

```json
{"executors": [{"name": "train", "path": "/abs/.../scripts/train.py", "cli_framework": "argparse", "has_main_guard": true}]}
```

When the experiment-dir has a MARs `meta.json` at its root, the envelope
also includes a `meta` block with the fields claude-hpc knows about plus
a path-derived tier:

```json
{
  "executors": [...],
  "meta": {
    "experiment_id": "run-001-foo",
    "seed": 42,
    "purpose": "ridge baseline",
    "tier": 2
  }
}
```

`tier` is `1` for `probes/probe-*` paths with `probe.py` at root, `2` for
`runs/run-*` paths with `scripts/`, and `null` otherwise. The MARs layout
filter narrows discovery to `scripts/` (Tier-2 entrypoints) and the
root-level `probe.py` (Tier-1) — never `src/`, MARs's modules-only dir.

Idempotent: yes. Error codes: `internal` only. Exit: 0.

### `list-in-flight`

Purpose: list every journal record in `--experiment-dir` whose
`status` is `in_flight`. The recovery path for a fresh Claude Code or
agent session.

Args: `--experiment-dir`.

`data` shape:

```json
{
  "runs": [
    {
      "run_id": "sweep_3a7b8c9d",
      "profile": "sweep",
      "cluster": "hoffman2",
      "job_ids": ["12345"],
      "total_tasks": 24,
      "submitted_at": "2026-04-28T17:00:00+00:00",
      "last_status": {"complete": 12, "running": 8, "...": "..."}
    }
  ]
}
```

Idempotent: yes. Error codes: `journal_corrupt` if a run file has a
mismatched `schema_version`. Exit: 0 / 3.

### `campaign status`

Purpose: report per-iteration reduced metrics for one closed-loop
campaign. Walks every sidecar tagged with `--campaign-id`, runs
`reduce_metrics` on each iteration's result directories, and emits the
history dict-list. Pure local read; no SSH, no scheduler.

Args: `--experiment-dir`, `--campaign-id <id>` (required).

`data` shape (validated against `schemas/campaign.output.json`'s
`status_data`):

```json
{
  "campaign_id": "ml_ridge_optuna_q1",
  "iterations": 12,
  "in_flight": 0,
  "history": [
    {"loss": 0.5, "n_samples": 1},
    {"loss": 0.42, "n_samples": 1},
    "..."
  ],
  "run_ids": [
    "20260101-000000-aaaaaaa",
    "20260101-000001-bbbbbbb",
    "..."
  ]
}
```

`history` is oldest-first; pending iterations whose result directories
don't exist yet contribute `{}`. `in_flight` counts journal records with
this campaign tag still in `in_flight` status.

Idempotent: yes. Exit: 0.

### `campaign list`

Purpose: list every campaign with at least one sidecar in this
experiment, with iteration counts. Untagged (open-loop) sidecars are
excluded.

Args: `--experiment-dir`.

`data` shape (validated against `schemas/campaign.output.json`'s
`list_data`):

```json
{
  "campaigns": [
    {"campaign_id": "ml_ridge_optuna_q1", "iterations": 12},
    {"campaign_id": "walk_forward_2026q1", "iterations": 26}
  ]
}
```

Idempotent: yes. Exit: 0.

### `status`

Purpose: poll cluster status for one run. SSH-issues a fresh
`python -m hpc_mapreduce.reduce.status` on the remote, updates the
journal record's `last_status`, and writes a `<run_id>.last_status.json`
cache file next to the journal record.

Args: `--experiment-dir`, `--run-id <id>` (required).

`data` shape (validated against `schemas/monitor-hpc.output.json`):

```json
{
  "run_id": "sweep_3a7b8c9d",
  "lifecycle_state": "in_flight",
  "last_status": {"complete": 12, "running": 8, "pending": 4, "failed": 0, "unknown": 0, "checked_at": "2026-04-28T17:05:00+00:00"},
  "combined_waves": [],
  "failed_waves": []
}
```

Idempotent: yes (each call refreshes the cached snapshot; no cluster
state is mutated). Error codes: `journal_corrupt` (no record),
`ssh_unreachable`, `remote_command_failed`. Exit: 0 / 2 / 3.

Example:

```bash
hpc-mapreduce status --run-id sweep_3a7b8c9d
```

### `submit`

Purpose: record a submission in the journal. The actual `qsub`/`sbatch`
is the caller's responsibility — `submit` only persists the bookkeeping
needed for `/monitor-hpc` to pick up the run later.

Idempotent on `run_id`: a retried call with the same `run_id` returns
the existing record with `deduped: true` and emits no new side effects.
See `slash_commands/runner.py:submit_and_record`.

Args:

| Flag | Required | Meaning |
|---|---|---|
| `--experiment-dir` | no | default CWD |
| `--spec spec.json` | yes | input spec |
| `--dry-run` | no | validate spec, report shape, no journal write |
| `--from-meta` | no | overlay missing `profile` / `job_name` from `<experiment-dir>/meta.json` `experiment_id` (setdefault; silent no-op when meta is absent) |

`--spec` shape (validated against `schemas/submit.input.json`):

```json
{
  "profile": "sweep",
  "cluster": "hoffman2",
  "ssh_target": "user@host",
  "remote_path": "/u/home/user/myexp",
  "job_name": "sweep-2026-04-28",
  "run_id": "sweep-20260428-153012-3a7b8c9d",
  "job_ids": ["12345"],
  "total_tasks": 24,
  "runtime": null
}
```

`run_id` is the primary (and only) identity field; the per-run sidecar
lives at `.hpc/runs/<run_id>.json`.

Optional `runtime` accepts `"uv"` or `null` (default). When `"uv"`,
`HPC_RUNTIME=uv` is exported into the job environment so the template's
`uv sync` preamble fires before dispatch.

`data` shape (validated against `schemas/submit.output.json`):

```json
{
  "run_id": "sweep-20260428-153012-3a7b8c9d",
  "job_ids": ["12345"],
  "total_tasks": 24,
  "deduped": false
}
```

`--dry-run` returns:

```json
{"would_launch": 24, "profile": "sweep", "cluster": "hoffman2",
 "run_id": "sweep-20260428-153012-3a7b8c9d", "dry_run": true}
```

Idempotent: yes. Error codes: `spec_invalid` (missing required
fields), `config_invalid` (spec unreadable), `journal_corrupt`. Exit: 0 / 1 / 3.

### `aggregate`

Purpose: run the on-cluster combiner for one wave (`combine_wave` in
the runner). Records `combined_waves` / `failed_waves` to the journal.

Args:

| Flag | Required | Meaning |
|---|---|---|
| `--experiment-dir` | no | default CWD |
| `--run-id <id>` | yes | target run |
| `--wave <int>` | yes | wave index |
| `--output-dir <path>` | no | default `<experiment-dir>/_aggregated/<run_id>/` |
| `--force` | no | re-run combiner even if wave appears combined |

`data` shape:

```json
{
  "run_id": "sweep_3a7b8c9d",
  "wave": 0,
  "combined": true,
  "output_dir": "/abs/.../_aggregated/sweep_3a7b8c9d",
  "stdout_tail": "...",
  "stderr_tail": ""
}
```

Idempotent: yes on success (`combined=true` is recorded once). Error
codes: `journal_corrupt` (no record), `spec_invalid` (missing
`--wave`), `ssh_unreachable`, `remote_command_failed`. Exit: 0 on
success, 2 if combiner failed.

### `resubmit`

Purpose: record a resubmission attempt in the journal. The actual
`qsub`/`sbatch` is the caller's responsibility; this updates per-task
retry counters and (optionally) the active `job_ids` list.

Args: `--experiment-dir`, `--run-id <id>` (required), `--spec spec.json` (required).

`--spec` shape (validated against `schemas/resubmit.input.json`):

```json
{
  "failed_task_ids": [3, 7, 12],
  "category": "gpu_oom",
  "overrides": {"mem": "32G"},
  "new_job_ids": ["12346"]
}
```

`data` shape:

```json
{
  "run_id": "sweep_3a7b8c9d",
  "retries": {"3": {"attempts": 1, "category": "gpu_oom", "overrides": {"mem": "32G"}}},
  "job_ids": ["12346"]
}
```

Idempotent: **no** — each call increments per-task `attempts`. Error
codes: `spec_invalid` (empty `failed_task_ids`, missing `category`),
`journal_corrupt` (no record). Exit: 0 / 1 / 3.

### `reconcile`

Purpose: self-healing resume. Fan-out three SSH calls in parallel:
fresh status report, list of `_combiner/wave_*.json`, alive job IDs.
Writes the merged result back atomically. If `job_ids` are non-empty
but none are alive, flips `lifecycle_state` to `abandoned`.

Args: `--experiment-dir`, `--run-id <id>` (required), `--scheduler {sge,slurm}` (required).

`data` shape:

```json
{
  "run_id": "sweep_3a7b8c9d",
  "lifecycle_state": "abandoned",
  "combined_waves": [0],
  "failed_waves": [],
  "last_status": {"complete": 24, "...": "...", "checked_at": "..."}
}
```

Idempotent: yes. Error codes: `journal_corrupt`, `ssh_unreachable`,
`remote_command_failed`. Exit: 0 / 2 / 3.

### `build-executor`

Purpose: scaffold a new executor file from `hpc_mapreduce/templates/starters/`.

Args:

| Flag | Required | Meaning |
|---|---|---|
| `--name <stem>` | yes | output filename stem (no `.py`) |
| `--output-dir <dir>` | no | default CWD |
| `--type {plain}` | no | default `plain` |
| `--force` | no | overwrite existing destination |

Type → starter template:

| Type | Source file (under `hpc_mapreduce/templates/starters/`) |
|---|---|
| `plain` | `executor_template.py` |

Per-task fan-out is expressed inline in `.hpc/tasks.py`
(`itertools.product`, slicing, date-window comprehensions). The
canonical reference is `hpc_mapreduce/templates/tasks_example.py`; the
agent walks the user through adapting it during `/submit` Step 6.

`data` shape: `{"path": "/abs/.../<name>.py", "type": "plain", "source": "/abs/.../executor_template.py"}`.

Idempotent: **no** — file creation has side effects, refuses to
overwrite without `--force`. Error codes: `spec_invalid` (unknown
type, refusing overwrite), `config_invalid` (template missing on disk).
Exit: 0 / 1.
