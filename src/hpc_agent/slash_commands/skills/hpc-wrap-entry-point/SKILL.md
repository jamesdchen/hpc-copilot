---
name: hpc-wrap-entry-point
description: "Onboard a repo for hpc-agent submission, autonomously. The deterministic head is ONE call to `wrap-entry-point-auto` (detect → pathway table → decorate → frozen-YAML scan → fixed-params partition, in code); the skill supplies the caller-owned intent (`goal` + `task_generator`), resolves whichever of the three named escalations comes back — `needs_pick` (an entry-point tie), `needs_intent` (a human-owned field), `needs_wrapper_argv` (an argv template for a non-introspectable CLI surface) — and then invokes the `interview` primitive to persist `tasks.py` + `interview.json`. No `[Y/n]` prompts; every deterministic choice point is resolved by the verb, and every genuine judgment point is escalated by name. Human-driven callers (`/submit-hpc`'s interview phase) gather intent from the user *first* and pass a fully-resolved spec; the skill records what it was given."
allowed-tools: Bash Read Write Glob
execution: inline
category: agent-autonomous
---

Agent-facing composition over two primitives: **[wrap-entry-point-auto](../../../../docs/primitives/wrap-entry-point-auto.md)** (the deterministic head — one call) and the **[interview](../../../../docs/primitives/interview.md) primitive** (the persist). Autonomous mode lets the verb fill everything a repo scan can decide; the slash consumer (`/submit-hpc`'s interview phase) passes a fully-resolved spec and the skill just records.

The skill persists, in either pathway:
- A `tasks.py` (from the supplied `task_generator`) whose kwargs include `<stem>_sha` for every frozen YAML, so `cmd_sha` distinguishes `exp_42.yaml` from `exp_43.yaml` and catches in-place edits.
- An `interview.json` recording the entry-point shape (`register_run` pointer for the decorate pathway; `shell_command` block with the wrapper for the wrapper pathway).
- **Only in the wrapper pathway**: a `@register_run` **wrapper** at `<experiment>/.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the user's entry point with kwargs substituted. Downstream primitives (`classify-axis`, `validate-executor-signatures`) introspect the wrapper's typed signature; the underlying entry point stays untouched.

**"Where am I / what's next" is a query, not a recollection.** `hpc-agent suggest-prelude-action --experiment-dir <dir>` answers it mechanically off the five durable prelude substrates (notebook journal, audit-config seat, pack journal + opt-in integrity, `axes.yaml`, `interview.json`) and returns `{rung, action, why, scaffold}`. Run it when you are unsure whether this skill is even the next step — rung 5 (`audit-handoff`) and rung 6 (`interview`) are the two that route here. Contract: `docs/primitives/suggest-prelude-action.md`.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" means **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** — the harness runs them concurrently. NOT shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — that trips the permission classifier as a compound command.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Return via the emit-skill-return file primitive — never via chat.** The Skill tool result is no longer the return mechanism; the parent (`hpc-submit`, `hpc-campaign`, …) reads your return envelope from `<experiment_dir>/.hpc/_returns/hpc-wrap-entry-point.json`. The final step of this skill (Step 6 below) writes that envelope and invokes `hpc-agent emit-skill-return` as the LAST tool call — no closing chat message of any kind. A non-tool-call closing message fires the harness's end-of-turn signal, the parent never resumes, and the user has to type "keep going". The schema for the envelope lives at `hpc_agent/schemas/skill_returns/hpc-wrap-entry-point.json` and is enforced by the emit verb.

## Inputs

Caller-supplied (the skill refuses with `spec_invalid` if these are absent; the verb escalates them as `needs_intent`):

| Field | Why the caller has to supply it |
|---|---|
| `goal` | One-line free-text intent — the skill cannot invent it. |
| `task_generator` | The shape of the scale-up axis (`items_x_seeds`, `cartesian_product`, `enumerated`, `numeric_linspace`/`logspace`) plus its params. Cannot be inferred from the repo. |

Everything else is an OVERRIDE of something `wrap-entry-point-auto` decides — pass it through in that verb's spec, never re-derive it here. The authoritative field list is `hpc_agent/schemas/wrap_entry_point_auto.input.json`: `entry_point_path`, `run_name`, `entry_point_kind`, `argv`, `signature`, `goal`, `task_generator`, `task_count`, `frozen_configs`, `fixed_params`. A bare call (no `--spec`) is valid: it runs to the first genuine judgment point and names it.

Two fields the verb deliberately does NOT own, and that are therefore NOT in its spec:

- `produced_by` — the `interview` primitive's own composer stamps `produced_by.operator` from `git config user.name` and discloses it in `interview.json._materialized.composed_defaults`. Pass `{"kind": "human"}` and let it compose; a caller-supplied `operator` is left untouched.
- `data_axis_hint` — `classify-axis-auto`'s seat (Step 5).

## When to run

- The user's repo has any non-notebook entry point — `main.py`, `train.py`, `run_experiment.py`, `python -m pkg.cli`, `./simulator`, etc. — and no `@register_run` decoration anywhere.
- **The repo is greenfield** — no entry point yet — and the caller wants a seed scaffold.
- A fresh `/submit-hpc` escalated with `mature_repo_needs_interview`.

## Steps

### 1. Assemble the caller-owned intent

`goal` and `task_generator` come from the caller or the human; nothing else in this skill needs gathering up front. The entry point handles *one task* — the `task_generator` enumerates the **N tasks** to fan out. Common shapes:

| Shape | When to use | Params |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied list of N task dicts | `items=[{...}, {...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

The skill does **not** invent a `task_generator` — refuse with `spec_invalid` if absent and the human is unreachable. (The slash command elicits this from the user; MARs supplies it explicitly.)

**Elicit `goal` and `task_generator` as FREE-TEXT the human TYPES — never a pre-filled option they click.** These two are the `REQUIRED_CALLER_FIELDS` (`ops/submit/field_partition.py`), and the downstream `_assert_human_authorship` gate verifies every value token against the human's own utterance log; a click on a value YOU pre-filled (an `AskUserQuestion` option whose text you wrote) carries no authorship, so it is refused at `append-decision` and forces a re-type — the awkward loop where the tool asks a multiple-choice question and then rejects the answer. Ask an OPEN question ("How many seeds, and what `n_samples` per task?") and let the human type "20 seeds, n_samples=1000000"; counts and ranges are fine ("20 seeds", "0 through 19"). Reserve buttons for fields enumerated from GROUND TRUTH (cluster from `clusters.yaml`, `data_axis` kind, entry-point shape) — never for the sweep magnitudes the gate locks. You MAY echo your PARSE back for a yes/no confirm ("reading that as 20 tasks, seeds 0–19, 1e6 samples each — right?"): the value still originated from the human's typing, so it passes.

**Also elicit the work's `scopes` (scope tags) as FREE-TEXT the human types** — their own words for what this experiment tests (e.g. `edge-x`, `rv-data`), recorded on the interview so the run's evidence-memory index keys on them. An empty answer is recorded as **no tags** and disclosed downstream (lineage-keyed priors still find the work by code identity) — never invent a tag to fill the gap (an agent-invented tag is index poisoning). Tags are identity, never interpreted; core never reads what a tag means.

**Fixed enumeration vs. adaptive sweep.** The shapes above enumerate a *fixed* task set up front. If the sweep is **adaptive** — each batch's hyperparameters depend on prior results (Bayesian optimization / Optuna ask-tell, PBT, Hyperband) — it is NOT a `task_generator`: route to **`hpc-campaign`** and materialize the strategy with **`hpc-agent scaffold-strategy --name {optuna,pbt}`**. The framework drives the submit→monitor→aggregate→decide loop and owns the ask/tell contract (see the hpc-campaign strategy-authoring contract). Do NOT hand-roll a campaign controller or reverse-engineer the strategy from source.

### 2. Call `wrap-entry-point-auto` ONCE

Write the spec (every field optional; include the intent from Step 1 plus any caller overrides) and invoke:

```bash
hpc-agent wrap-entry-point-auto --spec /tmp/wrap_spec.json --experiment-dir <experiment_dir>
```

This ONE call is the whole deterministic head — entry-point detection, the entry-point/entry-function ladders, the pathway decision table, `decorate-entry-point`, the frozen-YAML convention scan, and the fixed-params partition. Do **not** hand-walk those; do not re-run `detect-entry-point` to "check" them. The contract (ladders, rule ids, which globs are scanned, what counts as covered) lives in `docs/primitives/wrap-entry-point-auto.md` — read it there rather than from prose here.

Two properties worth holding onto while you branch:

- **Every escalation leaves the repo byte-identical.** Decoration is the last step, after all three escalation branches are ruled out.
- **Idempotent** on `experiment_dir`. A second call finds the decoration on disk and writes nothing.

Branch on the discriminated return: `onboarded` → Step 4; `needs_pick` / `needs_intent` / `needs_wrapper_argv` → Step 3; `spec_invalid` → Step 3e.

### 3. Resolve the escalation, then re-call

Each escalation names the exact field it needs and why code must not produce it (`ask`). Resolve it, add the field to the spec, and re-invoke `hpc-agent wrap-entry-point-auto` — the verb is the only writer, so resolution is always "supply the field and re-call", never "do the step by hand".

**3a. `needs_pick`** — an entry-point-FILE tie (`reason: entry_point_tie`) or an entry-FUNCTION tie (`entry_function_tie`). `candidates` lists every tied candidate and `resolve_with` names the field that breaks it (`entry_point_path` / `run_name`).

- If the caller's instruction already names one explicitly, apply it.
- Otherwise **relay the candidates to the human** and let them pick. Do not silently choose across `main.py` / `train.py` / `run.py`: a wrong pick is not recoverable without the user noticing.

**3b. `needs_intent`** — `goal` / `task_generator` / `task_count`, or a specific uncovered required param of the entry point's signature (`entry_point.fixed_params.<param>`). `never_invented` pins the subset code must never fabricate; `partition` carries the computed param classes when the escalation is an uncovered param rather than an absent generator.

- **Gather these from the human. NEVER invent one** — not under a "safe default" rationale, not from a plausible-looking constant in the source. This is unchanged doctrine: the `REQUIRED_CALLER_FIELDS` class has no safe default, and an uncovered required param means every task would fail (#195).
- An uncovered param has exactly two honest remedies: the human supplies the value (`fixed_params`), or the entry point gains a signature default. A value read out of a comment or a README is not a default.

**3c. `needs_wrapper_argv`** — direct decoration is structurally blocked and the entry point's CLI surface is not introspectable by the composite, so `argv` + `signature` must come from the caller. The escalation carries `argv_kind` (why), `pathway_rule`, `argv_head` (the leading argv elements code CAN compose), `missing_fields`, `missing_intent_fields`, and `python_module_alternative`.

First ask whether the surface was mechanically extracted — `detect-entry-point` reads argparse and click parameter declarations by AST:

```bash
hpc-agent detect-entry-point --experiment-dir <experiment_dir>
```

Read the candidate row whose `path` equals the escalation's `entry_point_path`, then branch on its `argv_extraction`:

- **`extracted`** — `argv_params` is the mechanically read parameter list (in declaration order, which is the order the framework binds). Compose from it, not by eye: `argv` = `argv_head` + one `names[0]` / `{dest}` pair per option (positionals contribute the placeholder alone); `signature` = `{dest: type}` mapped into the interview's four accepted types (`str` / `int` / `float` / `bool` — `type` is *source text*, so a `pathlib.Path` converter maps to `str`). A param carrying `is_flag` takes NO value: append the flag alone, and its signature type is `bool`. A param carrying `secondary_names` is a click on/off pair — the OFF spelling is the secondary name, so the wrapper emits one or the other, never both. `multiple` means one argv occurrence per value; `choices` bounds the domain a swept value must stay inside.
- **A param carrying `unextracted`** — its names are real but the listed argument(s) were not modeled. Read those out of the source and hand-derive **only that param**.
- **`unsupported`** — `argv_params` is `null` and the whole surface is not mechanically knowable (typer derives its CLI from type hints, hydra from a composed YAML tree, fire from a live signature, a shell script has no Python surface at all). Hand-derive the flags from the source, on top of `argv_head`.

Hand-derivation is now the *remainder* — the unextracted arguments and the unsupported frameworks — and that remainder is the honest boundary. Never guess a flag name for an `extracted` param; never treat an `unsupported` verdict as license to skip reading the source.

**3d. `python_module_alternative`** — present on `needs_wrapper_argv` whenever the entry point is importable as a dotted `{module, function}` from the campaign dir. It is a DISCLOSURE, not a recommendation: it targets the same function with **no file edit**, so it is the answer when editing the file is undesirable — vendor code, a read-only checkout, a submodule the lab does not own. That is caller judgment, not a repo fact, which is why code never selects it. Offer it to the human alongside the argv ask, and pass `entry_point_kind: "python_module"` if they take it. Note the two constraints: no `fixed_params` (that kind's wire shape cannot carry them — cover the param with a signature default instead), and a `src`-layout package yields no importable dotted name, so the field is simply absent there.

**3e. `spec_invalid`** — a structural refusal, each naming its remedy in `remediation`:

- **`greenfield_repo`** — nothing to onboard. Scaffold a seed first, using the caller-supplied `shape` (default `script`), then re-run the composite:

  ```bash
  hpc-agent build-template --repo-dir . --shape script    # or --shape notebook
  ```

- **A contradictory `entry_point_kind`** — most often a forced `register_run` on a signature-rewriting decorator / a non-Python entry point / a file with no module-level `def`. The refusal is deliberate: rerouting it silently would break override-first, and letting it through would ship an executor the framework cannot introspect. Take one of the two remedies it states — drop the override, or say `shell_command` explicitly.
- **Unparseable / underivable** — the Python entry point does not parse, a caller `run_name` is not a module-level `def`, or a wrapper `run_name` does not sanitize to a Python identifier. Surface it to the caller; the skill does not loop on its own.

### 4. Persist: invoke the `interview` primitive

The `onboarded` return carries `interview_spec` — the composed fragment (`goal` / `task_count` / `task_generator` / `entry_point`, with `frozen_configs`, `fixed_params` and the pathway's entry-point block already filled). Add the one field the verb deliberately leaves out and write the result to `/tmp/interview_spec.json`:

```json
{ "produced_by": {"kind": "human"} }
```

`produced_by.operator` is composed server-side from `git config user.name` and disclosed in `interview.json._materialized.composed_defaults`; a caller-supplied operator is left untouched. Do not shell out to `git config` yourself.

If the caller pre-resolved the series axis AND the pathway is `wrapper`, add `entry_point.data_axis_hint` here — it is valid **only** on `entry_point.kind: shell_command` (#260); emitting it on a `register_run` spec fails schema validation and costs a retry round-trip. Otherwise omit it and let Step 5 own the axis.

```bash
hpc-agent interview --spec /tmp/interview_spec.json --campaign-dir .
```

On `ok=True`: the envelope reports the materialized artifacts (`tasks.py`, `interview.json`, plus `.hpc/wrappers/<run_name>.py` only on the wrapper pathway), `total_tasks`, and `cmd_sha`. On `error_code=spec_invalid`: surface the message to the caller — most often a typo (argv placeholder not in signature) or a missing frozen config. The skill does not loop on its own; the caller (slash or MARs) decides whether to re-supply.

### 5. The data axis belongs to `classify-axis-auto`

Do not walk the axis decision tree here. `classify-axis-auto` is the deterministic head for it (preflight → the AST fast-path matcher → the `axes.yaml` recorder, in one call), and it escalates `needs_llm_tree` — with the `source_path` to read and the `run_signature_sha` to echo — exactly when the matcher abstains and the LLM tree is genuinely needed:

```bash
hpc-agent classify-axis-auto --experiment-dir <experiment_dir>
```

On the wrapper pathway the wrapper body is a `subprocess.check_call`, so the matcher has nothing to introspect and the tree (or a caller-supplied `data_axis`) is load-bearing — see the `hpc-classify-axis` skill, which owns that dialog.

### 6. Emit the return envelope (final tool call)

The parent skill reads the return envelope from `<experiment_dir>/.hpc/_returns/hpc-wrap-entry-point.json`. Stage it, then emit:

1. Use the `Write` tool to write the envelope to `<experiment_dir>/.hpc/_returns/hpc-wrap-entry-point.staged.json`. Required fields on the Success branch: `ok: true`, `skill: "hpc-wrap-entry-point"`, `entry_point_kind` (`"register_run"` or `"shell_command"`), `run_name`, `tasks_py_path`, `interview_json_path`, `total_tasks` (from `interview`'s envelope), `cmd_sha` (from `interview`'s envelope). Optional: `wrapper_path` (set on the wrapper pathway; null/omit on the direct-decoration pathway), `files_edited` (the entry-point file when `wrap-entry-point-auto` reported `decorated: true`; empty `[]` otherwise). On a fatal error, write the standard `ErrorEnvelope` shape.

2. Invoke as your FINAL tool call:

   ```bash
   hpc-agent emit-skill-return --skill hpc-wrap-entry-point --experiment-dir <experiment_dir>
   ```

   The verb validates against `hpc_agent/schemas/skill_returns/hpc-wrap-entry-point.json` and atomically renames `.staged.json` → `.json`. Then **hand control back to the parent without ending your turn** — emit no summary or closing message. The parent's next action is `hpc-agent fetch-skill-return --skill hpc-wrap-entry-point`.

The submit workflow's Step 0b picks up `_materialized.entry_point` and threads `executor_cmd` into the submit-flow spec (wrapper pathway) or runs its normal `@register_run` discovery (direct-decoration pathway) — no further setup needed.

## Notes

- **One call, then only judgment.** The deterministic head is `wrap-entry-point-auto`; this skill's remaining job is the three named escalations plus the caller-owned intent. If you find yourself reconstructing which pathway applies, which YAMLs are frozen, or which params are uncovered, you are re-deriving something the verb already decided — read its return instead.
- **Two on-ramps, one contract.** Greenfield repos scaffold an entry point via `build-template --shape {script,notebook}` (the `greenfield_repo` refusal); mature repos onboard the existing one. Both paths end in the same place: a `@register_run`-decorated function on disk plus a materialized `tasks.py` + `interview.json`. The canonical description of the contract is `docs/internals/experiment-contract.md`.
- **Direct decoration is the default; the wrapper is a rescue boat.** A two-line code edit beats a subprocess shim whenever it's possible. The wrapper is for non-Python entry points, decorator conflicts, and read-only vendor code — and `python_module` is the third option for that last case, when the file must not be edited at all.
- **Decoration is a verb, never an edit.** This skill carries no `Edit` tool: an `Edit`-tool decoration once rewrote a scaffold's whole body into experiment logic, and that affordance is removed. `wrap-entry-point-auto` performs the bounded AST line-splice (`from hpc_agent import register_run` + `@register_run`, body byte-identical) as its last step.
- **Idempotent.** Re-running with the same intent overwrites `interview.json` (and, on the wrapper pathway, the wrapper file) byte-equivalently (modulo `_materialized.at`). Editing the underlying entry point's flags requires re-running this skill.
- **Signature drift safety (wrapper pathway).** The wrapper's typed signature is what `validate-executor-signatures` checks at submit time. If the entry point's actual flags drift from the declared signature, the canary catches the argparse / CLI error (one task, not a hundred).
- **The wrapper IS the contract (wrapper pathway).** The framework reads the wrapper's signature, not the entry point's. Keep the wrapper in sync.
- **One frozen experiment per YAML.** Each `configs/exp_NN.yaml` is its own experiment with its own `cmd_sha`. Two submits of the same YAML dedup; an in-place edit makes `cmd_sha` differ. To run a different frozen experiment, re-run this skill against the new YAML. `frozen_configs` requires a `task_generator` — a hand-written `tasks.py` has to include the shas itself.
- **Ambiguity escalates, never auto-resolves silently.** Multiple entry points without a caller pick, a missing intent field, an uncovered required param, a non-introspectable CLI surface — each is a named escalation with the exact field that resolves it, and each leaves the repo untouched.
