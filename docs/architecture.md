# Architecture

claude-hpc is a primitive-based HPC orchestration framework. The package
is organised as a layered DAG: each layer depends on lower layers but
not the other way round. New code finds its destination by asking
"what layer am I writing in?" and following the layering rules.

## Layering DAG

```
┌─────────────────────────────────────────────────────────────────────┐
│  Surfaces (what the user / agent calls into)                        │
│                                                                     │
│  src/slash_commands/commands/   skills/                             │
│  user-typed entry points         agent-callable workflows           │
│  (thin redirects to skills)      (one SKILL.md per workflow)        │
│                                  ↓                                  │
│  agent_cli.py (`hpc-agent` console script)                          │
│  argparse + verb groups (forecast/validate/build) + flat aliases    │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Workflows (multi-atom orchestration, registered as @primitive      │
│  with verb='workflow')                                              │
│                                                                     │
│  flows/                                                             │
│    ├ submit_flow.py        ─┐                                       │
│    ├ monitor_flow.py        │  user-facing pipelines                │
│    ├ aggregate_flow.py      │  (rsync + qsub + record / poll /      │
│    ├ resubmit_flow.py       │  combine + classify)                  │
│    └ validate_campaign.py  ─┘                                       │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Atoms (single-concern @primitives — query / validate / mutate /    │
│  submit / scaffold)                                                 │
│                                                                     │
│  atoms/         build-executor, build-tasks-py, recall, recommend-  │
│                 partition, validate-*, walltime-arbitrage, ...      │
│  runner/        SSH-bound mutate primitives: submit-spec,           │
│                 record-status, combine-wave, mark-terminal,         │
│                 update-run-constraints                              │
│  mapreduce/     reduce-side: status, classify, history, rollup,     │
│  └ reduce/      tui (rich-based per-task summary)                   │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌──────────────────────────────────────┬──────────────────────────────┐
│  Forecast (predict)                  │  Planning (decide)           │
│                                      │                              │
│  forecast/                           │  planning/                   │
│    ├ queue_wait_baseline.py          │    ├ planner.py              │
│    ├ predict_start.py                │    ├ resubmit_planner.py     │
│    ├ queue_simulator.py              │    ├ throughput.py           │
│    ├ backfill.py + calibration       │    ├ daisy_chain.py          │
│    └ residual_lifetime.py            │    └ stages.py               │
│                                      │                              │
│  Pure functions over inputs:         │  Pure functions over inputs: │
│  squeue text + sshare text +         │  forecast outputs + cluster  │
│  history → predicted ETA             │  config → ranked candidates  │
└──────────────────────────────────────┴──────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  Infra (transport + cluster introspection)                          │
│                                                                     │
│  infra/                                                             │
│    ├ remote.py     ssh / scp / rsync wrappers + multiplexing        │
│    ├ backends/     per-scheduler dispatchers (sge, slurm)           │
│    ├ inspect/      qstat / scontrol / sacct parsers + snapshots    │
│    ├ clusters.py   clusters.yaml loader + per-cluster validators   │
│    └ gpu.py        GPU queue selection + live qstat scoring         │
└──────────────────────────────────┬──────────────────────────────────┘
                                   ↓
┌─────────────────────────────────────────────────────────────────────┐
│  State (persisted artifacts) + _internal (framework primitives)     │
│                                                                     │
│  state/            _internal/                                       │
│    ├ runs.py         ├ session/  per-run journal (3-module pkg)     │
│    ├ runtime_prior   ├ primitive.py  @primitive decorator           │
│    ├ discover.py     ├ schema.py     spec validation                │
│    └ user_profiles   ├ io.py         atomic-locked-update + flock   │
│                      ├ lifecycle.py  StrEnum: TaskStatus etc.       │
│                      ├ telemetry.py  monitor.jsonl writer           │
│                      ├ time.py       canonical UTC helpers          │
│                      └ version.py    cross-domain schema manifest   │
└─────────────────────────────────────────────────────────────────────┘
```

Cross-cutting:

- **`_schema_models/`** — Pydantic v2 BaseModels grouped by domain
  (`workflows/`, `validators/`, `fixtures/`, `queries/`, `actions/`).
  The *authoring* SoT for every wire shape.
- **`schemas/`** — JSON Schemas, regenerated from `_schema_models/` by
  `scripts/build_schemas.py`. The *wire* SoT every external consumer
  reads.
- **`docs/primitives/`** — one `.md` per `@primitive`. Frontmatter
  auto-generated from the registry; bodies hand-written. The
  agent-context surface (`hpc-agent capabilities --full` projects them).

## The predict / decide / act boundary

The single most important invariant: **forecast is pure prediction;
planning is pure decision; runner is the only layer that mutates the
cluster.**

