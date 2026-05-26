`/campaign-hpc` is the **human-interview wrapper** around the `hpc-campaign` skill — the agent-autonomous decision layer that drives one tick of a closed-loop campaign (the per-iteration `submit-flow → monitor-flow → aggregate-flow` loop whose `tasks.py` adapts to prior results).

The slash's job is the first-time-setup interview (path picking, slug tagging) and per-tick user-facing dialogs (validate-campaign findings interpretation, `decide` step responses). Per-tick mechanics live in the skill, which composes `hpc-submit`, `hpc-status`, and `hpc-aggregate` for each phase.

## First-time setup interview (only the first time per campaign)

### Path

Ask the user:

```
Do you have a fixed grid to step through — walk-forward windows, ablations,
a manual hyperparam sweep? → Path A.

Or do you want an optimizer to choose params adaptively — Optuna, random-search,
PBT? → Path B.
```

**Path A**: walk the user through writing `tasks.py` with the manual grid. `resolve(task_id)` enumerates the grid; `total()` returns its size.

**Path B**: walk the user through writing `tasks.py` with the strategy library. Inside `total()` / `resolve()`, the user calls:
- `study.tell(prev_trial, prev_metric)` for each prior iteration (loaded via `prior(experiment_dir, campaign_id)`)
- `study.ask()` to get the next batch
- **Add `_optuna_trial_number` (or equivalent unique field) into the kwargs dict** so each iteration's `cmd_sha` differs even when the strategy picks repeat params. Without this, the framework dedupes the second iteration silently and the campaign collapses. The `hpc-campaign` skill enforces this via `validate-campaign`'s `missing_stochastic_marker` error.

### Slug

```
What should we call this campaign?
```

Validate against `^[A-Za-z0-9._\-]+$`.

## Per-tick handoff

Each `/campaign-hpc` invocation drives one tick. Invoke the `hpc-campaign` skill via the Skill tool:

```
Skill("hpc-campaign", {
  experiment_dir: ".",
  campaign_id: "<slug>",
  path: "<A | B>",
  allow_warnings: true,
  mode: "interview"
})
```

The skill reads the campaign cursor, asks the campaign driver what to do next, composes the matching workflow skill (submit / status / aggregate), and returns the per-tick envelope.

Surface to the user:
- `data.report.result.step` (which step ran this tick)
- `data.report.result.run_id` and `lifecycle_state` (if applicable)
- `data.report.decisions` — validate-campaign findings handled, decide defaults applied
- `data.report.anomalies`

## On `validate-campaign` findings (skill returns `validate_campaign_failed`)

The skill blocks the tick if validation has errors. Surface to the user by severity:

| Severity | Dialog |
|---|---|
| `error` | "Validation found errors:<br>- `<code>`: `<message>`<br>Apply `<suggested_fix>` or edit `tasks.py` / dataset / playbook.yaml; rerun /campaign-hpc." |
| `warning` (when `allow_warnings=false`) | "Validation warning: walltime 3600s is below historical p95 (5400s). Proceed anyway?" If yes, re-invoke with `allow_warnings: true`. |
| `info` | Surface for visibility; doesn't block. |

Common `code` values:

| `code` | What to fix |
|---|---|
| `literal_value_not_allowed` | Fix the value in `tasks.py.resolve(i)` per `evidence.allowed`. |
| `missing_parameter` | Remove the kwarg or add it to the executor signature. |
| `row_index_oob` | Drop the index or extend the dataset. |
| `required_column_null` | Drop the row or backfill the column. |
| `walltime_below_quantile` | Raise `requested_walltime_sec` to `evidence.quantile_sec`. |
| `known_bad_combination` | Switch GPU type or remove the workload tag. |
| `missing_stochastic_marker` (Path B) | Add `_optuna_trial_number` to kwargs. |

There is no `--force` flag by design. If a rule is wrong for the project, edit `.hpc/playbook.yaml` (one version-controlled commit).

## On `decide` step (skill returns the decide envelope)

The driver surfaces `decide` steps for judgement calls — budget gates, convergence gates, early-stop suggestions. The skill returns the question + context; the slash asks the user:

```
The campaign has used 48 of 60 cluster-hours budgeted. Three iterations
remaining at the current burn rate would exceed the budget. Options:
  [1] continue — proceed with the remaining iterations
  [2] stop — declare the campaign complete with current results
  [3] increase budget — set a new ceiling and continue
Which?
```

Re-invoke `hpc-campaign` with the user's answer in the `decide_response` field.

## When the user asks "show me what landed"

```bash
hpc-agent campaign list                                  # if >1, ask which
hpc-agent campaign status --campaign-id <slug>           # per-iteration history
```

Group multiple in-flight runs by `campaign_id` for display.

## For unattended runs

Two options:

- Schedule a recurring campaign-tick run in cron. The campaign driver CLI is the headless surface — each tick advances one step.
- `/loop 30m /campaign-hpc` inside a chat session — repeats this slash on a 30-minute interval.

## Notes

- **Pause and resume is trivial.** State lives in sidecars + the campaign cursor on disk. Re-running the slash resumes from the cursor.
- **Path B `_optuna_trial_number` is load-bearing.** Without a unique marker per iteration, `cmd_sha` is the same across ticks → the submit-flow primitive dedupes → the campaign silently collapses. The skill enforces this via `validate-campaign`.
- **Concurrency is opt-in.** Default to sequential — walk-forward iteration N+1 depends on N's result. For K iterations in flight (Optuna's `constant_liar=True` is built for this), pass `--concurrency K` to the driver.
