# Operations

Auto-generated from `hpc-mapreduce capabilities`. Run `uv run python scripts/build_operations_index.py` after editing any primitive frontmatter; the script subprocess-calls the CLI and parses the same JSON envelope an external agent would get at runtime, so this page is provably in sync with runtime introspection.

**29 operations total**: 26 primitive atoms + 3 workflow atoms.

## How to read this page

Every operation in `claude-hpc` is a CLI atom or a Python-only primitive that emits the same `{ok, data, error_code}` envelope shape (see `docs/cli-spec.md`). Workflow atoms compose primitive atoms but are externally indistinguishable from primitives — that's the Composite property that makes higher-level workflows like campaigns work.

**Composability rule**: any operation can invoke any other operation by shelling to its CLI form (or importing its Python form). Higher-level workflows (e.g. `submit-flow → monitor-flow → aggregate-flow` chained by a campaign loop) are just operations that invoke other operations.

**Discoverability**: `hpc-mapreduce capabilities` returns this same catalog at runtime in `data.operations`. Agents that don't have access to this page can introspect the framework via that subprocess call.

## `query` (18)

Read-only, no side effects. Freely composable; cacheable.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`best-submit-window`](primitives/best-submit-window.md) | ✓ | _none_ | `hpc-mapreduce best-submit-window --profile <p> --cluster <c> [--within-hours N] [--top-k K]` | `claude_hpc.agent_cli.cmd_best_submit_window` | `claude_hpc/schemas/best_submit_window.input.json` | `claude_hpc/schemas/best_submit_window.output.json` |
| [`campaign-health`](primitives/campaign-health.md) | ✓ | _none_ | `hpc-mapreduce campaign-health [--campaign-id <id>] [--since-iso <ts>]` | `claude_hpc.orchestrator.campaign_health.campaign_health` | `claude_hpc/schemas/campaign_health.input.json` | `claude_hpc/schemas/campaign_health.output.json` |
| [`campaign-list`](primitives/campaign-list.md) | ✓ | _none_ | `hpc-mapreduce campaign list [--experiment-dir <dir>]` | `claude_hpc.atoms.campaign_list.campaign_list` | — | — |
| [`campaign-status`](primitives/campaign-status.md) | ✓ | _none_ | `hpc-mapreduce campaign status --campaign-id <id> [--experiment-dir <dir>]` | `claude_hpc.atoms.campaign_status.campaign_status` | — | — |
| [`capabilities`](primitives/capabilities.md) | ✓ | _none_ | `hpc-mapreduce capabilities` | `claude_hpc.atoms.capabilities.capabilities` | — | `claude_hpc/schemas/capabilities.output.json` |
| [`clusters-describe`](primitives/clusters-describe.md) | ✓ | _none_ | `hpc-mapreduce clusters describe <name>` | `claude_hpc.atoms.clusters.describe_cluster` | — | `claude_hpc/schemas/clusters_describe.output.json` |
| [`clusters-list`](primitives/clusters-list.md) | ✓ | _none_ | `hpc-mapreduce clusters list` | `claude_hpc.atoms.clusters.list_clusters` | — | `claude_hpc/schemas/clusters_list.output.json` |
| [`discover-executors`](primitives/discover-executors.md) | ✓ | _none_ | `hpc-mapreduce discover --experiment-dir <path>` | `claude_hpc.orchestrator.discover.discover_executors` | — | `claude_hpc/schemas/discover.output.json` |
| [`failures`](primitives/failures.md) | ✓ | ssh | `hpc-mapreduce failures --run-id <id> [--lines <n>]` | `claude_hpc.atoms.failures.fetch_failures` | — | `claude_hpc/schemas/failures.output.json` |
| [`house-edge`](primitives/house-edge.md) | ✓ | _none_ | `hpc-mapreduce house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]` | `claude_hpc.atoms.house_edge.house_edge` | — | — |
| [`inspect-cluster`](primitives/inspect-cluster.md) | ✓ | ssh | `hpc-mapreduce inspect-cluster --cluster <name> [...]` | `claude_hpc.infra.inspect.inspect_cluster` | — | `claude_hpc/schemas/inspect_cluster.output.json` |
| [`list-in-flight`](primitives/list-in-flight.md) | ✓ | _none_ | `hpc-mapreduce list-in-flight --experiment-dir <path>` | `claude_hpc.atoms.list_in_flight.list_in_flight` | — | `claude_hpc/schemas/list_in_flight.output.json` |
| [`logs`](primitives/logs.md) | ✓ | ssh | `hpc-mapreduce logs --run-id <id> (--task-id <ids> | --all-failed) [--lines <n>]` | `claude_hpc.atoms.logs.fetch_logs` | — | — |
| [`poll-run-status`](primitives/poll-run-status.md) | ✓ | ssh; writes-journal | `hpc-mapreduce status --run-id <id> [--experiment-dir <dir>]` | `claude_hpc.orchestrator.runner.record_status` | — | `claude_hpc/schemas/status.output.json` |
| [`predict-queue-wait`](primitives/predict-queue-wait.md) | ✓ | _none_ | `hpc-mapreduce predict-queue-wait --profile <p> --cluster <c> [--backend auto|des|diurnal_ma] [--n-replications N] [--at-iso <iso>] [--seed N]` | `claude_hpc.agent_cli.cmd_predict_queue_wait` | `claude_hpc/schemas/predict_queue_wait.input.json` | `claude_hpc/schemas/predict_queue_wait.output.json` |
| [`read-runtime-prior`](primitives/read-runtime-prior.md) | ✓ | _none_ | `hpc-mapreduce runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]` | `claude_hpc.agent_cli.cmd_runtime_prior` | — | `claude_hpc/schemas/runtime_prior.output.json` |
| [`score-submit-plan`](primitives/score-submit-plan.md) | ✓ | ssh | `hpc-mapreduce plan-submit --profile <name> --cluster <name> [...]` | `claude_hpc.agent_cli.cmd_plan_submit` | — | `claude_hpc/schemas/plan_submit.output.json` |
| [`walltime-drift`](primitives/walltime-drift.md) | ✓ | _none_ | `hpc-mapreduce walltime-drift --profile <name> --cluster <name> [--cmd-sha <sha>] [--base-safety-mult <f>]` | `claude_hpc.atoms.walltime_drift.walltime_drift` | — | — |

