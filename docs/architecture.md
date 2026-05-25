# Architecture

hpc-agent is a primitive-based HPC orchestration framework. The package
is organised as a layered DAG of *roles* (kernel, wire, substrate,
models, subjects, surfaces) — each layer depends on lower layers but
not the other way round. Inside the subject layer, each top-level
directory under `ops/` and `meta/` is a self-contained vertical that
does not reach sideways into its peers. New code finds its destination
by asking "what role am I writing in?" and following the layering
rules.

## Layering DAG

```
┌─────────────────────────────────────────────────────────────────────┐
│  Surfaces (what the user / agent calls into)                        │
│                                                                     │
│  src/slash_commands/commands/  src/slash_commands/skills/           │
│  user-typed entry points        in-chat Skill-tool utilities        │
│  (paired or workflow-trigger)   (2 paired with slashes)             │
│                                                                     │
│  src/hpc_agent/_kernel/extension/worker_prompts/                    │
│  delegated-worker prompts (submit, status, aggregate, campaign)     │
│                                  ↓                                  │
│  cli/dispatch.py (`hpc-agent` console script — main())              │
│    └ delegates to cli/parser.py + cli/_dispatch.py                  │
│  argparse + verb groups (validate/build/clusters) + flat aliases    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Workflow primitives (verb="workflow" — multi-step orchestration    │
│  composed declaratively via @primitive(composes=[...]))             │
│                                                                     │
│  ops/submit_flow.py        submit-flow                              │
│  ops/monitor_flow.py       monitor-flow                             │
│  ops/aggregate_flow.py     aggregate-flow                           │
│  ops/verify_canary.py      verify-canary                            │
│  ops/recover_flow.py       recover-flow                             │
│  meta/validate_campaign.py validate-campaign                        │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Subjects — each a self-contained vertical (atoms, runners,         │
│  classifiers). No cross-subject imports; see "Cross-subject         │
│  composition" below.                                                │
│                                                                     │
│  ops/        operational subjects (atoms only — workflows sit at    │
│              ops/ root as sibling files; see above)                 │
│   ├ aggregate/  combine, cluster_reduce, invariants, runner         │
│   ├ clusters/   list, describe                                      │
│   ├ memory/     recall, interview                                   │
│   ├ monitor/    status, reconcile, logs, list_in_flight, arm,       │
│   │            summary, update_constraints, logs_atom               │
│   ├ preflight/  check                                               │
│   ├ recover/    runner, batching, failure_signatures,               │
│   │            failures_atom, runner_failures                       │
│   ├ submit/     runner, plan_summary, plan_throughput,              │
│   │            recommend_partition                                  │
│   └ validate/   executor_signatures, input_dataset, self_qos_limit, │
│                stochastic_marker, walltime_against_history          │
│                                                                     │
│  meta/       "operations about operations" — workflows at root,     │
│              subject dirs hold atoms                                │
│   └ campaign/   driver, cursor, dirs, manifest, atoms/              │
│                (atoms/ holds advance, budget, converged, init,      │
│                health, list_campaigns, load_context, replay,        │
│                status — the per-tick steps load-context spawns)     │
│                                                                     │
│  incorporation/  scaffolding primitives                             │
│      axes_init, classify_axis, export_package, build/{executor,     │
│      submit_spec, tasks_py, template}                               │
│                                                                     │
│  Cross-subject primitive bridge:                                    │
│      runner.py (package-root) re-exports a small back-compat        │
│      surface for atom-to-atom cross-subject calls. Most workflow    │
│      composition is now direct: workflows live at ops/ + meta/ root │
│      (sibling to subjects) and import atoms inside subjects without │
│      a bridge.                                                      │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Models (domain logic that runs on the cluster, not the laptop)     │
│                                                                     │
│  models/mapreduce/  combiner + reducer + dispatch                   │
│    ├ combiner.py    per-wave on-cluster combiner driver             │
│    ├ dispatch.py    array-batch task dispatcher                     │
│    ├ metrics_io.py  per-task metrics sidecar writer                 │
│    ├ reduce/        status, classify, history, metrics              │
│    └ templates/     job-script scaffolds + tasks_example.py         │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Substrate (cross-cutting — NOT subjects; horizontal services        │
│  every subject is allowed to import)                                │
│                                                                     │
│  infra/                        state/                               │
│   ├ remote.py  ssh/scp/rsync   ├ runs.py        run sidecars        │
│   ├ backends/  sge, slurm      ├ journal.py     per-run journal     │
│   ├ inspect/   qstat/scontrol  ├ run_record.py  RunRecord shape     │
│   ├ clusters.py  YAML loader   ├ index.py       discovery index     │
│   ├ gpu.py    GPU selection    ├ discover.py    executor discovery  │
│   ├ throughput.py   planner    ├ runtime_prior  walltime/n_samples  │
│   ├ constraints.py             ├ stages.py      multi-stage DAG     │
│   ├ cluster_status.py SSH      ├ axes.py        axis manifest       │
│   ├ cluster_logs.py   tail     └ user_profiles  per-user knobs      │
│   ├ time.py   canonical UTC                                         │
│   ├ io.py     atomic flock                                          │
│   ├ parsing.py                                                      │
│   └ cache.py                                                        │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Kernel (framework machinery — registry, contracts, lifecycle,      │
│  agent-extension surfaces). Subjects depend on kernel; kernel       │
│  never reaches up into subjects.                                    │
│                                                                     │
│  _kernel/                                                           │
│   ├ registry/      primitive decorator + operations catalog +       │
│   │                plugin loader                                    │
│   │   ├ primitive.py   @primitive + PrimitiveMeta + SideEffect      │
│   │   ├ operations.py  agent-facing operations catalog envelope     │
│   │   └ plugins.py     hpc_agent.plugins entry-point loader         │
│   ├ contract/      schema + layout invariants                       │
│   │   ├ schema.py      runtime spec validation                      │
│   │   └ layout.py      RepoLayout, JournalLayout                    │
│   ├ lifecycle/     primitive lifecycle + spawn invocation           │
│   │   ├ lifecycle.py   StrEnum: LifecycleState, FailureCategory     │
│   │   ├ invoke.py      WorkerInvoker, InvocationResult,             │
│   │   │                RenderedPrompt                               │
│   │   └ playbook.py                                                 │
│   └ extension/     kernel-to-agent surfaces                         │
│       ├ capabilities.py   operations-catalog envelope (kernel       │
│       │                   introspection primitive)                  │
│       ├ spawn_prompt.py   spawn-contract render/parse               │
│       ├ telemetry.py      monitor.jsonl writer                      │
│       ├ version.py        cross-domain schema manifest              │
│       └ worker_prompts/   worker procedure markdown package         │
│                           (loaded via importlib.resources)          │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Wire (Pydantic v2 models — authoring SoT for every wire shape)     │
│                                                                     │
│  _wire/                                                             │
│   ├ _shared.py         shared aliases (RunIdStrict, Scheduler,      │
│   │                    LifecycleStateTerminal, ErrorCode, …)        │
│   ├ spawn_contract.py  spawn-prompt envelope                        │
│   ├ actions/           input specs for mutating primitives          │
│   ├ queries/           input/output for query primitives            │
│   ├ validators/        input specs for validate primitives          │
│   ├ fixtures/          test/round-trip fixtures                     │
│   └ workflows/         input/output for workflow primitives         │
│                                                                     │
│  schemas/   JSON Schemas generated from _wire/ by                   │
│             scripts/build_schemas.py. The *wire* SoT every          │
│             external consumer reads.                                │
└─────────────────────────────────────────────────────────────────────┘
```

