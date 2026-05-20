# Primitives

A **primitive** is the smallest agent-and-human-shareable operation: one verb, one input contract, one output contract, one set of error codes, one declared side-effect class. Both `slash_commands/commands/*.md` (human-facing, interactive) and `skills/hpc-*/SKILL.md` (agent-facing, terse) **compose** from this catalog instead of describing the same operations from scratch.

Why this layer exists:

- **Single source of truth.** "How to submit a spec" lives in exactly one file. Today the same flow is described in `slash_commands/commands/submit-hpc.md` (770 lines, interactive) and `skills/hpc-submit/SKILL.md` (88 lines, terse) — and the two drift independently.
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
  python: hpc_agent.runner.submit_and_record
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
| [best-submit-window](best-submit-window.md) | yes | _none_ | `hpc-agent best-submit-window --profile <p> --cluster <c> [--within-hours N] [--top-k K]` |
| [campaign-advance](campaign-advance.md) | yes | _none_ | `hpc-agent campaign advance --campaign-id <id>` |
| [campaign-budget](campaign-budget.md) | yes | _none_ | `hpc-agent campaign budget --campaign-id <id>` |
| [campaign-converged](campaign-converged.md) | yes | _none_ | `hpc-agent campaign converged --campaign-id <id>` |
| [campaign-health](campaign-health.md) | yes | _none_ | `hpc-agent campaign-health [--campaign-id <id>] [--since-iso <ts>]` |
| [campaign-list](campaign-list.md) | yes | _none_ | `hpc-agent campaign list [--experiment-dir <dir>]` |
| [campaign-replay](campaign-replay.md) | yes | _none_ | `hpc-agent campaign replay --campaign-id <id> [--last-n <n>]` |
| [campaign-status](campaign-status.md) | yes | _none_ | `hpc-agent campaign status --campaign-id <id> [--experiment-dir <dir>]` |
| [capabilities](capabilities.md) | yes | _none_ | `hpc-agent capabilities` |
| [clusters-describe](clusters-describe.md) | yes | _none_ | `hpc-agent clusters describe <name> [--strict]` |
| [clusters-list](clusters-list.md) | yes | _none_ | `hpc-agent clusters list` |
| [decide-monitor-arm](decide-monitor-arm.md) | yes | _none_ | `hpc-agent decide-monitor-arm --spec <path>` |
| [discover-executors](discover-executors.md) | yes | _none_ | `hpc-agent discover --experiment-dir <path> [--search-dirs <a,b,c>]` |
| [discover-reducers](discover-reducers.md) | yes | _none_ | `hpc-agent discover-reducers --experiment-dir <path>` |
| [failures](failures.md) | yes | ssh: `<cluster>` | `hpc-agent failures --run-id <id> [--lines <n>]` |
| [find-prior-run](find-prior-run.md) | yes | _none_ | `hpc-agent find-prior-run --experiment-dir <path> --cmd-sha <hex>` |
| [house-edge](house-edge.md) | yes | _none_ | `hpc-agent house-edge --profile <name> --cluster <name> [--cmd-sha <sha>]` |
| [inspect-cluster](inspect-cluster.md) | yes | ssh: `<cluster>` | `hpc-agent inspect-cluster --cluster <name> [...]` |
| [list-in-flight](list-in-flight.md) | yes | _none_ | `hpc-agent list-in-flight --experiment-dir <path>` |
| [logs](logs.md) | yes | ssh: `<cluster>` | `hpc-agent logs --run-id <id> (--task-id <ids> | --all-failed) [--lines <n>]` |
| [monitor-summary](monitor-summary.md) | yes | _none_ | `hpc-agent monitor-summary --experiment-dir <path> --run-id <id>` |
| [plan-throughput](plan-throughput.md) | yes | _none_ | `hpc-agent plan-throughput --cluster <name> --total-tasks <n> [--est-task-duration-s <n>]` |
| [poll-run-status](poll-run-status.md) | yes | ssh: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent status --run-id <id> [--experiment-dir <dir>]` |
| [predict-queue-wait](predict-queue-wait.md) | yes | _none_ | `hpc-agent predict-queue-wait --profile <p> --cluster <c> [--backend auto|des|diurnal_ma] [--n-replications N] [--at-iso <iso>] [--seed N]` |
| [predict-start-time](predict-start-time.md) | yes | _none_ | `hpc-agent predict-start-time --spec <path>` |
| [read-runtime-prior](read-runtime-prior.md) | yes | _none_ | `hpc-agent runtime-prior --profile <name> --cluster <name> [--cmd-sha <sha>]` |
| [recall](recall.md) | yes | _none_ | `hpc-agent recall` |
| [recommend-partition](recommend-partition.md) | yes | _none_ | `(none — Python-only primitive)` |
| [recommend-wait-alternative](recommend-wait-alternative.md) | yes | _none_ | `(none — Python-only primitive)` |
| [score-submit-plan](score-submit-plan.md) | yes | ssh: `<cluster>` | `hpc-agent plan-submit --profile <name> --cluster <name> [...]` |
| [suggest-setup-action](suggest-setup-action.md) | yes | _none_ | `hpc-agent suggest-setup-action --experiment-dir <path>` |
| [summarize-submit-plan](summarize-submit-plan.md) | yes | _none_ | `hpc-agent summarize-submit-plan --spec <path>` |
| [verify-aggregation-complete](verify-aggregation-complete.md) | yes | _none_ | `hpc-agent verify-aggregation-complete --experiment-dir <path> --run-id <id> --combiner-dir <path>` |
| [walltime-drift](walltime-drift.md) | yes | _none_ | `hpc-agent walltime-drift --profile <name> --cluster <name> [--cmd-sha <sha>] [--base-safety-mult <f>]` |

### `validate` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [check-preflight](check-preflight.md) | yes | _none_ | `hpc-agent preflight [--cluster <name>]` |
| [validate](validate.md) | yes | ssh: `<cluster>` | `(none — Python-only primitive)` |
| [validate-executor-signatures](validate-executor-signatures.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-input-dataset](validate-input-dataset.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-self-qos-limit](validate-self-qos-limit.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-stochastic-marker](validate-stochastic-marker.md) | yes | _none_ | `(none — Python-only primitive)` |
| [validate-walltime-against-history](validate-walltime-against-history.md) | yes | _none_ | `(none — Python-only primitive)` |

### `mutate` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [cluster-reduce](cluster-reduce.md) | yes | ssh: `<cluster>`; sync-pull: `<remote_path>/<output_rel>` | `hpc-agent cluster-reduce --experiment-dir <path> --run-id <id> [--aggregate-cmd <cmd>]` |
| [combine-wave](combine-wave.md) | yes | ssh: `<cluster>`; runs: `cluster-side`; writes-cluster: `<output_dir>/_combiner/wave_<N>.json`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]` |
| [mark-run-terminal](mark-run-terminal.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `(none — Python-only primitive)` |
| [prune-orphan-sidecars](prune-orphan-sidecars.md) | yes | removes-files: `<experiment>/.hpc/runs/*.json` | `(none — Python-only primitive)` |
| [reconcile-journal](reconcile-journal.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`; ssh: `<cluster>` | `hpc-agent reconcile --run-id <id> --scheduler {sge|slurm} [--experiment-dir <dir>]` |
| [resubmit-failed](resubmit-failed.md) | yes | scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent resubmit --run-id <id> --spec spec.json [--experiment-dir <dir>]` |
| [update-run-constraints](update-run-constraints.md) | yes | ssh: `<cluster>` | `(none — Python-only primitive)` |

### `submit` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [submit-spec](submit-spec.md) | yes | writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json`; scheduler-submit: `<cluster>` | `hpc-agent submit --spec <path> [--experiment-dir <dir>] [--dry-run]` |

### `scaffold` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [axes-init](axes-init.md) | yes | writes-sidecar: `<experiment>/.hpc/axes.yaml` | `hpc-agent axes-init` |
| [build-executor](build-executor.md) | no | writes-file: `<output_dir>/<name>.py` | `hpc-agent build-executor --name <stem> [--output-dir <dir>] [--type plain] [--force]` |
| [build-submit-spec](build-submit-spec.md) | yes | _none_ | `hpc-agent build-submit-spec --spec <path>` |
| [build-tasks-py](build-tasks-py.md) | yes | writes-sidecar: `<experiment>/.hpc/tasks.py` | `hpc-agent build-tasks-py --spec <path>` |
| [campaign-init](campaign-init.md) | yes | writes-sidecar: `<experiment>/.hpc/campaigns/<id>/manifest.json` | `hpc-agent campaign init --campaign-id <id> --strategy <s>` |
| [interview](interview.md) | yes | file_write: `<campaign_dir>/{interview.json,meta.json}` | `hpc-agent interview` |

### `workflow` primitives

| Primitive | Idempotent | Side effects | CLI |
|---|---|---|---|
| [aggregate-flow](aggregate-flow.md) | yes | ssh: `<cluster>`; sync-pull: `<ssh_target>:<remote_path>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent aggregate-flow --spec <path>` |
| [monitor-flow](monitor-flow.md) | yes | ssh: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent monitor-flow --spec <path>` |
| [submit-flow](submit-flow.md) | yes | sync-push: `<ssh_target>:<remote_path>`; scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent submit-flow --spec <path>` |
| [submit-flow-batch](submit-flow-batch.md) | yes | sync-push: `<ssh_target>:<remote_path>`; scheduler-submit: `<cluster>`; writes-journal: `~/.claude/hpc/<repo_hash>/runs/<run_id>.json` | `hpc-agent submit-flow-batch --spec <path>` |
| [validate-campaign](validate-campaign.md) | yes | _none_ | `hpc-agent validate-campaign --spec <path>` |
| [verify-canary](verify-canary.md) | yes | ssh: `<cluster>` | `hpc-agent verify-canary --experiment-dir <path> --canary-run-id <id> [--expect-output <path>] [--fingerprint <relpath>]` |
<!-- END PRIMITIVE CATALOG -->

## How slash commands and skills consume primitives

Both surfaces compose from the same catalog but with different concerns layered on top:

**Slash command body** (human-facing): pre-amble that asks the user about choices → invoke primitive → present results in human-readable form → ask whether to continue → next primitive.

**Skill body** (agent-facing): preconditions check → invoke primitive → parse the JSON envelope → branch on `error_code` → next primitive. No interactive prompts. The body is mostly a pipeline declaration.

When the same operation is needed from both surfaces, both files reference the primitive — they don't restate the contract. Drift is bounded to surface-specific concerns (interactive prompts for slash commands; envelope-parsing recipes for skills).

## Adding a primitive

1. Identify a single operation that maps cleanly to one CLI subcommand or one Python function in `hpc_agent.runner` / `hpc_agent`. If it doesn't, the operation is too large; split it.
2. Write `docs/primitives/<name>.md` with the frontmatter contract and a short body.
3. Update consumers (slash commands, skills) to point at the primitive instead of restating its contract.
4. Run `uv run python scripts/build_primitive_index.py` — the catalog table above regenerates from frontmatter; no hand-editing.

The bar is "would this contract be useful to a caller that doesn't care how it's implemented?" If yes, it's a primitive. If the operation is just "the agent does some reasoning and writes a file", that's not a primitive — that's surface-specific glue.
