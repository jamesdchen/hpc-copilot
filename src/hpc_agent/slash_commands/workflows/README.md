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

## Intake (frontload the args, warm)

Every plan declares its full input surface up front as `ARGS_CONTRACT` — a
plain object naming each arg, whether it is required, and what it means.
The launch discipline built on that:

- **Resolve every field BEFORE launching.** A plan throws on missing
  required args at t=0; it never discovers mid-run that it needed
  something from the human. The only mid-run returns are PARKS (gates,
  failures) — never questions.
- **Warm, not cold.** The launching session does not interrogate the user
  field-by-field from zero. It first fills every slot it can itself —
  cheap local reads, a `campaign-recon` sweep, the previous run's args —
  and then presents the WHOLE proposed arg set to the user in ONE
  exchange, as diffs to nudge ("driving run-014 with workflow=submit,
  maxTicks=25 — correct anything") rather than open questions to answer.
  The user's correction is a diff against a concrete proposal, which is
  cheap; a cold "what run id?" is an interruption, which is not.
- **One exchange, then run.** If the user's nudge changes a value, fold it
  in and launch; do not re-propose unless a correction invalidates other
  proposed fields.
- **A park is a question, not a stop (auto-resume).** When a drive parks at
  a gate and the human journals the `y`, relaunch the workflow with
  `resumeFromRunId` in the same breath — no "shall I continue?", no waiting
  for a nudge. Cached steps replay free; the next tick consumes the fresh
  greenlight. The human's part of the exchange is the decision, never the
  restart.
- **Upfront the y's when the human wants to walk away.** Unattended driving
  (overnight, long lunches) does NOT mean the workflow passes gates — it
  means the intake exchange also offers the OVERNIGHT STANDING CONSENT the
  kernel already consumes (`ops/overnight.py`; `block-drive` auto-advances
  a consented boundary in code): the human types ONE consent utterance
  inline — scope, hard caps (`expires_at` + budget/walltime), bound to the
  current spec identity, with a harness-tracked `status-watch` armed as the
  wake — journaled via `append-decision` under the overnight block, in the
  main session, like every y. `campaign-run` then runs gate-free through
  consented boundaries and parks only on out-of-scope, expired, or
  spec-changed ones; every auto-advance lands in the consumption ledger and
  the morning brief. The workflow itself never touches the consent — the
  upfronting is an INTAKE step, and the consuming is the KERNEL's.

This is the fix for the prior iteration's failure mode: the old build-swarm
onboarding spent its opening turns coldly deriving what it needed instead
of proposing warm defaults and letting the human steer by exception.

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

- **2026-07-28 — section repointed at the researcher lifecycle; the build
  protocol fully erased.** The original flagship (`swarm-units.js`, a build
  swarm over handoff packages) was deleted by user direction: this section
  serves *running experiments*, not building the repo. In a second order
  the same day the whole bespoke build protocol went with it —
  `docs/plans/_TEMPLATE-handoff/`, `scripts/check_handoff_disjointness.py`,
  and its tests — because the dev loop needs no protocol of its own:
  Claude Code natively runs dynamic workflows, and this repo's devx layer
  exists to AUGMENT that experience (lints, regen, contract tests, the
  `tag-session` ingestion seam), never to replace its orchestration.
  Historical handoff packages under `docs/plans/` remain as records.
  `campaign-recon.js` landed as the flagship at the recon-only delegation
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
