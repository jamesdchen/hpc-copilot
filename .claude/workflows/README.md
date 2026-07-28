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
     `kind` (`agent` | `script` — script steps run one rule-fixed command
     and relay its result; the determinism-boundary principle applied to
     orchestration: an LLM relays mechanized checks, it never re-derives
     them), `run` (`once` | `fan-out` | `conditional`), `needs` (DAG edges
     as step ids, optionally scoped with an `@qualifier` suffix such as
     `@previous-wave`), `isolation` (`shared-checkout` | `fresh-worktree`),
     `prompt`/`command` (the PROMPTS/COMMANDS key it dispatches), `output`
     (a `SCHEMAS` key, or `null` for free text), and optional `effort`
     (reasoning-effort tier), `abort_on_failure` (a failed instance aborts
     the run instead of log-and-continue), `retry` (re-dispatches for a
     dead instance).
   - `PROMPTS` / `COMMANDS`: one pure `(namedInputs) => string` template
     per step — prompts never inline engine API calls or engine state.
2. **RUNTIME ADAPTER** — the only section that may call the engine API
   (`agent()`, `parallel()`, `pipeline()`, `phase()`, `log()`, `budget`,
   `workflow()`). It walks the plan: control flow implements `STEPS[].needs`,
   and each dispatch takes its kind/schema/isolation/effort from its `STEPS`
   entry (see `runStep()` in `swarm-units.js`) so the declared plan cannot
   drift from what runs. The adapter also validates the plan before the
   first agent is dispatched (`validatePlan()`: unknown fields, unknown
   step/schema/prompt refs, bad enum values, DAG cycles) — the load-time
   strictness declarative engines get from their parsers, so a typo cannot
   silently take effect mid-run.

## Porting to another orchestrator

Translate the plan section only:

| Plan element                | Declarative-DAG equivalent (e.g. workflow.toml)   |
| --------------------------- | ------------------------------------------------- |
| `STEPS[].id` / `needs`      | step + its dependency edges                       |
| `kind: script` + `COMMANDS` | deterministic step type (`run_script`/`run_shell`)|
| `run: fan-out`              | matrix/map over items (units)                     |
| `run: conditional`          | conditional step / skip-if-empty                  |
| `isolation: fresh-worktree` | per-step container or worktree checkout           |
| `effort` (and model choice) | per-step agent/model assignment                   |
| `abort_on_failure`          | abort/all-stop failure policy                     |
| `retry`                     | bounded `on_failure` remediation (`max_attempts`) |
| `PROMPTS.<step>`            | the step's agent instruction (template + inputs)  |
| `SCHEMAS.<key>`             | the step's structured-output/validation schema    |
| `ARGS_CONTRACT`             | workflow inputs/parameters                        |

Engine-specific semantics to re-check when porting, not assume: barrier
behavior of `needs` under fan-out (here: a wave's integrate step waits for
that wave's builds only), failed-instance handling (here: a dead build agent
is re-dispatched once per its `retry` count, then skipped and logged, while
an `abort_on_failure` step ends the whole run), and where prompts'
git/push permissions land in the target's sandbox model (here: the
`pushPolicy: 'hold'` arg keeps every push on the machine for human
inspection).

Durable inputs (the unit-specs.json + architect memo of a handoff package)
already live outside the scripts — docs/plans/ is the portable artifact
layer; scripts only consume it.

## Lineage notes

- **2026-07-28 — awman v0.11.0 study.** `kind: script` steps, load-time plan
  validation, `abort_on_failure`/`retry` failure policy, per-step `effort`,
  and the `pushPolicy` gate were adopted after studying awman's workflow
  format (`docs/05-workflows.md`, `13-dynamic-workflows.md`,
  `15-parallel-workflows.md` at v0.11.0), which mechanizes the same shapes
  as typed setup/teardown steps, parse-time strict validation, bounded
  `on_failure` blocks, and per-step agent/model fields.
- **Same pass — duplicated mechanism re-pointed.** The `load-specs` prompt
  originally asked the loader agent to re-derive same-wave overlap /
  claim-typo / dirty-worktree checks by eyeball; those are exactly what
  `scripts/check_handoff_disjointness.py` mechanizes (with tested fire
  paths), so the plan now runs the checker as a `script` step
  (`check-disjointness`) and the loader is forbidden from re-deriving it.
  Unit briefs are likewise no longer round-tripped through the loader's
  token stream — build units read their own spec entry from the worktree
  copy of unit-specs.json, byte-exact.
