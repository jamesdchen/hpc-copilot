# Operations

Auto-generated from `hpc-mapreduce capabilities`. Run `uv run python scripts/build_operations_index.py` after editing any primitive frontmatter; the script subprocess-calls the CLI and parses the same JSON envelope an external agent would get at runtime, so this page is provably in sync with runtime introspection.

**22 operations total**: 19 primitive atoms + 3 workflow atoms.

## How to read this page

Every operation in `claude-hpc` is a CLI atom or a Python-only primitive that emits the same `{ok, data, error_code}` envelope shape (see `docs/cli-spec.md`). Workflow atoms compose primitive atoms but are externally indistinguishable from primitives — that's the Composite property that makes higher-level workflows like campaigns work.

**Composability rule**: any operation can invoke any other operation by shelling to its CLI form (or importing its Python form). Higher-level workflows (e.g. `submit-flow → monitor-flow → aggregate-flow` chained by a campaign loop) are just operations that invoke other operations.

**Discoverability**: `hpc-mapreduce capabilities` returns this same catalog at runtime in `data.operations`. Agents that don't have access to this page can introspect the framework via that subprocess call.

## `query` (11)

Read-only, no side effects. Freely composable; cacheable.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`campaign-list`](primitives/campaign-list.md) | ✓ | _none_ | `hpc-mapreduce campaign list [--experiment-dir <dir>]` | `hpc_mapreduce.reduce.history.find_sidecars_by_campaign` | — | — |
| [`campaign-status`](primitives/campaign-status.md) | ✓ | _none_ | `hpc-mapreduce campaign status --campaign-id <id> [--experiment-dir <dir>]` | `hpc_mapreduce.reduce.history.prior` | — | — |
| [`capabilities`](primitives/capabilities.md) | ✓ | _none_ | `hpc-mapreduce capabilities` | `hpc_mapreduce.agent_cli.cmd_capabilities` | — | `hpc_mapreduce/schemas/capabilities.output.json` |
| [`clusters-describe`](primitives/clusters-describe.md) | ✓ | _none_ | `hpc-mapreduce clusters describe <name>` | `hpc_mapreduce.agent_cli.cmd_clusters_describe` | — | `hpc_mapreduce/schemas/clusters_describe.output.json` |
| [`clusters-list`](primitives/clusters-list.md) | ✓ | _none_ | `hpc-mapreduce clusters list` | `hpc_mapreduce.agent_cli.cmd_clusters_list` | — | `hpc_mapreduce/schemas/clusters_list.output.json` |
| [`discover-executors`](primitives/discover-executors.md) | ✓ | _none_ | `hpc-mapreduce discover --experiment-dir <path>` | `hpc_mapreduce.job.discover.discover_executors` | — | `hpc_mapreduce/schemas/discover.output.json` |
| [`inspect-cluster`](primitives/inspect-cluster.md) | ✓ | cache, ssh | `hpc-mapreduce inspect-cluster --cluster <name> [...]` | `hpc_mapreduce.infra.inspect.inspect_cluster` | — | `hpc_mapreduce/schemas/inspect_cluster.output.json` |
| [`list-in-flight`](primitives/list-in-flight.md) | ✓ | _none_ | `hpc-mapreduce list-in-flight --experiment-dir <path>` | `slash_commands.session.find_in_flight_runs` | — | `hpc_mapreduce/schemas/list_in_flight.output.json` |
| [`poll-run-status`](primitives/poll-run-status.md) | ✓ | ssh, writes | `hpc-mapreduce status --run-id <id> [--experiment-dir <dir>]` | `slash_commands.runner.record_status` | — | `hpc_mapreduce/schemas/status.output.json` |
| [`read-runtime-prior`](primitives/read-runtime-prior.md) | ✓ | _none_ | `hpc-mapreduce runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]` | `hpc_mapreduce.job.runtime_prior.summarize` | — | `hpc_mapreduce/schemas/runtime_prior.output.json` |
| [`score-submit-plan`](primitives/score-submit-plan.md) | ✓ | ssh | `hpc-mapreduce plan-submit --profile <name> --cluster <name> [...]` | `hpc_mapreduce.job.planner.plan_submit` | — | `hpc_mapreduce/schemas/plan_submit.output.json` |

