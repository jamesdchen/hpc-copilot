# Operations

Auto-generated from `hpc-mapreduce capabilities`. Run `uv run python scripts/build_operations_index.py` after editing any primitive frontmatter; the script subprocess-calls the CLI and parses the same JSON envelope an external agent would get at runtime, so this page is provably in sync with runtime introspection.

**27 operations total**: 24 primitive atoms + 3 workflow atoms.

## How to read this page

Every operation in `claude-hpc` is a CLI atom or a Python-only primitive that emits the same `{ok, data, error_code}` envelope shape (see `docs/cli-spec.md`). Workflow atoms compose primitive atoms but are externally indistinguishable from primitives — that's the Composite property that makes higher-level workflows like campaigns work.

**Composability rule**: any operation can invoke any other operation by shelling to its CLI form (or importing its Python form). Higher-level workflows (e.g. `submit-flow → monitor-flow → aggregate-flow` chained by a campaign loop) are just operations that invoke other operations.

**Discoverability**: `hpc-mapreduce capabilities` returns this same catalog at runtime in `data.operations`. Agents that don't have access to this page can introspect the framework via that subprocess call.

## `query` (16)

Read-only, no side effects. Freely composable; cacheable.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`campaign-health`](primitives/campaign-health.md) | ✓ | _none_ | `hpc-mapreduce campaign-health [--campaign-id <id>] [--since-iso <ts>]` | `claude_hpc.orchestrator.campaign_health.campaign_health` | `hpc_mapreduce/schemas/campaign_health.input.json` | `hpc_mapreduce/schemas/campaign_health.output.json` |
| [`campaign-list`](primitives/campaign-list.md) | ✓ | _none_ | `hpc-mapreduce campaign list [--experiment-dir <dir>]` | `hpc_mapreduce.agent_cli.cmd_campaign_list` | — | — |
| [`campaign-status`](primitives/campaign-status.md) | ✓ | _none_ | `hpc-mapreduce campaign status --campaign-id <id> [--experiment-dir <dir>]` | `hpc_mapreduce.agent_cli.cmd_campaign_status` | — | — |
| [`capabilities`](primitives/capabilities.md) | ✓ | _none_ | `hpc-mapreduce capabilities` | `hpc_mapreduce.agent_cli.cmd_capabilities` | — | `hpc_mapreduce/schemas/capabilities.output.json` |
| [`clusters-describe`](primitives/clusters-describe.md) | ✓ | _none_ | `hpc-mapreduce clusters describe <name>` | `hpc_mapreduce.agent_cli.cmd_clusters_describe` | — | `hpc_mapreduce/schemas/clusters_describe.output.json` |
| [`clusters-list`](primitives/clusters-list.md) | ✓ | _none_ | `hpc-mapreduce clusters list` | `hpc_mapreduce.agent_cli.cmd_clusters_list` | — | `hpc_mapreduce/schemas/clusters_list.output.json` |
| [`discover-executors`](primitives/discover-executors.md) | ✓ | _none_ | `hpc-mapreduce discover --experiment-dir <path>` | `claude_hpc.orchestrator.discover.discover_executors` | — | `hpc_mapreduce/schemas/discover.output.json` |
| [`failures`](primitives/failures.md) | ✓ | ssh | `hpc-mapreduce failures --run-id <id> [--lines <n>]` | `hpc_mapreduce.agent_cli.cmd_failures` | — | — |
| [`house-edge`](primitives/house-edge.md) | ✓ | _none_ | `hpc-mapreduce house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]` | `hpc_mapreduce.agent_cli.cmd_house_edge` | — | — |
| [`inspect-cluster`](primitives/inspect-cluster.md) | ✓ | ssh | `hpc-mapreduce inspect-cluster --cluster <name> [...]` | `hpc_mapreduce.infra.inspect.inspect_cluster` | — | `hpc_mapreduce/schemas/inspect_cluster.output.json` |
| [`list-in-flight`](primitives/list-in-flight.md) | ✓ | _none_ | `hpc-mapreduce list-in-flight --experiment-dir <path>` | `hpc_mapreduce.agent_cli.cmd_list_in_flight` | — | `hpc_mapreduce/schemas/list_in_flight.output.json` |
| [`logs`](primitives/logs.md) | ✓ | ssh | `hpc-mapreduce logs --run-id <id> (--task-id <ids> | --all-failed) [--lines <n>]` | `hpc_mapreduce.agent_cli.cmd_logs` | — | — |
| [`poll-run-status`](primitives/poll-run-status.md) | ✓ | ssh; writes-journal | `hpc-mapreduce status --run-id <id> [--experiment-dir <dir>]` | `slash_commands.runner.record_status` | — | `hpc_mapreduce/schemas/status.output.json` |
| [`read-runtime-prior`](primitives/read-runtime-prior.md) | ✓ | _none_ | `hpc-mapreduce runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]` | `hpc_mapreduce.agent_cli.cmd_runtime_prior` | — | `hpc_mapreduce/schemas/runtime_prior.output.json` |
| [`score-submit-plan`](primitives/score-submit-plan.md) | ✓ | ssh | `hpc-mapreduce plan-submit --profile <name> --cluster <name> [...]` | `hpc_mapreduce.agent_cli.cmd_plan_submit` | — | `hpc_mapreduce/schemas/plan_submit.output.json` |
| [`walltime-drift`](primitives/walltime-drift.md) | ✓ | _none_ | `hpc-mapreduce walltime-drift --profile <name> --cluster <name> [--cmd-sha <sha>] [--base-safety-mult <f>]` | `hpc_mapreduce.agent_cli.cmd_walltime_drift` | — | — |