Cross-cutting:

- **`_wire/`** — Pydantic v2 BaseModels grouped by domain
  (`workflows/`, `validators/`, `fixtures/`, `queries/`, `actions/`).
  The *authoring* SoT for every wire shape.
- **`schemas/`** — JSON Schemas, regenerated from `_wire/` by
  `scripts/build_schemas.py`. The *wire* SoT every external consumer
  reads.
- **`docs/primitives/`** — one `.md` per `@primitive`. Frontmatter
  auto-generated from the registry; bodies hand-written. The
  agent-context surface (`hpc-agent capabilities --full` projects them).
- **`runner.py`** (package-root) — cross-subject primitive bridge +
  back-compat shim. See "Cross-subject composition" below.

## The decide / act boundary

The single most important invariant: **pure planning code does not
mutate the cluster; only primitives carrying a declared `ssh` /
`scheduler-submit` side effect do.** Each `@primitive` declares a
`verb` and a `side_effects` tuple; the registry IS the source of truth
for that boundary.

| Verb       | Reads                                  | Writes                  | Side effects |
|------------|----------------------------------------|-------------------------|--------------|
| `query`    | spec / history, cluster snapshot       | nothing                 | none / read-only ssh |
| `validate` | spec / history                         | nothing                 | none         |
| `scaffold` | spec / templates                       | local files             | filesystem   |
| `mutate`   | spec + cluster state                   | journal + sidecars + cluster | scoped ssh   |
| `submit`   | spec + plan                            | journal + cluster state | ssh / qsub   |
| `workflow` | composes the above                     | what its atoms write    | what its atoms declare |

