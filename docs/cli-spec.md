# `hpc-mapreduce` CLI Specification

Cross-cutting contract for the shell CLI shipped at `hpc_mapreduce/agent_cli.py` (entry point `hpc-mapreduce`). Per-subcommand contracts live in **[`docs/primitives/`](primitives/)** — one file per operation, with full input/output/error/idempotency contracts in YAML frontmatter. This file documents only what's shared across every subcommand: stdout envelope shape, exit-code mapping, and the schemas list.

The slash-command surface in `slash_commands/commands/` is documented elsewhere; both surfaces compose from the primitive layer.

## Schemas (machine-readable contracts)

The JSON Schemas under `hpc_mapreduce/schemas/` are the source of truth — agents constructing or validating envelopes should validate against the schema, not parse this markdown.

- `envelope.json` — universal stdout envelope (success / error).
- `submit.input.json`, `submit.output.json` — `submit --spec` shape.
- `status.output.json` — `status` data block.
- `capabilities.output.json` — `capabilities` data block.
- `preflight.output.json` — `preflight` data block.
- `resubmit.input.json` — `resubmit --spec` shape.
- `campaign.output.json` — `campaign status` / `campaign list` data block.
- `discover.output.json` — `discover` data block.
- `stages.input.json` — output shape of `.hpc/stages.py::stages()` (loaded by `hpc_mapreduce.job.stages.load_stages`; not a CLI input but agents authoring `stages.py` should validate against this).

## Conventions

- Stdout is exactly one line: a single JSON envelope. No banners, no logs.
- Stderr carries JSON-per-line log records (debug for humans).
- Every subcommand accepts `--experiment-dir` (defaults to CWD) unless the operation is global (e.g. `clusters list`, `capabilities`).
- Subcommands with non-trivial inputs accept `--spec path/to/spec.json`.
- Idempotent subcommands set `"idempotent": true` on the success envelope.
- `hpc-mapreduce --version` prints the package version and exits 0.

## Universal envelope

### Success

```json
{"ok": true, "idempotent": <bool>, "data": {<subcommand-specific>}}
```

Optionally a top-level `partial_errors` array carries `{code, detail}`
records when the operation succeeded but one or more cluster-side data
sources were degraded (`qhost_failed`, `scontrol_failed`,
`qstat_unavailable`, `qacct_unavailable`, `malformed_row`, ...). This
is distinct from `data.errors`, which is a primitive-internal field
some subcommands keep for back-compat. Consumers should prefer
`partial_errors` when present.

```json
{"ok": true, "idempotent": true, "data": {...},
 "partial_errors": [{"code": "qhost_failed", "detail": "qhost timed out"}]}
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

Source of truth: `hpc_mapreduce/schemas/envelope.json` and the `HpcError` hierarchy in `slash_commands/errors.py`.

## Exit code → error_code mapping

Wired in `hpc_mapreduce/agent_cli.py` (`_EXIT_CODE_BY_CATEGORY`).

| Exit | Category | Meaning | error_codes that map here |
|---|---|---|---|
| 0 | — | success | (no error envelope) |
| 1 | `user` | caller-fixable | `spec_invalid`, `executor_not_found`, `cluster_unknown`, `config_invalid` |
| 2 | `cluster`, `network` | remote/cluster issue | `ssh_unreachable`, `scheduler_throttled`, `remote_command_failed`, `combiner_failed`, `cluster_timeout`, `outputs_missing` |
| 3 | `internal` | bug in framework or corrupt state | `journal_corrupt`, `internal` |

`preflight` returns 2 when any check fails (it is a `cluster`-class diagnostic, even though the envelope is `ok=true`).

Per-primitive exit-code overrides live in each primitive's frontmatter (`exit_codes:` field).

## Subcommands

Every subcommand is documented as a primitive in [`docs/primitives/`](primitives/). The catalog table at `docs/primitives/README.md` lists them with one-line summaries; click through for the full contract.

CLI ↔ primitive mapping:

| CLI | Primitive |
|---|---|
| `hpc-mapreduce capabilities` | [capabilities](primitives/capabilities.md) |
| `hpc-mapreduce preflight` | [check-preflight](primitives/check-preflight.md) |
| `hpc-mapreduce clusters list` | [clusters-list](primitives/clusters-list.md) |
| `hpc-mapreduce clusters describe <name>` | [clusters-describe](primitives/clusters-describe.md) |
| `hpc-mapreduce discover` | [discover-executors](primitives/discover-executors.md) |
| `hpc-mapreduce list-in-flight` | [list-in-flight](primitives/list-in-flight.md) |
| `hpc-mapreduce campaign status` | [campaign-status](primitives/campaign-status.md) |
| `hpc-mapreduce campaign list` | [campaign-list](primitives/campaign-list.md) |
| `hpc-mapreduce status --run-id <id>` | [poll-run-status](primitives/poll-run-status.md) |
| `hpc-mapreduce submit --spec <path>` | [submit-spec](primitives/submit-spec.md) |
| `hpc-mapreduce aggregate --run-id <id> --wave <N>` | [combine-wave](primitives/combine-wave.md) |
| `hpc-mapreduce resubmit --run-id <id> --spec <path>` | [resubmit-failed](primitives/resubmit-failed.md) |
| `hpc-mapreduce reconcile --run-id <id>` | [reconcile-journal](primitives/reconcile-journal.md) |
| `hpc-mapreduce build-executor --name <stem>` | [build-executor](primitives/build-executor.md) |
| `hpc-mapreduce inspect-cluster --cluster <name>` | [inspect-cluster](primitives/inspect-cluster.md) |
| `hpc-mapreduce runtime-prior --profile <p> --cluster <c>` | [read-runtime-prior](primitives/read-runtime-prior.md) |
| `hpc-mapreduce plan-submit --profile <p> --cluster <c>` | [score-submit-plan](primitives/score-submit-plan.md) |

This table is hand-maintained until `scripts/build_primitive_index.py` learns to render it; the catalog at `docs/primitives/README.md` is auto-generated and is the canonical view.
