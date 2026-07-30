---
name: wrap-entry-point-auto
verb: workflow
side_effects:
- filesystem: '<entry point> (in-place: import + @register_run) — direct-decoration
    pathway only'
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent wrap-entry-point-auto [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.incorporation.wrap_entry_point_auto.wrap_entry_point_auto
---
# wrap-entry-point-auto

The deterministic head of the `hpc-wrap-entry-point` skill, collapsed into
**one call**. Composes `detect-entry-point` (the six-probe repo scan) →
the pathway decision table → `decorate-entry-point` (the bounded AST
line-splice) → the frozen-YAML convention scan → the fixed-params
partition. The sub-verbs are called **directly in-process** — no
subprocess fan-out — so the strict detect-produces-the-candidates →
table-consumes-them dependency is a code invariant rather than a prose
instruction an agent re-derives on every run.

Three of those five steps existed only as prose tables in the skill
(`SKILL.md` lines 93-104, 147-159, 180-193). This primitive promotes all
three to code. The LLM now makes one tool call and only works the genuine
judgment: an entry-point tie, the human-owned intent fields, and the argv
template for a non-introspectable wrapper entry point.

## Inputs

See `hpc_agent/schemas/wrap_entry_point_auto.{input,output}.json`. Every
field is optional; a bare call gets as far as the first judgment point and
names it.

- `entry_point_path` / `run_name` — override the detection ladders.
- `entry_point_kind` — force the pathway (`shell_command` is the table's
  explicit-caller-choice row).
- `argv` / `signature` — the wrapper pathway's template + typed signature.
- `goal` / `task_generator` / `task_count` — the human-owned intent. Never
  invented here.
- `frozen_configs` / `fixed_params` — override the convention scan / cover
  uncovered required params.

`experiment_dir` is the framework-context argument (the repo root every
detected path is relative to).

## Outputs

A discriminated result over four terminal shapes. One succeeds; three are
**named escalations** that state exactly which value is needed and why code
must not produce it.

| Shape | Meaning |
|---|---|
| `{onboarded: true, ...}` | Every deterministic step ran. Carries the pathway + deciding rule ids, the decoration echo, the frozen configs + their `<stem>_sha` kwargs, the param partition, and the ready-to-hand `interview_spec` fragment. |
| `{needs_pick: true, ...}` | An entry-point-**file** tie (`reason: entry_point_tie`) or an entry-**function** tie (`entry_function_tie`). Every candidate is listed; `resolve_with` names the field that breaks it. |
| `{needs_intent: true, ...}` | `goal` / `task_generator` / `task_count` is absent, or a specific required param of the entry point's signature is uncovered. `never_invented` pins the subset code must never fabricate. |
| `{needs_wrapper_argv: true, ...}` | The wrapper pathway needs `argv` + `signature`. `argv_kind` names *why* the surface is not introspectable and `argv_head` carries the leading elements code CAN compose. |

**Every escalation leaves the repo byte-identical.** Decoration is the last
step, after all three escalation branches are ruled out — so a
non-`onboarded` return has written nothing.

## The entry-point ladders

The FILE, in order (the deciding rung is echoed as `entry_point_rule`):

1. `caller_entry_point_path`
2. `existing_register_run` — exactly one file already carries
   `@register_run`; the repo is already onboarded and that file IS the
   entry point.
3. `sole_candidate` — exactly one detection candidate matched.

Anything else with 2+ matches is an `entry_point_tie`. A repo with no
candidate and no decoration is `spec_invalid` (`greenfield_repo`) naming
`build-template` — this verb onboards an existing entry point, it never
authors one.

The FUNCTION (Python entry points only), in order:

1. `caller_run_name` (must be a module-level `def`)
2. `existing_register_run_function`
3. `conventional_main` — a def named `main`. Substrate convention (how
   Python entry points are spelled), not experiment semantics, so code may
   apply it.
4. `sole_public_def`
5. `no_decoratable_function` — none at all; routes to the wrapper.

## The pathway decision table (SKILL.md:93-104, promoted)

| Rule id | Pathway | Trigger |
|---|---|---|
| `caller_forced_shell_command` | wrapper | the caller set `entry_point_kind: shell_command` |
| `caller_forced_register_run` | decorate | the caller set `entry_point_kind: register_run` |
| `non_python_entry_point` | wrapper | shell script, binary, or console script |
| `signature_rewriting_decorator` | wrapper | `@hydra.main` / a consuming `@click.command` / `@app.command` |
| `no_decoratable_function` | wrapper | the file is all top-level code |
| `body_parses_argv` | wrapper | the resolved function reads `sys.argv` / drives a parser |
| `kwargs_signature` | decorate | the params are already real kwargs (**the default**) |

Rows are evaluated top-down. Over-refusal into the wrapper is safe by
design — the wrapper always works, whereas decorating through a
signature-rewriting decorator silently produces an executor the framework
cannot introspect. The caller override is evaluated FIRST (the prose lists
it last): an override that loses to a detected row is not an override.

The rewriting-decorator predicate is imported from `decorate-entry-point`
itself, so the table routes to the wrapper EXACTLY when the decoration verb
would have refused.

## The frozen-YAML scan (SKILL.md:147-159, promoted)

`configs/*.yaml`, `configs/*.yml`, `conf/*.yaml` — the prose's globs
verbatim, including the missing `conf/*.yml`. Widening them here would
silently change which files land in an experiment's `cmd_sha`, which is a
contract change rather than a transcription fix; a repo using `conf/*.yml`
passes `frozen_configs` explicitly. Each match yields a `<stem>_sha`
kwarg name (derived by the same helper the wrapper materializer hashes
with, so the two cannot drift), and those names count as **covered** in the
partition.

## The fixed-params partition (SKILL.md:180-193, promoted)

Every declared param lands in exactly one class:

- **axis** — the `task_generator` produces it per task (or the framework
  threads it as a `<stem>_sha`). Deliberately left out of `fixed_params`.
- **defaulted** — the entry point's own signature supplies a value.
- **uncovered** — required, not an axis, not in the caller's
  `fixed_params`. Every task would fail on it (#195), so it escalates by
  name. No value is ever manufactured: a signature default is what makes a
  param defaulted, and there is no other source code is entitled to read.

`**kwargs` on the entry point empties `uncovered` — an unmatched kwarg is
absorbed rather than a `TypeError`.

## Errors

- `spec_invalid` (`greenfield_repo`) — nothing to onboard. Remediation
  names `build-template --shape {script,notebook}`.
- `spec_invalid` — the Python entry point does not parse, a caller
  `run_name` is not a module-level `def`, the entry-point file is
  unreadable, or a wrapper `run_name` cannot be derived as a valid Python
  identifier.

## Idempotency

Keyed on `experiment_dir`. A second call finds the decoration on disk
(rung 2 of the file ladder, rung 2 of the function ladder), reports
`already_decorated: true` / `decorated: false`, and writes nothing — the
repo is byte-identical to after the first call.

## Notes

- This verb never calls the `interview` primitive. It emits the
  `interview_spec` **fragment** instead; `produced_by` is deliberately
  absent (stamping the operator is the interview verb's own composer) and
  so is `data_axis_hint` (that is `classify-axis-auto`'s seat).
- It never materializes the wrapper file either — the `interview` verb owns
  that write. The wrapper pathway's job here is to compose and validate the
  `shell_command` entry block.
