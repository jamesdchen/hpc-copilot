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
- `entry_point_kind` — force the pathway. All three `InterviewSpec`
  entry-point kinds are reachable: `register_run`, `shell_command`, and
  `python_module` (see below).
- `argv` / `signature` — the wrapper pathway's template + typed signature.
- `data_axis_hint` / `solver` — the two **wrapper-only** interview fields
  (see below). Copied through verbatim; refused by name on the other two
  pathways.
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
| `{needs_wrapper_argv: true, ...}` | The wrapper pathway needs `argv` + `signature`. `argv_kind` names *why* the surface is not introspectable, `argv_head` carries the leading elements code CAN compose, `argv_extraction` / `argv_params` carry the mechanical parameter read the in-process scan already produced, and `python_module_alternative` names the other kind SKILL.md:98 offers for the same row. |

**Every escalation leaves the repo byte-identical.** Decoration is the last
step, after all three escalation branches are ruled out — so a
non-`onboarded` return has written nothing. Pinned by
`test_no_escalation_branch_writes_to_the_repo` (parametrized over all four
branches, on an *undecorated* fixture so the snapshot can actually move) plus
`test_the_snapshot_pin_can_actually_fire`.

## All three entry-point kinds are representable

| Kind | Pathway | Chosen by |
|---|---|---|
| `register_run` | `decorate` | code (the default row) |
| `shell_command` | `wrapper` | code (every fallback row) |
| `python_module` | `module` | **caller override only** |

`python_module` targets the function by dotted path (`{kind, module,
function}`) with no file edit — the framework introspects the undecorated
signature. Code never selects it autonomously, because what separates it from
direct decoration on the *same* kwarg'd function is "may we edit this file"
(vendor code, a read-only checkout) — caller judgment, not a repo fact.
SKILL.md:98 offers it as row 2's second option, so `needs_wrapper_argv`
**discloses** the derived `{module, function}` whenever one is importable; the
gap is named, never silent.