Pure-planning helpers (the throughput planner, resubmit batcher, axis
classifier, etc.) live in `infra/` and `ops/<subject>/` as
`verb="query"` primitives or plain functions — never with
`subprocess.run("ssh ...")` inline. The convention is: the slash
command runs the SSH; the framework primitive parses the text. This
keeps planning replayable and unit-testable, and keeps audits of "what
does this primitive touch?" trivial.

The advisory / forecasting layer (queue-wait prediction, submit-plan
scoring) lives in the optional `hpc-agent-pro` plugin, which
re-attaches through the `hpc_agent.plugins` entry-point seam wired up
in `_kernel/registry/plugins.py`.

## The @primitive registry

Every wire-callable operation is decorated:

```python
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent import errors

@primitive(
    name="summarize-submit-plan",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli=CliShape(spec_arg="SummarizeSubmitPlanSpec", ...),
    agent_facing=True,
)
def summarize_submit_plan(...): ...
```

The decorator:

- Registers the function in a process-wide registry
  (`get_registry()` / `get_meta(name)`)
- Drives `cli/_dispatch.py`'s generic adapter (the `cli=` field is a
  `CliShape` declaration — see `cli/_dispatch.py`)
- Drives `docs/primitives/<name>.md`'s frontmatter (regenerated by
  `scripts/build_primitive_frontmatter.py`)
- Drives `docs/generated/operations.md` (regenerated by
  `scripts/build_operations_index.py`)
- Drives the JSON-schema filename (`schemas/<name>.input.json` /
  `<name>.output.json`)

### Declarative composition: `composes=`

A composite primitive (workflow or scaffold) declares the atoms it
delegates to via `composes=`. The decorator accepts two forms:

1. **String names** — `composes=["combine-wave", "verify-canary"]`.
   This is the canonical form for **cross-subject composition** —
   declarative metadata that doesn't import the target primitive's
   module, so it doesn't trip the subject-imports lint. The names are
   resolved against the live `_REGISTRY` at decoration time; a typo
   or rename becomes an import-time `ValueError`.
2. **Function references** — `composes=[combine_wave, verify_canary]`.
   Same-subject only; the referenced atom must already be decorated
   (its `_primitive_meta` attribute is consulted), so the
   `_PRIMITIVE_MODULES` ordering in `_kernel/registry/primitive.py`
   puts atoms before the composites that reference them.

For the *callable* form of cross-subject composition see
"Cross-subject composition" below.

Population happens via `register_primitives()` — explicit
import-once-at-startup of every module listed in `_PRIMITIVE_MODULES`.
Querying the registry before registration raises `RuntimeError` (the
old auto-import-on-first-query path silently swallowed `ImportError`
and made missing-decorator bugs hard to diagnose). Tests use an
autouse fixture; the `hpc-agent` CLI invokes it from `main()` before
dispatch.

To add a primitive, follow the recipe in
[`internals/adding-a-primitive.md`](internals/adding-a-primitive.md).
The mechanical pieces are all generated; you write the function body
+ the doc body.

## Two source-of-truth chains

The framework keeps a strict 2-step SoT chain so wire consumers and
human / LLM consumers stay in lockstep:

1. **Wire shapes**: Pydantic model in `_wire/<domain>/<name>.py`
   → JSON Schema in `schemas/<name>.input.json`, regenerated by
   `scripts/build_schemas.py`. CI gates on `--check`.

2. **Operation catalog**: `@primitive` decorator → frontmatter in
   `docs/primitives/<name>.md`, regenerated by
   `scripts/build_primitive_frontmatter.py`. CI gates on `--check`.
   The bodies of those docs are hand-written.

