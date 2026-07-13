`/aggregate-hpc` is the **human-interview wrapper** around the `hpc-aggregate` skill — the block-loop relay that starts with `aggregate-check` (readiness + integrity, never auto-masking a problem) and, on a clean run, reduces via `aggregate-run` (whose reducer — never the LLM — computes every aggregate number). The slash parses arguments, invokes the skill, and relays each brief.

## The flow

1. **Parse `$ARGUMENTS`** — optional `profile`, `run_id`, and whether the user pre-approved partial aggregation.
2. **Invoke the skill.** It runs `aggregate-check` and hands back the readiness/integrity brief.
3. **Relay the brief, collect `y` or a nudge.** Show the readiness digest (terminal status, combined/failed waves) and any `integrity_issues` — each carries `auto_masked: false` and a conservative recommendation. Plus the `next_block` suggestion (`aggregate-run` when clean). The user greenlights with a `y` or nudges (e.g. approve `allow_partial` on `missing_waves`). No per-field `[Y/n]` dialogs.
4. **Loop.** On `y`, the skill journals the greenlight and fires `aggregate-run`; relay its results-table brief. The user chooses the interpretation — do not re-compute or interpret the numbers.

## Invocation

Invoke the `hpc-aggregate` skill via the Skill tool (only the fields the user pinned):

```
Skill("hpc-aggregate", {
  experiment_dir: ".",
  run_id: <required — confirm with the user if not stated>,
  allow_partial: <if user pre-approved>
})
```

`run_id` is required (`aggregate-check`'s spec demands it; nothing
auto-discovers a run) — if the user didn't name one, list `.hpc/runs/`
and confirm before the first tick. The block resumes its own stage from
the journal.

## Relaying a brief

- **Multiple profiles with terminal runs:** the check brief lists them; ask which and fold into the nudge.
- **`missing_waves`:** surfaced as a decision with the safe recommendation to investigate (a partial usually masks a real cluster failure). The user explicitly greenlights `allow_partial` after seeing what's missing.
- **A contamination / provenance / column-schema integrity issue** is never auto-masked and carries `next_block: null` — surface it; investigation is the user's branch, not a proceed-anyway default.

## `spec_invalid` from the skill

- `nothing_to_aggregate` (cluster-confirmed via the check's reconcile): "Nothing to aggregate — no terminal runs."
- A run that ran-and-failed surfaces the classified error; an `abandoned` run surfaces a re-submit / combiner-only remediation.

## Notes

- **Refuse partial by default.** Aggregating on incomplete waves silently produces wrong final metrics.
- **The reducer computes every number.** Never hand-assemble `metrics.json`, even for a mean of ten numbers.
- **Idempotent.** Re-aggregating produces byte-identical output.
