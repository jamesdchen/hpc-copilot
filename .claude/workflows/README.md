# Saved workflows

Scripts here run on the Claude Code Workflow engine (`Workflow` tool /
`/workflows`). That engine is one vendor's orchestrator; we expect
declarative step/DAG formats from other tools (e.g. awman's `workflow.toml`)
to converge on the same shape, so every script keeps a hard seam between
what any orchestrator needs and what this one's API looks like.

## Structure contract (portability seam)

Each script is two sections, in order:

1. **PORTABLE PLAN** — engine-neutral, and the only section a port
   translates:
   - `ARGS_CONTRACT`: named inputs and their meaning.
   - `SCHEMAS`: structured-output contracts, plain JSON Schema.
   - `STEPS`: the step graph. Per step: `id`, `phase` (display grouping),
     `run` (`once` | `fan-out` | `conditional`), `needs` (DAG edges, i.e.
     barriers), `isolation` (`shared-checkout` | `fresh-worktree`),
     `output` (a `SCHEMAS` key, or `null` for free text).
   - `PROMPTS`: one pure `(namedInputs) => string` template per step —
     prompts never inline engine API calls or engine state.
2. **RUNTIME ADAPTER** — the only section that may call the engine API
   (`agent()`, `parallel()`, `pipeline()`, `phase()`, `log()`, `budget`,
   `workflow()`). It walks the plan: control flow implements `STEPS[].needs`,
   and each dispatch takes its schema/isolation/phase from its `STEPS` entry
   (see `runStep()` in `swarm-units.js`) so the declared plan cannot drift
   from what runs.

## Porting to another orchestrator

Translate the plan section only:

| Plan element                | Declarative-DAG equivalent (e.g. workflow.toml)   |
| --------------------------- | ------------------------------------------------- |
| `STEPS[].id` / `needs`      | step + its dependency edges                       |
| `run: fan-out`              | matrix/map over items (units)                     |
| `run: conditional`          | conditional step / skip-if-empty                  |
| `isolation: fresh-worktree` | per-step container or worktree checkout           |
| `PROMPTS.<step>`            | the step's agent instruction (template + inputs)  |
| `SCHEMAS.<key>`             | the step's structured-output/validation schema    |
| `ARGS_CONTRACT`             | workflow inputs/parameters                        |

Engine-specific semantics to re-check when porting, not assume: barrier
behavior of `needs` under fan-out (here: a wave's integrate step waits for
that wave's builds only), failed-instance handling (here: a dead build agent
yields `null` and is skipped, logged by the adapter), and where prompts'
git/push permissions land in the target's sandbox model.

Durable inputs (the unit-specs.json + architect memo of a handoff package)
already live outside the scripts — docs/plans/ is the portable artifact
layer; scripts only consume it.
