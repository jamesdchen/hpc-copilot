`/campaign-hpc` is the **human-interview wrapper** around the `hpc-campaign` skill — the agent-autonomous decision layer that drives one tick of a closed-loop campaign (the per-iteration `submit → monitor → aggregate` loop whose `tasks.py` adapts to prior results).

The slash handles the first-time-setup interview (path picking, slug tagging) and per-tick user-facing dialogs.

## First-time setup interview (only the first time per campaign)

### Path

Ask the user:

```
Do you have a fixed grid to step through — walk-forward windows, ablations,
a manual hyperparam sweep? → Path A.

Or do you want an optimizer to choose params adaptively — Optuna, random-search,
PBT? → Path B.
```

**Path A**: walk the user through writing `tasks.py` with the manual grid.

**Path B**: walk through writing `tasks.py` with the strategy library. Critical: **add `_optuna_trial_number` (or equivalent unique marker) into kwargs** so each iteration's `cmd_sha` differs even when the strategy picks repeat params. Without this, the framework dedupes the second iteration silently and the campaign collapses. The `hpc-campaign` skill enforces this via `validate-campaign`'s `missing_stochastic_marker` error.

### Slug

```
What should we call this campaign?
```

Validate against `^[A-Za-z0-9._\-]+$`.

## Per-tick invocation

Invoke the `hpc-campaign` skill via the Skill tool with the per-tick spec:

```
Skill("hpc-campaign", {
  experiment_dir: ".",
  campaign_id: "<slug>",
  path: "<A | B>",
  allow_warnings: <true|false>  // default true
})
```

## On `needs_resolution` — walking ambiguities

The campaign skill propagates ambiguities from its composed skills (`hpc-submit`, `hpc-status`, `hpc-aggregate`) plus its own. Use the same dialog templates as `/submit-hpc`, `/monitor-hpc`, `/aggregate-hpc` for those fields.

Campaign-specific ambiguities:

### Dialog: `allow_warnings`

Validate-campaign produced warnings (e.g., walltime below historical p95). Ask:

```
Validation warning: <message>. Proceed anyway? [Y/n]
```

If Y → re-invoke with `allow_warnings: true`.

### Dialog: `decide_response`

The driver surfaced a judgement call (budget gate, convergence gate). Present the question + context from `ambiguity.context`, with options from `ambiguity.candidates`:

```
<question from context>
  1. continue — <description>
  2. stop — <description>
  3. increase_budget — <description>
Which?
```

Re-invoke with the user's answer in the `decide_response` field.

## On `spec_invalid` from the skill

- `unknown_campaign` — show the list of known campaigns; user picks.
- `validate_campaign_failed` — surface findings by severity:

  | Severity | Action |
  |---|---|
  | `error` | "Validation errors: <code>: <message>. Apply <suggested_fix> or edit tasks.py / dataset / playbook.yaml; rerun /campaign-hpc." |
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

  There is no `--force` flag by design. If a rule is wrong for the project, edit `.hpc/playbook.yaml` (version-controlled).

## On final envelope

Surface:
- `data.report.result.step` (which step ran this tick)
- `data.report.result.run_id` and `lifecycle_state` (if applicable)
- `data.report.decisions`
- `data.report.anomalies`

## When the user asks "show me what landed"

```bash
hpc-agent campaign list                                  # if >1, ask which
hpc-agent campaign status --campaign-id <slug>           # per-iteration history
```

Group multiple in-flight runs by `campaign_id` for display.

## For unattended runs

- Schedule a recurring campaign-tick run in cron.
- `/loop 30m /campaign-hpc` inside a chat session.

## Notes

- **Pause and resume is trivial.** State lives in sidecars + the campaign cursor on disk.
- **Concurrency is opt-in.** Default to sequential.
