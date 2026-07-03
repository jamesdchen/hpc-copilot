`/submit-hpc` is the **human-interview wrapper** around the `hpc-submit` skill — the block-loop relay that starts the submit chain (`submit-s1`), surfaces each block's code-digested brief, and drives the propose→`y`/nudge loop. The slash parses the user's arguments into the initial spec, invokes the skill, and relays each brief to the user; the skill body owns the block invocation (do not shell it from this slash).

## The flow

1. **Parse `$ARGUMENTS`** into an initial spec (whatever the user pre-stated — cluster, no-canary, campaign, free-form intent).
2. **Invoke the skill** on that spec. It runs `submit-s1` and hands back the first brief.
3. **Relay each brief, collect `y` or a nudge.** The block does the deciding-support work: `submit-s1` returns the resolved plan with every ambiguity carrying a **pre-filled recommendation** (cluster, entry-point, data-axis, walltime, …). Show the recommendations and the `next_block` suggestion, then let the user answer with a single `y` (greenlight the recommended plan and the suggested next block) or a natural-language nudge ("no — hold walltime, halve the grid"). There are no per-field `[Y/n]` dialogs any more: the brief carries the recommendations, the user greenlights or nudges the whole thing.
4. **Loop.** On `y`, the skill journals the greenlight and fires exactly the block the envelope named (`submit-s2` → `submit-s3` → `submit-s4`). On a nudge, the skill re-drafts a fresh brief from the same block. Continue until the harvest (`submit-s4`) brief.

The one field the block cannot invent is `task_generator` (the sweep shape) — when no `tasks.py` exists it surfaces as a required S1 field. Ask the user for the scale-up shape (`items_x_seeds` / `cartesian_product` / `enumerated` / `numeric_linspace`) and fold their answer into the nudge; the framework never invents a sweep.

## Invocation

Invoke the `hpc-submit` skill via the Skill tool with the initial spec (only the fields the user supplied):

```
Skill("hpc-submit", {
  experiment_dir: ".",
  cluster: <if user stated --cluster>,
  no_canary: <if user stated --no-canary>,
  campaign_id: <if --campaign-id>,
  task_generator: <if inferable from $ARGUMENTS, else omit>
})
```

The skill resolves the rest through the block loop; the slash never enumerates every field.

## Speculative canary (opt-in)

If the user wants to overlap the S1 review with the canary, tell the skill to run `submit-speculate` during the S1 round — a plain `y` then finds S2 already done, and a spec-changing nudge just re-canaries (nudges never cancel). One speculative canary per pending brief.

## Relaying a brief to the user

Present, per block:
- `reason` (the one-line state) and the human-readable `brief` (resolved fields + recommendations at S1; "canary green, est. N core-hours" at S2; the terminal status digest at S3; the code-extracted results table at S4).
- The `next_block` suggestion (its `verb` + `why`).

Collect `y` or the nudge and hand it back to the skill. Do **not** re-compute the brief's numbers or interpret the results table — at harvest the user chooses the interpretation from the code-extracted table.

## Args

`$ARGUMENTS` formats:
- Free-form intent: `"run ridge with horizon=[1, 5, 25]"` — parse to `task_generator` params.
- Flags: `--cluster <name>`, `--no-canary`, `--campaign-id <slug>`.
- Empty: invoke with `{experiment_dir: "."}`; the skill's S1 brief surfaces what needs the user.

## Common cluster failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `Eqw` state (SGE) | Job error | `qmod -cj <JOBID>` or resubmit |
| `PENDING` (SLURM) > 30 min | Resource unavailable | Try a different partition |
| Memory exceeded | Exceeded mem limit | Resubmit with higher memory |
| Walltime exceeded | Exceeded time limit | Resubmit with longer walltime |
| `ModuleNotFoundError` | Env not set up | Check modules and `conda_env` |