## `validate` (1)

Read + binary health check. Same composability as `query`.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`check-preflight`](primitives/check-preflight.md) | ✓ | _none_ | `hpc-mapreduce preflight [--cluster <name>]` | `hpc_mapreduce.preflight.run` | — | `hpc_mapreduce/schemas/preflight.output.json` |

## `mutate` (5)

Writes to journal / sidecar / blacklist. Need flock + idempotency-key consideration.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`combine-wave`](primitives/combine-wave.md) | ✓ | mutates, runs, ssh, writes | `hpc-mapreduce aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]` | `slash_commands.runner.combine_wave` | — | `hpc_mapreduce/schemas/combine_wave.output.json` |
| [`mark-run-terminal`](primitives/mark-run-terminal.md) | ✓ | mutates | `(none — Python-only primitive)` | `slash_commands.runner.mark_terminal` | — | — |
| [`reconcile-journal`](primitives/reconcile-journal.md) | ✓ | mutates, ssh | `hpc-mapreduce reconcile --run-id <id> --scheduler {sge|slurm} [--experiment-dir <dir>]` | `slash_commands.runner.reconcile` | — | `hpc_mapreduce/schemas/reconcile.output.json` |
| [`record-segv-blacklist`](primitives/record-segv-blacklist.md) | ✓ | mutates | `(none — Python-only primitive)` | `hpc_mapreduce.job.blacklist.record_segv` | — | — |
| [`resubmit-failed`](primitives/resubmit-failed.md) | ✓ | mutates | `hpc-mapreduce resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]` | `slash_commands.runner.resubmit_failed` | `hpc_mapreduce/schemas/resubmit.input.json` | — |

## `submit` (1)

Records a new submission (sidecar write + journal entry).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`submit-spec`](primitives/submit-spec.md) | ✓ | rsyncs, submits, writes | `hpc-mapreduce submit --spec <path> [--experiment-dir <dir>] [--dry-run] [--from-meta]` | `slash_commands.runner.submit_and_record` | `hpc_mapreduce/schemas/submit.input.json` | `hpc_mapreduce/schemas/submit.output.json` |

## `scaffold` (1)

Creates new files (e.g. starter executor templates).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`build-executor`](primitives/build-executor.md) | ✗ | writes | `hpc-mapreduce build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]` | `hpc_mapreduce.agent_cli.cmd_build_executor` | — | `hpc_mapreduce/schemas/build_executor.output.json` |

## `workflow` (3)

End-to-end pipelines composing other primitives. Same envelope shape as primitives — indistinguishable to higher-level callers (the Composite property).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`aggregate-flow`](primitives/aggregate-flow.md) | ✓ | mutates, writes | `hpc-mapreduce aggregate-flow --spec <path>` | `hpc_mapreduce.job.aggregate_flow.aggregate_flow` | `hpc_mapreduce/schemas/aggregate_flow.input.json` | `hpc_mapreduce/schemas/aggregate_flow.output.json` |
| [`monitor-flow`](primitives/monitor-flow.md) | ✓ | mutates, writes | `hpc-mapreduce monitor-flow --spec <path>` | `hpc_mapreduce.job.monitor_flow.monitor_flow` | `hpc_mapreduce/schemas/monitor_flow.input.json` | `hpc_mapreduce/schemas/monitor_flow.output.json` |
| [`submit-flow`](primitives/submit-flow.md) | ✓ | rsyncs, submits, writes | `hpc-mapreduce submit-flow --spec <path>` | `hpc_mapreduce.job.submit_flow.submit_flow` | `hpc_mapreduce/schemas/submit_flow.input.json` | `hpc_mapreduce/schemas/submit_flow.output.json` |