| Layer    | Reads                                  | Writes                  | Side effects |
|----------|----------------------------------------|-------------------------|--------------|
| forecast | squeue/sshare text, history snapshots  | nothing                 | none         |
| planning | forecast output, cluster config        | nothing                 | none         |
| atoms    | depends on the atom's verb             | sidecars / journal      | scoped       |
| flows    | composes atoms                         | journal + cluster state | SSH / qsub   |
| runner   | run_id + cluster                       | cluster state           | SSH / qsub   |

If you find yourself adding a `subprocess.run("ssh ...")` in
`forecast/` or `planning/`, stop. The convention is: the slash command
runs the SSH; the framework primitive parses the text. This keeps
forecasts replayable and unit-testable, and keeps audits of "what does
this primitive touch?" trivial.

## The @primitive registry

Every wire-callable operation is decorated:

```python
@primitive(
    name="best-submit-window",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    cli="hpc-agent best-submit-window --profile <p> --cluster <c>",
    agent_facing=True,
)
def best_submit_window(...): ...
```

The decorator:

- Registers the function in a process-wide registry (`get_registry()`)
- Drives `agent_cli.py`'s argparse subcommand naming (the same string
  is also the CLI verb)
- Drives `docs/primitives/<name>.md`'s frontmatter (regenerated by
  `scripts/build_primitive_frontmatter.py`)
- Drives `docs/generated/operations.md` (regenerated by
  `build_operations_index.py`)
- Drives the JSON schema filename (`schemas/<name>.input.json` /
  `<name>.output.json`)

To add a primitive, follow the recipe in
[`internals/adding-a-primitive.md`](internals/adding-a-primitive.md).
The mechanical pieces are all generated; you write the function body
+ the doc body.

## Two source-of-truth chains

The framework keeps a strict 2-step SoT chain so wire consumers and
human / LLM consumers stay in lockstep:

1. **Wire shapes**: Pydantic model in `_schema_models/<domain>/<name>.py`
   → JSON Schema in `schemas/<name>.input.json`, regenerated by
   `build_schemas.py`. CI gates on `--check`.

2. **Operation catalog**: `@primitive` decorator → frontmatter in
   `docs/primitives/<name>.md`, regenerated by
   `build_primitive_frontmatter.py`. CI gates on `--check`. The
   bodies of those docs are hand-written.

Editing a Pydantic model without re-running `build_schemas.py --write`
fails CI. Editing a `@primitive(...)` decorator without re-running
`build_primitive_frontmatter.py --write` fails CI.

## CLI surface

`hpc-agent` (entry point: `claude_hpc.agent_cli:main`) exposes every
primitive as a subcommand. Subcommands can be invoked flat
(`hpc-agent predict-queue-wait ...`) or under a verb group
(`hpc-agent forecast predict-queue-wait ...`); the verb groups
(`forecast`, `validate`, `build`, plus the existing `clusters` /
`campaign`) are argv pre-processors so flat-form invocations always
keep working.

The agent-facing JSON envelope is uniform: `{"ok": bool, "data": {...}}`
on success, `{"ok": false, "error_code": str, "category": str,
"retry_safe": bool, ...}` on failure. Documented at
[`reference/cli-spec.md`](reference/cli-spec.md).

## Agent surfaces

Two:

1. **Skills** (`skills/<id>/SKILL.md`) — agent-canonical workflows
   invoked by Claude Code's `Skill` tool. Have richer metadata
   (model, tools, arguments).
2. **Slash commands** (`src/slash_commands/commands/<stem>.md`) —
   user-typed entry points. As of the audit refactor, these are thin
   redirects to the matching skill: a 5-line "use the X skill" body.
   Single SoT for workflow content lives in the skill.

The pair table in `scripts/lint_skill_command_sync.py:WORKFLOW_PAIRS`
pins which skill matches which slash command; CI fails if either
surface gains a workflow without the other.

## When in doubt

- **Adding a primitive?** → `docs/internals/adding-a-primitive.md`
- **Adding a cluster?** → `infra/clusters.py:CLUSTER_YAML_KEYS` lists
  every supported key; add an entry + a getter validator if the new
  key needs schema-checking.
- **Adding a backend?** → `infra/backends/` has one module per
  scheduler; the registry pattern is `get_backend_class(scheduler)`.
- **Splitting a file?** → `_internal/session/` is the pattern: package
  with `__init__.py` re-exporting + per-concern submodules.
- **Naming a config knob?** → `HPC_*` env-vars listed in
  `docs/reference/env-vars.md`; per-cluster YAML keys in
  `clusters.yaml`. Default to the latter; use env-vars only for
  things that legitimately vary per-shell (timeouts, sandbox
  redirects).