Editing a Pydantic model without re-running `build_schemas.py --write`
fails CI. Editing a `@primitive(...)` decorator without re-running
`build_primitive_frontmatter.py --write` fails CI.

## CLI surface

`hpc-agent` (entry point: `hpc_agent.cli.dispatch:main`, per
`pyproject.toml`) exposes every primitive as a subcommand. The
parser is built by walking the registry — each primitive's `cli=`
field is a `CliShape` consumed by `hpc_agent.cli._dispatch`. Tier-3
verbs that don't have a `@primitive` backing (`run`, `capabilities`,
`install-commands`, `setup`, `describe`) declare their own
`register(sub)` function in `cli/<module>.py` and are aggregated by
`cli/parser.py`. The `cli/main.py` module re-exports `main` so
external callers can `from hpc_agent.cli import main`; the canonical
entry is `hpc_agent.cli.dispatch:main`.

Subcommands can be invoked flat (`hpc-agent validate-campaign ...`)
or under a verb group (`hpc-agent validate validate-campaign ...`);
the verb groups (`validate`, `build`, `clusters`, plus the existing
`campaign`) are argv pre-processors so flat-form invocations always
keep working.

The agent-facing JSON envelope is uniform: `{"ok": bool, "data": {...}}`
on success, `{"ok": false, "error_code": str, "category": str,
"retry_safe": bool, ...}` on failure. Documented at
[`reference/cli-spec.md`](reference/cli-spec.md).

## Agent surfaces

Three, mirroring the three call-sites a workflow can fire from:

1. **Skills** (`src/slash_commands/skills/<id>/SKILL.md`) —
   in-chat utilities Claude Code's interactive session invokes via the
   `Skill` tool. Small focused actions (`hpc-build-executor`,
   `hpc-classify-axis`); paired 1:1 with a slash command. Have richer
   metadata (model, tools, arguments).
2. **Worker prompts** (`src/hpc_agent/_kernel/extension/worker_prompts/<workflow>.md`) —
   the four host workflows (`submit`, `status`, `aggregate`, `campaign`)
   delegated workers consume. A `claude -p --bare` worker has no
   `Skill` tool, so `_kernel/extension/spawn_prompt.py` inlines the
   prompt body verbatim into `cacheable_prefix` (loaded via
   `importlib.resources`). Snapshot tests pin the rendered bytes so
   prompt-cache hit rates don't silently regress.
3. **Slash commands** (`src/slash_commands/commands/<stem>.md`) —
   user-typed entry points. Two routing modes coexist:
   - **Paired** (`hpc-axes-init`, `classify-axis-hpc`) — 5-line "use
     the X skill" redirect. Single SoT lives in the paired skill.
   - **Workflow trigger** (`submit-hpc`, `monitor-hpc`,
     `aggregate-hpc`, `campaign-hpc`) — routes through
     `hpc-agent run <workflow>` to the spawn pipeline, which loads
     the body from `worker_prompts/<workflow>.md`. No paired skill —
     the workflow IS the worker prompt.

Two lint scripts pin the surfaces against each other:
`scripts/lint_skill_command_sync.py:WORKFLOW_PAIRS` enumerates the
skill↔slash redirects; `WORKFLOW_TRIGGER_SLASHES` (same file)
enumerates the `hpc-agent run` triggers. CI fails if a new workflow
shows up on one surface without the other. Skill-policy rationale
lives in `docs/internals/skill-policy.md`.

## Cross-subject composition

Each top-level directory under `src/hpc_agent/ops/` and
`src/hpc_agent/meta/` is a *subject* — a self-contained vertical
slice. Subjects MUST NOT reach sideways into each other's internals.
This is enforced by `scripts/lint_subject_imports.py`, which
AST-scans every file under `ops/<subject>/` and `meta/<subject>/` and
rejects any `from hpc_agent.<role>.<other_subject>...` import.

Allowed cross-cutting roots (these are substrate, not subjects):

- `hpc_agent.infra.*`
- `hpc_agent.state.*`

When two subjects genuinely need to share code, three patterns apply,
in order of preference:

1. **Helper-shaped shared code → `infra/`.** A parser, transport
   helper, or planning function used by more than one subject lives
   under `hpc_agent.infra.<name>`. PR #90 moved the throughput planner
   + remote backend factory there; PR #96 did the same for
   `cluster_status.py` and `cluster_logs.py`. The subject-imports lint
   permits `from hpc_agent.infra.* import …` from any subject.

