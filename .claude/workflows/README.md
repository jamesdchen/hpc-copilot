# Saved workflows

Scripts here run on the Claude Code Workflow engine (`Workflow` tool /
`/workflows`). That engine is one vendor's orchestrator; we expect
declarative step/DAG formats from other tools (e.g. awman's `workflow.toml`)
to converge on the same shape, so every script keeps a hard seam between
what any orchestrator needs and what this one's API looks like.

## Delegation boundary (what a workflow may do at all)

Plans operate at two scopes of `docs/design/agent-delegation.md`:

- **Recon scope (rule 1)** — read-only fan-out, the context firewall:
  every command a `query`/`validate` verb (`campaign-recon.js`).
- **Plan-relay scope (rule 5)** — a `kind: 'script'` step may relay ANY
  registry verb except the rendezvous lock, `block-drive` ticks included,
  because the command is an authored template and the model composes
  nothing (`campaign-run.js`). Gates PARK, never pass: a tick that returns
  `awaiting_decision` ends the run with the brief, the human journals the
  `y` inline, and the workflow resumes via `resumeFromRunId` from cache.

Two lines hold at every scope, mechanized by
`tests/contracts/test_workflow_plan_delegation.py` against the live
registry: `append-decision` appears in NO plan, in no section; and any
mutating/workflow verb appears ONLY inside a plan's `COMMANDS` block —
model-facing `PROMPTS` text stays at recon scope. Render-bearing output
travels as pointers + counts, never a paraphrase.

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
     as step ids, optionally scoped with an `@qualifier` suffix),
     `isolation` (`shared-checkout` | `fresh-worktree`),
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
   entry (see `runStep()` in `campaign-recon.js`) so the declared plan cannot
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
| `run: fan-out`              | matrix/map over items (probes)                    |
| `run: conditional`          | conditional step / skip-if-empty                  |
| `isolation: fresh-worktree` | per-step container or worktree checkout           |
| `effort` (and model choice) | per-step agent/model assignment                   |
| `abort_on_failure`          | abort/all-stop failure policy                     |
| `retry`                     | bounded `on_failure` remediation (`max_attempts`) |
| `PROMPTS.<step>`            | the step's agent instruction (template + inputs)  |
| `SCHEMAS.<key>`             | the step's structured-output/validation schema    |
| `ARGS_CONTRACT`             | workflow inputs/parameters                        |

Engine-specific semantics to re-check when porting, not assume: barrier
behavior of `needs` under fan-out, failed-instance handling (here: a dead
probe agent is re-dispatched once per its `retry` count, then reported by
name — a recon plan never aborts, a red probe is data), and how the
target's sandbox model scopes what a step may execute (here: the
query/validate-only command rule above).

## Lineage notes

- **2026-07-28 — section repointed at the researcher lifecycle.** The
  original flagship (`swarm-units.js`, a build swarm over handoff packages)
  was deleted by user direction: this section now serves *running
  experiments*, not building the repo. The swarm-dispatch protocol it
  mechanized returns to its durable form — `docs/plans/` handoff packages
  (template: `docs/plans/_TEMPLATE-handoff/`) implemented per plan prose,
  with `scripts/check_handoff_disjointness.py` (which outlives the deleted
  script; its fire paths stay pinned in
  `tests/scripts/test_check_handoff_disjointness.py`) guarding file claims.
  `campaign-recon.js` lands as the flagship at the recon-only delegation
  level; its adapter (`validatePlan`/`runStep`/`runWithRetry`, the
  `scriptStep` relay prompt, the step vocabulary) is carried over from
  `swarm-units.js` verbatim — the plan data changed, the wheel did not.
- **2026-07-28 — awman v0.11.0 study.** `kind: script` steps, load-time plan
  validation, `abort_on_failure`/`retry` failure policy, per-step `effort`,
  and the `pushPolicy` gate were adopted after studying awman's workflow
  format (`docs/05-workflows.md`, `13-dynamic-workflows.md`,
  `15-parallel-workflows.md` at v0.11.0), which mechanizes the same shapes
  as typed setup/teardown steps, parse-time strict validation, bounded
  `on_failure` blocks, and per-step agent/model fields.
- **Same pass — duplicated mechanism re-pointed.** The build swarm's loader
  prompt originally re-derived file-claim checks by eyeball; those are
  exactly what `scripts/check_handoff_disjointness.py` mechanizes, so the
  plan ran the checker as a `script` step and forbade the loader from
  re-deriving it. The rule generalizes to every plan here: an LLM relays a
  mechanized check, it never re-derives one.
