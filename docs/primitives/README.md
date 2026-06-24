# Primitives

A **primitive** is the smallest agent-and-human-shareable operation: one verb, one input contract, one output contract, one set of error codes, one declared side-effect class. Both `slash_commands/commands/*.md` (human-facing, interactive) and `slash_commands/skills/hpc-*/SKILL.md` (agent-facing, terse) **compose** from this catalog instead of describing the same operations from scratch.

Why this layer exists:

- **Single source of truth.** "How to submit a spec" lives in exactly one file. Today the same flow is described in `slash_commands/commands/submit-hpc.md` (770 lines, interactive) and `slash_commands/skills/hpc-submit/SKILL.md` (88 lines, terse) — and the two drift independently.
- **Composability.** A skill or slash command body becomes a short pipeline of primitive calls plus the surface-specific glue (interactive prompts for slash commands; defaults + envelope-parsing for skills). When a primitive's contract changes, only the primitive doc moves; consumers re-validate against the new contract.
- **Discoverability.** One catalog the agent can scan to find "what produces a sidecar" / "what's idempotent" / "what's safe to retry" without grepping prose.

## Primitive contract (frontmatter)

Every primitive file ships YAML frontmatter with **behavioral metadata only**. Field-level contracts (input/output shapes) live in JSON Schemas under `hpc_agent/schemas/`; the primitive's `backed_by` field points at the schema-validated CLI/Python entry point.

```yaml
---
name: submit-spec                         # kebab-case, unique
verb: submit                              # one of: query, validate, mutate, submit, scaffold, workflow
side_effects:                             # what changes outside the agent's view
  - writes: .hpc/runs/<run_id>.json
  - writes: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json
idempotent: true                          # safe to replay with same inputs?
idempotency_key: spec.run_id              # what makes a replay equivalent
error_codes:                              # what callers should handle
  - code: spec_invalid
    category: user
    retry_safe: false
  - code: ssh_unreachable
    category: network
    retry_safe: true
backed_by:                                # implementation + field-level contract source
  cli: hpc-agent submit --spec <path>
  python: hpc_agent.ops.submit.runner.submit_and_record
---
```

The body covers Purpose (one paragraph), Compose with (predecessor/successor primitives), and Notes (caveats, gotchas, idempotency reasoning). The body NEVER describes interactive flows — that lives in the slash command. It NEVER restates field-level contracts — those live in the JSON schema.

**Why no `inputs:` / `outputs:` blocks?** Earlier iterations of this catalog included field-level contracts in frontmatter, then a validator script cross-checked them against schemas. Both layers are now obsolete: schemas are the single source of truth for field-level shapes, frontmatter is for behavioral metadata, and the validator was deleted (nothing to validate against). Keeping both was just maintaining duplicate contracts.

## Two body templates

Primitives carry one of two body templates depending on `agent_facing`. Both ship the same frontmatter — only the section structure changes — and `scripts/lint_primitive_doc_templates.py` gates the partition.

- **`agent_facing=True`** — read by LLMs via `render_llms_full` and clicked through from slash commands / skills. Outward-facing template:
  ```
  # <name>
  <one-paragraph what + why>
  ## Inputs
  ## Outputs
  ## Errors
  ## Idempotency
  ## Notes
  ```
- **`agent_facing=False`** — framework internals composed inside workflows. Audience is the next contributor, not an LLM (`render_llms_full` skips these bodies, ships only the catalog row). Contributor-facing template:
  ```
  # <name>
  > **Internal primitive.** [where it's composed from]
  <one-paragraph what + why>
  ## Composers
  ## Invariants
  ## Coupling
  ## Failure modes
  ```

The lint script counts headers in each tier's vocabulary; a doc whose body leans toward the wrong template for its tier fails CI. Stub bodies (`_Documentation pending._` placeholders auto-scaffolded when the registry has a primitive without a doc) are tolerated.

See [`docs/internals/adding-a-primitive.md`](../internals/adding-a-primitive.md) for the full add-a-primitive recipe.

## Catalog

Auto-generated from frontmatter, grouped by `verb` — run `uv run python scripts/build_primitive_index.py` after adding or editing a primitive. CI gates on `--check` so the table can never drift from the source.

The verb partitions primitives into bands the reader can scan independently:

- **`query`** — read-only, no side effects, freely composable
- **`validate`** — read + binary health check (preflight)
- **`mutate`** — write to journal / sidecar; need flock + idempotency-key consideration
- **`submit`** — record a new submission (sidecar write + journal entry)
- **`scaffold`** — create new files (e.g. starter executor templates)
- **`workflow`** — end-to-end pipelines composing other primitives; same envelope shape so they're indistinguishable to higher-level callers (the Composite property)

<!-- BEGIN PRIMITIVE CATALOG -->
### `query` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [batch-status](batch-status.md) | yes | ssh: `<cluster>` | `hpc-agent batch-status [--experiment-dir <dir>]` |
| [campaign-advance](campaign-advance.md) | yes | _none_ | `hpc-agent campaign advance [--experiment-dir <dir>] --campaign-id <campaign_id> [--max-iters <max_iters>] [--metric <metric>] [--target <target>] [--direction <direction>] [--plateau-window <plateau_window>] [--plateau-tolerance <plateau_tolerance>] [--plateau-mode <plateau_mode>] [--max-jobs <max_jobs>] [--max-tasks <max_tasks>] [--max-walltime-sec <max_walltime_sec>] [--max-core-hours <max_core_hours>] [--circuit-breaker-failures <circuit_breaker_failures>] [--max-task-resubmits <max_task_resubmits>]` |
| [campaign-budget](campaign-budget.md) | yes | _none_ | `hpc-agent campaign budget [--experiment-dir <dir>] --campaign-id <campaign_id> [--max-jobs <max_jobs>] [--max-tasks <max_tasks>] [--max-walltime-sec <max_walltime_sec>] [--max-core-hours <max_core_hours>]` |
| [campaign-converged](campaign-converged.md) | yes | _none_ | `hpc-agent campaign converged [--experiment-dir <dir>] --campaign-id <campaign_id> [--max-iters <max_iters>] [--metric <metric>] [--target <target>] [--direction <direction>] [--plateau-window <plateau_window>] [--plateau-tolerance <plateau_tolerance>] [--plateau-mode <plateau_mode>]` |
| [campaign-health](campaign-health.md) | yes | _none_ | `hpc-agent campaign health [--experiment-dir <dir>] [--campaign-id <campaign_id>] [--since-iso <since_iso>] [--profile <profile>] [--cluster <cluster>]` |
| [campaign-list](campaign-list.md) | yes | _none_ | `hpc-agent campaign list [--experiment-dir <dir>]` |
| [campaign-replay](campaign-replay.md) | yes | _none_ | `hpc-agent campaign replay [--experiment-dir <dir>] --campaign-id <campaign_id> [--last-n <last_n>]` |
| [campaign-status](campaign-status.md) | yes | _none_ | `hpc-agent campaign status [--experiment-dir <dir>] --campaign-id <campaign_id>` |
| [capabilities](capabilities.md) | yes | _none_ | `hpc-agent capabilities [--full]` |
| [classify-axis-easy](classify-axis-easy.md) | yes | _none_ | `hpc-agent classify-axis-easy --source-path <source_path> --run-name <run_name>` |
| [classify-campaign-path](classify-campaign-path.md) | yes | _none_ | `hpc-agent classify-campaign-path --source-path <source_path>` |
| [clusters-describe](clusters-describe.md) | yes | _none_ | `hpc-agent clusters describe <name> [--strict]` |
| [clusters-list](clusters-list.md) | yes | _none_ | `hpc-agent clusters list` |
| [compute-run-id](compute-run-id.md) | yes | _none_ | `hpc-agent compute-run-id [--experiment-dir <dir>] --run-name <run_name>` |
| [dag-frontier](dag-frontier.md) | yes | _none_ | `hpc-agent dag-frontier [--experiment-dir <dir>]` |
| [decide-concurrency](decide-concurrency.md) | yes | _none_ | `hpc-agent decide-concurrency [--supports-async] [--remaining-jobs <remaining_jobs>] [--in-flight <in_flight>] [--k-cap <k_cap>]` |
| [decide-monitor-arm](decide-monitor-arm.md) | yes | _none_ | `hpc-agent decide-monitor-arm --spec <path>` |
| [decide-partial-handling](decide-partial-handling.md) | yes | _none_ | `hpc-agent decide-partial-handling --failed-count <failed_count> --combined-count <combined_count> [--retries-exhausted]` |
| [decide-resubmit](decide-resubmit.md) | yes | _none_ | `hpc-agent decide-resubmit --failed-count <failed_count> --total-tasks <total_tasks> [--resubmit-failed-threshold <resubmit_failed_threshold>]` |
| [describe](describe.md) | yes | _none_ | `hpc-agent describe <name>` |
| [detect-entry-point](detect-entry-point.md) | yes | _none_ | `hpc-agent detect-entry-point --experiment-dir <experiment_dir>` |
| [discover-executors](discover-executors.md) | yes | _none_ | `hpc-agent discover [--experiment-dir <dir>] [--search-dirs <search_dirs>]` |
| [discover-reducers](discover-reducers.md) | yes | _none_ | `hpc-agent discover-reducers [--experiment-dir <dir>]` |
| [discover-runs](discover-runs.md) | yes | _none_ | `hpc-agent discover-runs [--experiment-dir <dir>]` |
| [failures](failures.md) | yes | ssh: `<cluster>` | `hpc-agent failures [--experiment-dir <dir>] --run-id <run_id> [--lines <lines>]` |
| [fetch-skill-return](fetch-skill-return.md) | yes | filesystem: `<experiment_dir>/.hpc/_returns/` | `hpc-agent fetch-skill-return [--experiment-dir <dir>] --skill <skill> [--no-clear]` |
| [find](find.md) | yes | _none_ | `hpc-agent find <query> [--limit <limit>]` |
| [find-prior-run](find-prior-run.md) | yes | _none_ | `hpc-agent find-prior-run [--experiment-dir <dir>] --cmd-sha <cmd_sha>` |
| [inspect-parallel-axes](inspect-parallel-axes.md) | yes | _none_ | `hpc-agent inspect-parallel-axes [--experiment-dir <dir>]` |
| [list-in-flight](list-in-flight.md) | yes | _none_ | `hpc-agent list-in-flight [--experiment-dir <dir>]` |
| [load-context](load-context.md) | yes | _none_ | `hpc-agent load-context [--experiment-dir <dir>]` |
| [logs](logs.md) | yes | ssh: `<cluster>` | `hpc-agent logs [--experiment-dir <dir>] --run-id <run_id> [--task-id <task_ids>] [--all-failed] [--lines <lines>]` |
| [monitor-summary](monitor-summary.md) | yes | _none_ | `hpc-agent monitor-summary [--experiment-dir <dir>] --run-id <run_id>` |
| [plan-throughput](plan-throughput.md) | yes | _none_ | `hpc-agent plan-throughput --cluster <cluster> --total-tasks <total_tasks> [--est-task-duration-s <est_task_duration_s>]` |
| [poll-run-status](poll-run-status.md) | yes | ssh: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent status [--experiment-dir <dir>] --run-id <run_id> [--min-rows <min_rows>]` |
| [recall](recall.md) | yes | _none_ | `hpc-agent recall [--limit <limit>] [--include-runtime] [--include-generator-stats] [--root <root>] [--task-kind <task_kind>] [--operator <operator>] [--since <since>]` |
| [recommend-partition](recommend-partition.md) | yes | _none_ | `hpc-agent recommend-partition --spec <path> [--experiment-dir <dir>]` |
| [recoveries-list](recoveries-list.md) | yes | _none_ | `hpc-agent recoveries list` |
| [recoveries-show](recoveries-show.md) | yes | _none_ | `hpc-agent recoveries show --kind <kind> [--placeholders <placeholders>]` |
| [resolve-resources](resolve-resources.md) | yes | _none_ | `hpc-agent resolve-resources --cluster <cluster> [--experiment-dir <experiment_dir>] [--profile <profile>] [--cmd-sha <cmd_sha>] [--walltime-sec <walltime_sec>] [--gpu-type <gpu_type>] [--safety-mult <safety_mult>] [--partition <partition>] [--user-preferred-partition <user_preferred_partition>] [--mpi-pe <mpi_pe>] [--mpi-ranks <mpi_ranks>]` |
| [scaffold-spec](scaffold-spec.md) | yes | _none_ | `hpc-agent scaffold-spec [--experiment-dir <dir>] --verb <verb> [--cluster <cluster>] [--run-name <run_name>] [--from-context]` |
| [suggest-setup-action](suggest-setup-action.md) | yes | _none_ | `hpc-agent suggest-setup-action [--experiment-dir <dir>]` |
| [summarize-submit-plan](summarize-submit-plan.md) | yes | _none_ | `hpc-agent summarize-submit-plan --spec <path>` |
| [verify-aggregation-complete](verify-aggregation-complete.md) | yes | _none_ | `hpc-agent verify-aggregation-complete [--experiment-dir <dir>] --run-id <run_id> [--combiner-dir <combiner_dir_local>] [--results-dir <results_dir_local>]` |
| [verify-submitted](verify-submitted.md) | yes | ssh: `<cluster>` | `hpc-agent verify-submitted [--experiment-dir <dir>] --run-id <run_id>` |

### `validate` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [aggregate-preflight](aggregate-preflight.md) | yes | _none_ | `hpc-agent aggregate-preflight --experiment-dir <experiment_dir> [--reconcile-scheduler <reconcile_scheduler>]` |
| [check-preflight](check-preflight.md) | yes | _none_ | `hpc-agent preflight [--spec <path>] [--cluster <cluster>]` |
| [check-task-generator-mismatch](check-task-generator-mismatch.md) | yes | _none_ | `hpc-agent check-task-generator-mismatch --caller-task-generator <caller_task_generator> [--cached-task-generator <cached_task_generator>]` |
| [classify-axis-preflight](classify-axis-preflight.md) | yes | _none_ | `hpc-agent classify-axis-preflight --experiment-dir <experiment_dir> [--run-name <run_name>] [--run-signature-sha <run_signature_sha>] [--root <root>] [--task-kind <task_kind>] [--data-axis-supplied]` |
| [dry-run-local](dry-run-local.md) | yes | _none_ | `(none — Python-only primitive)` |
| [prepare-phase2-spec](prepare-phase2-spec.md) | yes | _none_ | `hpc-agent prepare-phase2-spec --spec <path>` |
| [smoke-test-executor](smoke-test-executor.md) | yes | runs: `user`; filesystem: `<output_file>` | `hpc-agent smoke-test-executor --module-path <module_path> [--output-file <output_file>]` |
| [status-preflight](status-preflight.md) | yes | _none_ | `hpc-agent status-preflight --experiment-dir <experiment_dir>` |
| [submit-preflight](submit-preflight.md) | yes | _none_ | `hpc-agent submit-preflight --experiment-dir <experiment_dir> [--cluster <cluster>] [--profile <profile>] [--cmd-sha <cmd_sha>] [--walltime-sec <walltime_sec>] [--gpu-type <gpu_type>] [--safety-mult <safety_mult>] [--partition <partition>] [--user-preferred-partition <user_preferred_partition>]` |
| [validate-executor-signatures](validate-executor-signatures.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-input-dataset](validate-input-dataset.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-parents-ready](validate-parents-ready.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-self-qos-limit](validate-self-qos-limit.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-stochastic-marker](validate-stochastic-marker.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-walltime-against-history](validate-walltime-against-history.md) | yes | _none_ | `(none — Python-only primitive)` |

### `mutate` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [cluster-reduce](cluster-reduce.md) | yes | ssh: `<cluster>`; sync-pull: `<remote_path>/<output_rel>` | `hpc-agent cluster-reduce [--experiment-dir <dir>] --run-id <run_id> [--aggregate-cmd <aggregate_cmd>] [--output-path <output_path>] [--local-dir <local_dir>] [--extra-env <extra_env>] [--timeout-sec <timeout_sec>]` |
| [combine-wave](combine-wave.md) | yes | ssh: `<cluster>`; runs: `cluster-side`; writes-cluster: `<output_dir>/_combiner/wave_<N>.json`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent aggregate [--experiment-dir <dir>] --run-id <run_id> --wave <wave> [--force] [--require-outputs <require_outputs>] [--expect-output <expect_output>]` |
| [emit-skill-return](emit-skill-return.md) | yes | filesystem: `<experiment_dir>/.hpc/_returns/` | `hpc-agent emit-skill-return [--experiment-dir <dir>] --skill <skill>` |
| [mark-run-terminal](mark-run-terminal.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `(none — Python-only primitive)` |
| [provenance-manifest](provenance-manifest.md) | yes | file_write: `<experiment>/.hpc/provenance/<campaign_id>.json` | `hpc-agent provenance-manifest --spec <path> [--experiment-dir <dir>]` |
| [prune-orphan-sidecars](prune-orphan-sidecars.md) | yes | removes-files: `<experiment>/.hpc/runs/*.json` | `(none — Python-only primitive)` |
| [reconcile-journal](reconcile-journal.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`; ssh: `<cluster>` | `hpc-agent reconcile [--experiment-dir <dir>] --run-id <run_id> --scheduler <scheduler>` |
| [resubmit-failed](resubmit-failed.md) | yes | scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent resubmit [--experiment-dir <dir>] --run-id <run_id> --spec <spec>` |
| [update-run-constraints](update-run-constraints.md) | yes | ssh: `<cluster>` | `(none — Python-only primitive)` |
| [write-run-sidecar](write-run-sidecar.md) | yes | file_write: `<experiment>/.hpc/runs/<run_id>.json` | `hpc-agent write-run-sidecar --spec <path> [--experiment-dir <dir>]` |

### `submit` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [submit-spec](submit-spec.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`; scheduler-submit: `<cluster>` | `hpc-agent submit --spec <path> [--experiment-dir <dir>] [--dry-run]` |

### `scaffold` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [axes-init](axes-init.md) | yes | writes-sidecar: `<experiment>/.hpc/axes.yaml` | `hpc-agent axes-init [--experiment-dir <dir>] [--axes <axes>] [--homogeneous-axes <homogeneous_axes>] [--force]` |
| [build-executor](build-executor.md) | no | writes-file: `<output_dir>/<name>.py` | `hpc-agent build-executor --name <name> [--output-dir <output_dir>] [--type <type>] [--force]` |
| [build-submit-spec](build-submit-spec.md) | yes | _none_ | `hpc-agent build-submit-spec --spec <path> [--experiment-dir <dir>]` |
| [build-tasks-py](build-tasks-py.md) | yes | writes-sidecar: `<experiment>/.hpc/tasks.py`; writes-sidecar: `<experiment>/.hpc/cli.py` | `hpc-agent build-tasks-py [--experiment-dir <dir>] --spec <spec> [--force]` |
| [build-template](build-template.md) | yes | writes-file: `<repo_dir>/{.hpc/template.mk,.hpc/scaffold.py}` | `hpc-agent build-template [--repo-dir <repo_dir>] [--force] [--shape <shape>]` |
| [campaign-acknowledge-budget](campaign-acknowledge-budget.md) | yes | writes-sidecar: `<experiment>/.hpc/campaigns/<id>/budget_ack.json` | `hpc-agent campaign acknowledge-budget [--experiment-dir <dir>] --campaign-id <campaign_id> [--note <note>] [--max-jobs <max_jobs>] [--max-tasks <max_tasks>] [--max-walltime-sec <max_walltime_sec>] [--max-core-hours <max_core_hours>]` |
| [campaign-init](campaign-init.md) | yes | writes-sidecar: `<experiment>/.hpc/campaigns/<id>/manifest.json` | `hpc-agent campaign init [--experiment-dir <dir>] --campaign-id <campaign_id> [--goal <goal>] [--max-iters <max_iters>] [--metric <metric>] [--target <target>] [--direction <direction>] [--plateau-window <plateau_window>] [--plateau-tolerance <plateau_tolerance>] [--plateau-mode <plateau_mode>] [--max-jobs <max_jobs>] [--max-tasks <max_tasks>] [--max-walltime-sec <max_walltime_sec>] [--max-core-hours <max_core_hours>] [--circuit-breaker-failures <circuit_breaker_failures>] [--max-task-resubmits <max_task_resubmits>] [--strategy-name <strategy_name>] [--strategy-params-json <strategy_params_json>]` |
| [classify-axis](classify-axis.md) | yes | writes-sidecar: `<experiment>/.hpc/axes.yaml` | `hpc-agent classify-axis --spec <path> [--experiment-dir <dir>]` |
| [export-package](export-package.md) | yes | writes-sidecar: `<experiment>/src/*.py`; writes-sidecar: `<experiment>/.hpc/.build-cache.json` | `hpc-agent export-package [--experiment-dir <dir>] [--force]` |
| [install-commands](install-commands.md) | yes | filesystem: `~/.claude/` | `hpc-agent install-commands [--dry-run] [--claude-dir <claude_dir>]` |
| [interview](interview.md) | yes | file_write: `<campaign_dir>/{interview.json,meta.json,.claude/settings.json}` | `hpc-agent interview --spec <path> --campaign-dir <campaign_dir>` |
| [prepare-followup-specs](prepare-followup-specs.md) | yes | writes-followup-specs: `<experiment_dir>/monitor_spec.json` | `hpc-agent prepare-followup-specs --experiment-dir <experiment_dir> --run-id <run_id> [--cmd-sha <cmd_sha>] [--profile <profile>]` |
| [setup](setup.md) | yes | filesystem: `~/.claude/`; ssh: `<cluster>` | `hpc-agent setup [--dry-run] [--claude-dir <claude_dir>] [--cluster <cluster>] [--experiment-dir <experiment_dir>] [--install-cron]` |

### `workflow` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [aggregate-flow](aggregate-flow.md) | yes | ssh: `<cluster>`; sync-pull: `<ssh_target>:<remote_path>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent aggregate-flow [--spec <path>] [--experiment-dir <dir>] [--dry-run] [--run-id <run_id>]` |
| [campaign-run](campaign-run.md) | yes | scheduler-submit: `<cluster>`; ssh: `<cluster>`; writes-aggregate-output: `<experiment_dir>/_aggregated/<run_id>/` | `hpc-agent campaign-run --spec <path> [--experiment-dir <dir>]` |
| [monitor-flow](monitor-flow.md) | yes | ssh: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent monitor-flow --spec <path> [--experiment-dir <dir>] [--dry-run]` |
| [resolve-submit-inputs](resolve-submit-inputs.md) | yes | writes-sidecar: `<experiment>/.hpc/tasks.py`; writes-sidecar: `<experiment>/.hpc/cli.py`; writes-sidecar: `<experiment>/.hpc/runs/<run_id>.json` | `hpc-agent resolve-submit-inputs --spec <path> [--experiment-dir <dir>]` |
| [status-pipeline](status-pipeline.md) | yes | ssh: `<cluster>`; writes-tick-log: `<experiment_dir>/<run_id>.monitor.jsonl` | `hpc-agent status-pipeline --spec <path> [--experiment-dir <dir>]` |
| [submit-and-verify](submit-and-verify.md) | yes | scheduler-submit: `<cluster>`; ssh: `<cluster>` | `hpc-agent submit-and-verify --spec <path> [--experiment-dir <dir>]` |
| [submit-flow](submit-flow.md) | yes | sync-push: `<ssh_target>:<remote_path>`; scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent submit-flow --spec <path> [--experiment-dir <dir>] [--dry-run] [--partial-ok] [--invalidate-on-code-change]` |
| [submit-flow-batch](submit-flow-batch.md) | yes | sync-push: `<ssh_target>:<remote_path>`; scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent submit-flow-batch --spec <path> [--experiment-dir <dir>] [--dry-run]` |
| [submit-pipeline](submit-pipeline.md) | yes | scheduler-submit: `<cluster>`; ssh: `<cluster>`; writes-followup-specs: `<experiment_dir>/{monitor,aggregate}_spec.json` | `hpc-agent submit-pipeline --spec <path> [--experiment-dir <dir>]` |
| [validate-campaign](validate-campaign.md) | yes | _none_ | `hpc-agent validate-campaign --spec <path> [--experiment-dir <dir>]` |
| [verify-canary](verify-canary.md) | yes | ssh: `<cluster>` | `hpc-agent verify-canary [--experiment-dir <dir>] --canary-run-id <canary_run_id> [--expect-output <expect_output>] [--fingerprint <fingerprint>] [--verify-checkpoint] [--checkpoint-result-dir <checkpoint_result_dir>] [--poll-interval-sec <poll_interval_sec>] [--wait-budget-sec <wait_budget_sec>]` |
<!-- END PRIMITIVE CATALOG -->

## How slash commands and skills consume primitives

Both surfaces compose from the same catalog but with different concerns layered on top:

**Slash command body** (human-facing): pre-amble that asks the user about choices → invoke primitive → present results in human-readable form → ask whether to continue → next primitive.

**Skill body** (agent-facing): preconditions check → invoke primitive → parse the JSON envelope → branch on `error_code` → next primitive. No interactive prompts. The body is mostly a pipeline declaration.

When the same operation is needed from both surfaces, both files reference the primitive — they don't restate the contract. Drift is bounded to surface-specific concerns (interactive prompts for slash commands; envelope-parsing recipes for skills).

## Adding a primitive

1. Identify a single operation that maps cleanly to one CLI subcommand or one Python function in `hpc_agent.ops.*` / `hpc_agent`. If it doesn't, the operation is too large; split it.
2. Write `docs/primitives/<name>.md` with the frontmatter contract and a short body.
3. Update consumers (slash commands, skills) to point at the primitive instead of restating its contract.
4. Run `uv run python scripts/build_primitive_index.py` — the catalog table above regenerates from frontmatter; no hand-editing.

The bar is "would this contract be useful to a caller that doesn't care how it's implemented?" If yes, it's a primitive. If the operation is just "the agent does some reasoning and writes a file", that's not a primitive — that's surface-specific glue.