## `validate` (2)

Read + binary health check. Same composability as `query`.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`check-preflight`](primitives/check-preflight.md) | ✓ | _none_ | `hpc-mapreduce preflight [--cluster <name>]` | `claude_hpc.atoms.preflight.check_preflight` | — | `claude_hpc/schemas/preflight.output.json` |
| [`validate`](primitives/validate.md) | ✓ | ssh | `hpc-mapreduce validate --profile <p> --cluster <c> --walltime-sec <s> --mem-mb <m> --cpus <c>` | `claude_hpc.orchestrator.validate.validate_submission` | `claude_hpc/schemas/validate.input.json` | `claude_hpc/schemas/validate.output.json` |

## `mutate` (4)

Writes to journal / sidecar. Need flock + idempotency-key consideration.

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`combine-wave`](primitives/combine-wave.md) | ✓ | runs; ssh; writes-cluster; writes-journal | `hpc-mapreduce aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]` | `claude_hpc.orchestrator.runner.combine_wave` | — | `claude_hpc/schemas/combine_wave.output.json` |
| [`mark-run-terminal`](primitives/mark-run-terminal.md) | ✓ | writes-journal | `(none — Python-only primitive)` | `claude_hpc.orchestrator.runner.mark_terminal` | — | — |
| [`reconcile-journal`](primitives/reconcile-journal.md) | ✓ | ssh; writes-journal | `hpc-mapreduce reconcile --run-id <id> --scheduler {sge|slurm} [--experiment-dir <dir>]` | `claude_hpc.agent_cli.cmd_reconcile` | — | `claude_hpc/schemas/reconcile.output.json` |
| [`resubmit-failed`](primitives/resubmit-failed.md) | ✓ | scheduler-submit; writes-journal | `hpc-mapreduce resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]` | `claude_hpc.orchestrator.runner.resubmit_failed` | `claude_hpc/schemas/resubmit.input.json` | — |

## `submit` (1)

Records a new submission (sidecar write + journal entry).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`submit-spec`](primitives/submit-spec.md) | ✓ | scheduler-submit; writes-journal | `hpc-mapreduce submit --spec <path> [--experiment-dir <dir>] [--dry-run] [--from-meta]` | `claude_hpc.orchestrator.runner.submit_and_record` | `claude_hpc/schemas/submit.input.json` | `claude_hpc/schemas/submit.output.json` |

## `scaffold` (1)

Creates new files (e.g. starter executor templates).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`build-executor`](primitives/build-executor.md) | ✗ | writes-file | `hpc-mapreduce build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]` | `claude_hpc.agent_cli.cmd_build_executor` | — | `claude_hpc/schemas/build_executor.output.json` |

## `workflow` (3)

End-to-end pipelines composing other primitives. Same envelope shape as primitives — indistinguishable to higher-level callers (the Composite property).

| Operation | Idempotent | Side effects | CLI | Python | Input schema | Output schema |
|---|---|---|---|---|---|---|
| [`aggregate-flow`](primitives/aggregate-flow.md) | ✓ | rsync; ssh; writes-journal | `hpc-mapreduce aggregate-flow --spec <path>` | `claude_hpc.orchestrator.aggregate_flow.aggregate_flow` | `claude_hpc/schemas/aggregate_flow.input.json` | `claude_hpc/schemas/aggregate_flow.output.json` |
| [`monitor-flow`](primitives/monitor-flow.md) | ✓ | ssh; writes-journal | `hpc-mapreduce monitor-flow --spec <path>` | `claude_hpc.orchestrator.monitor_flow.monitor_flow` | `claude_hpc/schemas/monitor_flow.input.json` | `claude_hpc/schemas/monitor_flow.output.json` |
| [`submit-flow`](primitives/submit-flow.md) | ✓ | rsync; scheduler-submit; writes-journal | `hpc-mapreduce submit-flow --spec <path>` | `claude_hpc.orchestrator.submit_flow.submit_flow` | `claude_hpc/schemas/submit_flow.input.json` | `claude_hpc/schemas/submit_flow.output.json` |