## `validate` (2)

Read + binary health check. Same composability as `query`.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`check-preflight`](primitives/check-preflight.md) | ✓ | _none_ | `hpc-mapreduce preflight [--cluster <name>]` | `hpc_mapreduce.agent_cli.cmd_preflight` | — | `hpc_mapreduce/schemas/preflight.output.json` |
| [`validate`](primitives/validate.md) | ✓ | ssh | `hpc-mapreduce validate --profile <p> --cluster <c> --walltime-sec <s> --mem-mb <m> --cpus <c>` | `claude_hpc.orchestrator.validate.validate_submission` | `hpc_mapreduce/schemas/validate.input.json` | `hpc_mapreduce/schemas/validate.output.json` |

## `mutate` (4)

Writes to journal / sidecar. Need flock + idempotency-key consideration.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`combine-wave`](primitives/combine-wave.md) | ✓ | runs; ssh; writes-cluster; writes-journal | `hpc-mapreduce aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]` | `slash_commands.runner.combine_wave` | — | `hpc_mapreduce/schemas/combine_wave.output.json` |
| [`mark-run-terminal`](primitives/mark-run-terminal.md) | ✓ | writes-journal | `(none — Python-only primitive)` | `slash_commands.runner.mark_terminal` | — | — |
| [`reconcile-journal`](primitives/reconcile-journal.md) | ✓ | ssh; writes-journal | `hpc-mapreduce reconcile --run-id <id> --scheduler {sge|slurm} [--experiment-dir <dir>]` | `hpc_mapreduce.agent_cli.cmd_reconcile` | — | `hpc_mapreduce/schemas/reconcile.output.json` |
| [`resubmit-failed`](primitives/resubmit-failed.md) | ✓ | scheduler-submit; writes-journal | `hpc-mapreduce resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]` | `slash_commands.runner.resubmit_failed` | `hpc_mapreduce/schemas/resubmit.input.json` | — |

## `submit` (1)

Records a new submission (sidecar write + journal entry).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`submit-spec`](primitives/submit-spec.md) | ✓ | scheduler-submit; writes-journal | `hpc-mapreduce submit --spec <path> [--experiment-dir <dir>] [--dry-run] [--from-meta]` | `slash_commands.runner.submit_and_record` | `hpc_mapreduce/schemas/submit.input.json` | `hpc_mapreduce/schemas/submit.output.json` |

## `scaffold` (1)

Creates new files (e.g. starter executor templates).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`build-executor`](primitives/build-executor.md) | ✗ | writes-file | `hpc-mapreduce build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]` | `hpc_mapreduce.agent_cli.cmd_campaign_health` | — | `hpc_mapreduce/schemas/build_executor.output.json` |

## `workflow` (3)

End-to-end pipelines composing other primitives. Same envelope shape as primitives — indistinguishable to higher-level callers (the Composite property).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`aggregate-flow`](primitives/aggregate-flow.md) | ✓ | rsync; ssh; writes-journal | `hpc-mapreduce aggregate-flow --spec <path>` | `claude_hpc.orchestrator.aggregate_flow.aggregate_flow` | `hpc_mapreduce/schemas/aggregate_flow.input.json` | `hpc_mapreduce/schemas/aggregate_flow.output.json` |
| [`monitor-flow`](primitives/monitor-flow.md) | ✓ | ssh; writes-journal | `hpc-mapreduce monitor-flow --spec <path>` | `claude_hpc.orchestrator.monitor_flow.monitor_flow` | `hpc_mapreduce/schemas/monitor_flow.input.json` | `hpc_mapreduce/schemas/monitor_flow.output.json` |
| [`submit-flow`](primitives/submit-flow.md) | ✓ | rsync; scheduler-submit; writes-journal | `hpc-mapreduce submit-flow --spec <path>` | `claude_hpc.orchestrator.submit_flow.submit_flow` | `hpc_mapreduce/schemas/submit_flow.input.json` | `hpc_mapreduce/schemas/submit_flow.output.json` |

