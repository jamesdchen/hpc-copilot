`/campaign-hpc` is the **human-interview wrapper** around the `hpc-campaign` skill — the block-loop relay for a closed-loop campaign. A campaign spec is **greenlit once at start** (`campaign-greenlight`); execution then runs fully asynchronously with no per-iteration human boundary — only anomaly briefs (`campaign-watch`) and the completion brief (`campaign-complete`). The slash handles first-time setup (path, slug, spec authoring), invokes the skill, and relays each brief.

## First-time setup (only the first time per campaign)

### Path

Ask the user:

```
Do you have a fixed grid to step through — walk-forward windows, ablations,
a manual hyperparam sweep? → Path A.

Or do you want an optimizer to choose params adaptively — Optuna, random-search,
PBT? → Path B.
```

**Path A:** walk the user through `tasks.py` with the manual grid.
**Path B:** scaffold with `hpc-agent scaffold-strategy --name {optuna,pbt} --output-dir .` and customize only the search space. Critical: **add `_optuna_trial_number` (or equivalent unique marker) to kwargs** so each iteration's `cmd_sha` differs even on repeat params — without it the framework dedupes the second iteration and the campaign collapses. `campaign-greenlight`'s validation enforces this via `missing_stochastic_marker`.

### Slug

```
What should we call this campaign?
```

Validate against `^[A-Za-z0-9._\-]+$`.

## The flow

1. **Greenlight once.** Invoke the skill; it runs `campaign-greenlight` and hands back the digested spec brief (goal / budget / strategy / stop criteria / anomaly policy). Relay it; the user answers `y` (greenlight the whole spec) or a nudge (edit the spec, re-digest). On `y` the block stamps the greenlight and journals it, then execution runs asynchronously.
2. **Watch (no per-iteration boundary).** `campaign-watch` is a cheap read. A healthy campaign self-chains ticks — surface the health digest and let the user walk away. An anomaly brief (loud-fail guard or budget halt) is a `y`/nudge decision. Poll it on a schedule rather than blocking.
3. **Complete.** `campaign-complete` hands back the completion brief — spend vs budget, iterations, stop reason, a code-extracted per-iteration outcome table. The user chooses the interpretation.

## Invocation

Invoke the `hpc-campaign` skill via the Skill tool:

```
Skill("hpc-campaign", {
  experiment_dir: ".",
  campaign_id: "<slug>",
  path: "<A | B>"
})
```

## `spec_invalid` from the skill

- `unknown_campaign` — show the known campaigns; the user picks.
- `validate_campaign_failed` — surface findings by severity. Common `code` values:

  | `code` | What to fix |
  |---|---|
  | `literal_value_not_allowed` | Fix the value in `tasks.py.resolve(i)` per `evidence.allowed`. |
  | `missing_parameter` | Remove the kwarg or add it to the executor signature. |
  | `walltime_below_quantile` | Raise `requested_walltime_sec` to `evidence.quantile_sec`. |
  | `missing_stochastic_marker` (Path B) | Add `_optuna_trial_number` to kwargs. |

  There is no `--force` by design. If a rule is wrong for the project, edit `.hpc/playbook.yaml` (version-controlled).

## For unattended runs

- Schedule a recurring `campaign-watch` tick in cron.
- `/loop 30m /campaign-hpc` in-session.

## Notes

- **Greenlit once, then asynchronous.** No per-iteration human loop by design; pause/resume is trivial (state lives in sidecars + the campaign cursor on disk).
- **Concurrency is opt-in.** Async-refill correctness (drain-before-stop, budget headroom) is the driver's job.