2. **Declarative composition → `composes=` with string names.**
   A composite that just *names* a primitive from another subject in
   its `@primitive(composes=[...])` metadata doesn't import the target
   callable — string names resolve against the live registry. Pure
   metadata, lint-clean, also drives the agent-readable workflow graph
   in the operations catalog. Cross-package composition works the same
   way: a plugin primitive can compose a core primitive by wire name
   (see `hpc-agent-pro/src/hpc_agent_pro/smart_resubmit_flow.py`).

3. **Callable cross-subject calls → workflow at role root, OR
   `hpc_agent.runner`.** Two cases:

   - **Workflow needs to call atoms from multiple subjects** — keep
     the workflow file at the `ops/` or `meta/` *role root*
     (`ops/aggregate_flow.py`, `meta/validate_campaign.py`). The
     subject-imports lint short-circuits to `None` for files directly
     under the role root (`len(parts) < 2`), so the workflow can
     `from hpc_agent.ops.<other_subject>.<atom> import …` directly.
     This is the dominant pattern post-P5a; all six host workflows
     use it.

   - **An atom inside one subject needs to call a primitive in
     another** — route the call through `hpc_agent.runner`, the
     package-root bridge. `runner.py` lives outside every subject so
     the lint permits the import. `scripts/lint_runner_shim.py` gates
     what crosses: only `@primitive`-decorated symbols plus a small
     explicit allow-list of legacy back-compat helpers
     (`DEFAULT_AUTO_RETRY_POLICY`, `cluster_failures_by_fingerprint`,
     `build_job_env` — each carries a rationale in the lint).

   Conceptually `hpc_agent.runner` mirrors what `composes=` does at
   the metadata layer — `composes=["combine-wave"]` is the
   declarative form, `from hpc_agent.runner import combine_wave` is
   the callable form.

The rationale for keeping this strict (vs. a permissive allow-list,
which is what the codebase had through PR #97): allow-listed
exceptions accrete; principled extraction to `infra/` keeps the
architecture honest. PR #98 eradicated the allow-list; PR #108 added
lazy resolution so string-name `composes=` is order-agnostic; P5a
pulled workflows up to the role root so most cross-subject seams
disappeared entirely.

### Non-goals

- **Don't propose collapsing cross-subject composition into
  per-subject inlining.** If two subjects share a helper, that's a
  candidate for `infra/`, not duplication. If a workflow names atoms
  from multiple subjects, that's the workflow doing its job, not a
  violation.

- **Don't re-introduce a permissive `PER_FILE_ALLOWED_IMPORTS`
  allow-list.** Cross-subject reach is either `infra/` (helper),
  `composes=` (metadata), workflow-at-role-root (workflow), or
  `hpc_agent.runner` (atom-to-atom primitive call). Anything else is
  a smell.

- **Don't move workflow files back into subject dirs.** P5a moved
  them to the role root deliberately so workflow→atom cross-subject
  calls become trivial direct imports.

## When in doubt

- **Adding a primitive?** → `docs/internals/adding-a-primitive.md`
- **Adding a cluster?** → `infra/clusters.py:CLUSTER_YAML_KEYS` lists
  every supported key; add an entry + a getter validator if the new
  key needs schema-checking.
- **Adding a backend?** → `infra/backends/` has one module per
  scheduler; the registry pattern is `get_backend_class(scheduler)`.
- **Splitting a file?** → `state/` is the pattern: package
  with `__init__.py` re-exporting + per-concern submodules. The same
  applies inside any subject (`ops/monitor/`, `ops/aggregate/`, etc.).
- **Naming a config knob?** → `HPC_*` env-vars listed in
  `docs/reference/env-vars.md`; per-cluster YAML keys in
  `clusters.yaml`. Default to the latter; use env-vars only for
  things that legitimately vary per-shell (timeouts, sandbox
  redirects).
- **Two subjects need the same helper?** → extract to `infra/`. See
  "Cross-subject composition" above.
- **A workflow needs to call a primitive in another subject?** →
  import via `hpc_agent.runner`, and declare the link in
  `composes=[...]` so it shows up in the operations catalog.
