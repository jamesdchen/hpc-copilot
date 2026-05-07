Invoke the `hpc-build-executor` skill via the Skill tool (`skills/hpc-build-executor/SKILL.md`) for the workflow: scaffold the executor, smoke-test it, classify axes, invoke `axes-init`. The skill is the canonical SoT.

This slash command is the human-facing entry point for the **axes-init** half — initializing `.hpc/axes.yaml` so the framework can pick a parallelism axis automatically at submit time. Reasons to invoke standalone (rather than letting `/submit-hpc` walk through it):

- The experiment already has `tasks.py` but no `axes.yaml` (cold-start case before first submission).
- The experimenter wants to pre-declare axis classifications without committing to a submission yet.
- The parallelism shape changed (axis added, semantics flipped) and the existing `axes.yaml` needs replacing — the user invokes this command and the skill prompts for `--force`.

## Human-facing dialog

The skill's classification step is conversational: agent reads the executor + tasks.py, proposes a classification with one-sentence reasoning per axis, and asks for confirmation:

```
Found these parallel axes in your experiment:
 • `window` (20 values) — homogeneous (same model trained on a 6-month rolling window)
 • `model` (4 values) — heterogeneous (linear / ridge / xgboost / neural_net have very different runtimes)
 • `data_type` (3 values) — heterogeneous (equities are 10x larger than fx)

I'll write `.hpc/axes.yaml` with `homogeneous_axes: [window]` so the framework promotes `window` to the task array.

Looks right? [Y/n]
```

On **N**, abort without writing. The user can re-run `/hpc-axes-init` later, or write `.hpc/axes.yaml` by hand.

On **Y**, the skill invokes `hpc-agent axes-init` and parses the envelope per the primitive's contract. **If `axes.yaml` already exists**, the primitive returns `wrote: false`. Re-prompt the user asking whether to pass `--force` (they may have hand-edited the file). Don't auto-force.

## Notes

- **The picker doesn't require this file to function** — when no `axes.yaml` exists and no priors exist, the picker returns `(None, "no axes.yaml")` and the caller falls back to asking the user explicitly. Running `/hpc-axes-init` makes the cold-start path *automatic* instead of interactive.
- **Field-mirror discipline**: the schema permits exactly the fields the framework can act on. Putting search-space definitions or objective functions here is rejected at validation time. Keep that intent in `tasks.py` / executor code where it belongs.
