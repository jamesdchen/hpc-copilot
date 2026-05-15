# `hpc-agent` CLI Specification

Cross-cutting contract for the shell CLI shipped at `claude_hpc/agent_cli.py` (entry point `hpc-agent`). Per-subcommand contracts live in **[`docs/primitives/`](primitives/)** — one file per operation, with full input/output/error/idempotency contracts in YAML frontmatter. This file documents only what's shared across every subcommand: stdout envelope shape, exit-code mapping, and the schemas list.

The slash-command surface in `slash_commands/commands/` is documented elsewhere; both surfaces compose from the primitive layer.

## Schemas (machine-readable contracts)

The JSON Schemas under `claude_hpc/schemas/` are the **wire contract** — agents
constructing or validating envelopes validate against those files, not this
markdown. Internally they are **regenerated** by
`scripts/build_schemas.py` from Pydantic models under
`src/claude_hpc/_schema_models/`; the Python models are the *authoring* SoT
and the JSON files are a build artifact, the same posture
`docs/generated/operations.md` and `docs/primitives/<name>.md` frontmatter use
relative to the `@primitive` registry.

External consumers (agent harnesses, the in-process
`validate_output` boundary check) read the JSON. Framework
contributors edit the Pydantic. Pre-commit's `build-schemas`
`--check` gate fails CI when the two diverge.

73 schemas cover the full agent-facing surface:

- `envelope.json` — universal stdout envelope (success / error variants
  via discriminated union on `ok`).
- One `<primitive>.input.json` per primitive that takes a `--spec` payload.
- One `<primitive>.output.json` per primitive whose `data` block has a
  declared shape.
- Two persisted-data shapes used at runtime: `axes.json`
  (`<experiment>/.hpc/axes.yaml`) and `campaign_manifest.json`
  (`<campaign_dir>/manifest.json`).
- `stages.input.json` — output shape of `.hpc/stages.py::stages()`.

Cross-file `$ref` is rare post-Pydantic-migration; most schemas
inline shared constraints (run_id pattern, scheduler enum, lifecycle
states, error codes) from `_schema_models/_shared.py`. When a
shared constraint changes, edit the Python alias and regenerate —
every consumer schema updates in lock-step.

## Conventions

- Stdout is exactly one line: a single JSON envelope. No banners, no logs.
- Stderr carries JSON-per-line log records (debug for humans).
- Every subcommand accepts `--experiment-dir` (defaults to CWD) unless the operation is global (e.g. `clusters list`, `capabilities`).
- Subcommands with non-trivial inputs accept `--spec path/to/spec.json`.
- Idempotent subcommands set `"idempotent": true` on the success envelope.
- `hpc-agent --version` prints the package version and exits 0.

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
  "error_code": "<one of 15>",
  "message": "<human-readable>",
  "category": "user|cluster|network|internal",
  "retry_safe": <bool>,
  "remediation": "<optional>"
}
```

Source of truth: `claude_hpc/schemas/envelope.json` and the `HpcError` hierarchy in `claude_hpc/errors.py`.

## Exit code → error_code mapping

Wired in `claude_hpc/agent_cli.py` (`_EXIT_CODE_BY_CATEGORY`).

| Exit | Category | Meaning | error_codes that map here |
|---|---|---|---|
| 0 | — | success | (no error envelope) |
| 1 | `user` | caller-fixable | `spec_invalid`, `executor_not_found`, `cluster_unknown`, `config_invalid` |
| 2 | `cluster`, `network` | remote/cluster issue | `ssh_unreachable`, `scheduler_throttled`, `remote_command_failed`, `combiner_failed`, `cluster_timeout`, `outputs_missing`, `cluster_partially_degraded`, `preempted` |
| 3 | `internal` | bug in framework or corrupt state | `journal_corrupt`, `internal`, `schema_incompat` |

`preflight` returns 2 when any check fails (it is a `cluster`-class diagnostic, even though the envelope is `ok=true`).

Per-primitive exit-code overrides live in each primitive's frontmatter (`exit_codes:` field).

## Subcommands

Every subcommand is documented as a primitive in [`docs/primitives/`](primitives/). The catalog table at `docs/primitives/README.md` lists them with one-line summaries; click through for the full contract.

CLI ↔ primitive mapping:

| CLI | Primitive |
|---|---|
| `hpc-agent capabilities` | [capabilities](primitives/capabilities.md) |
| `hpc-agent preflight` | [check-preflight](primitives/check-preflight.md) |
| `hpc-agent clusters list` | [clusters-list](primitives/clusters-list.md) |
| `hpc-agent clusters describe <name>` | [clusters-describe](primitives/clusters-describe.md) |
| `hpc-agent discover` | [discover-executors](primitives/discover-executors.md) |
| `hpc-agent list-in-flight` | [list-in-flight](primitives/list-in-flight.md) |
| `hpc-agent campaign status` | [campaign-status](primitives/campaign-status.md) |
| `hpc-agent campaign list` | [campaign-list](primitives/campaign-list.md) |
| `hpc-agent status --run-id <id>` | [poll-run-status](primitives/poll-run-status.md) |
| `hpc-agent submit --spec <path>` | [submit-spec](primitives/submit-spec.md) |
| `hpc-agent aggregate --run-id <id> --wave <N>` | [combine-wave](primitives/combine-wave.md) |
| `hpc-agent resubmit --run-id <id> --spec <path>` | [resubmit-failed](primitives/resubmit-failed.md) |
| `hpc-agent reconcile --run-id <id>` | [reconcile-journal](primitives/reconcile-journal.md) |
| `hpc-agent build-executor --name <stem>` | [build-executor](primitives/build-executor.md) |
| `hpc-agent inspect-cluster --cluster <name>` | [inspect-cluster](primitives/inspect-cluster.md) |
| `hpc-agent runtime-prior --profile <p> --cluster <c>` | [read-runtime-prior](primitives/read-runtime-prior.md) |
| `hpc-agent plan-submit --profile <p> --cluster <c>` | [score-submit-plan](primitives/score-submit-plan.md) |

This table is hand-maintained until `scripts/build_primitive_index.py` learns to render it; the catalog at `docs/primitives/README.md` is auto-generated and is the canonical view.