The dotted name is only offered when the file is importable with the campaign
dir on `sys.path` (what `interview._validate_python_module_entry` prepends,
mirroring the cluster's `$REPO_DIR` on `PYTHONPATH`): a top-level module, or a
package chain where every directory carries an `__init__.py`. A `src`-layout
package yields no target — forcing `python_module` there is a named refusal
rather than a dotted name the interview's own validator would reject.

`python_module`'s wire shape carries **no `fixed_params`**, so supplying one is
refused rather than silently dropped, and the uncovered-param ask names a
satisfiable remedy there (a signature default, or a different kind).

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
| `caller_forced_python_module` | module | the caller set `entry_point_kind: python_module` |
| `caller_forced_register_run` | decorate **or refusal** | the caller set `entry_point_kind: register_run` |
| `non_python_entry_point` | wrapper | shell script, binary, or console script |
| `signature_rewriting_decorator` | wrapper | `@hydra.main` / a consuming `@click.command` / `@app.command` |
| `no_decoratable_function` | wrapper | the file is all top-level code |
| `body_parses_argv` | wrapper | the resolved function reads `sys.argv` / drives a parser |
| `kwargs_signature` | decorate | the params are already real kwargs (**the default**) |

Rows are evaluated top-down. Over-refusal into the wrapper is safe by
design — the wrapper always works, whereas decorating through a
signature-rewriting decorator silently produces an executor the framework
cannot introspect. The caller-override rows are evaluated FIRST (the prose
lists the override last): an override that loses to a detected row is not an
override. That ordering is **pinned**, not merely documented —
`test_override_first_beats_a_detected_row` and
`test_override_first_pin_can_fire_on_the_default_kind` both go red if the
override rows are relocated below the detected ones.

`caller_forced_register_run` is the one override that cannot simply win: the
outcome it asks for on a signature-rewriting decorator is exactly the unsafe
one SKILL.md:104 warns about. Silently rerouting it to the wrapper would break
override-first; letting it through would ship an un-introspectable executor. So
it is a **named refusal** (`spec_invalid`) stating both remedies — drop the
override, or say `shell_command` explicitly. The same refusal covers forcing
`register_run` onto a non-Python entry point or a file with no module-level
`def`.

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

## The carried argv extraction

`detect-entry-point` runs **in-process** here, and every candidate it returns
carries an `argv_extraction` verdict plus an `argv_params` list (argparse /
click parameters read straight off the AST; `unsupported` + `null` for every
surface whose flags are not declared as literals — see
[`detect-entry-point`](detect-entry-point.md) and the source of truth,
`src/hpc_agent/ops/argv_extract.py`).

Both fields ride the `needs_wrapper_argv` escalation. The composite already
paid for that scan, so dropping them would force the caller into a second
`detect-entry-point` call to compose the same argv template — re-opening the
produce→consume seam this verb exists to close. An entry point the *caller*
named that the scan never surfaced has no classified surface, so it reports
`unsupported` / `null`: the same honest verdict a typer file gets, never an
absent field a consumer has to read as "unknown".

## The two wrapper-only interview fields

`data_axis_hint` (#260) and `solver` exist on the interview's
`shell_command` entry shape and on **neither** introspectable shape. Both are
accepted as optional inputs and copied **verbatim** onto the composed
`shell_command` entry block:

- `data_axis_hint` — a wrapper body is a `subprocess.check_call` that
  `classify-axis` cannot introspect, so on this pathway the experimenter's
  declaration is the only way the classification reaches `axes.yaml` without
  an interactive tree. That is exactly why the field is load-bearing here and
  a caller error anywhere else: `register_run` / `python_module` are
  introspectable, so `classify-axis` reads the real function.
- `solver` — the checkpoint-instrumentation hint. The caller's override wins;
  otherwise the adapter `detect-entry-point` recognized in the source
  (`solver: "petsc"` on the candidate) becomes that adapter's default hint,
  because dropping a detected solve loop silently costs a long solve its
  preemption-safety.

Supplying either on the `decorate` / `module` pathway is a **named refusal**,
not a silent drop — the same posture `fixed_params` on `python_module`
already had. Both wire shapes are `extra="forbid"` and declare neither field,
so a value passed there could only vanish.

## Errors

- `spec_invalid` (`greenfield_repo`) — nothing to onboard. Remediation
  names `build-template --shape {script,notebook}`.
- `spec_invalid` — `data_axis_hint` / `solver` supplied on the `decorate` or
  `module` pathway (wrapper-only, #260). Raised before the decoration write,
  so the repo stays byte-identical.
- `spec_invalid` — the Python entry point does not parse, a caller
  `run_name` is not a module-level `def`, the entry-point file is
  unreadable, or a wrapper `run_name` cannot be derived as a valid Python
  identifier.
- `spec_invalid` — a contradictory `entry_point_kind`: `register_run` on a
  signature-rewriting decorator / a non-Python entry point / a file with no
  module-level `def`; `python_module` on a path with no importable dotted
  name, or carrying `fixed_params` (which that kind's wire shape cannot
  represent).

## Idempotency

Keyed on `experiment_dir`. A second call finds the decoration on disk
(rung 2 of the file ladder, rung 2 of the function ladder), reports
`already_decorated: true` / `decorated: false`, and writes nothing — the
repo is byte-identical to after the first call.

## Notes

- This verb never calls the `interview` primitive. It emits the
  `interview_spec` **fragment** instead — and that fragment is
  **submittable**: `interview.input.json` requires `produced_by`, so the
  fragment carries the minimal who-CLASS suggestion `{kind: "human"}`. It
  never names an OPERATOR; the interview's own P1.c composer fills
  `.operator` from `git config user.name` and discloses it as a composed
  default, so attribution requiredness is untouched. Pinned by
  `test_the_fragment_is_submittable_to_the_interview_verb`, which validates
  the fragment against `InterviewSpec` itself.
- It never walks the data-axis tree either (that is `classify-axis-auto`'s
  seat) — it only carries a `data_axis_hint` the caller already holds, and
  only onto the wrapper pathway.
- It never materializes the wrapper file either — the `interview` verb owns
  that write. The wrapper pathway's job here is to compose and validate the
  `shell_command` entry block.
