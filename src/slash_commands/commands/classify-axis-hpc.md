Invoke the `hpc-classify-axis` skill via the Skill tool (`skills/hpc-classify-axis/SKILL.md`) for the workflow: discover the `@register_run` runs, check the `axes.yaml` classification cache, conduct the proposes-then-confirms `DataAxis` interview, and record the result via the `classify-axis` primitive. The skill is the canonical SoT.

This slash command is the human-facing entry point for the **DataAxis classification** step — telling the framework *how the experiment's totally-ordered series may be split correctly* (`Independent` / `Associative` / `BoundedHalo` / `Sequential`). `/submit-hpc` walks through it automatically on a cache miss; reasons to invoke standalone:

- Pre-classify a notebook's `run()` before committing to a submission.
- The experiment's loop structure changed (an accumulator added, a window widened) and the stored classification is stale — the run's `run_signature_sha` drifted.
- Inspect or correct a prior classification without re-running the whole submit flow.

## Human-facing dialog

The classification is conversational: the agent reads `run()`, proposes a `DataAxis` with one-sentence reasoning, and asks for confirmation:

```
Your run `forecast` iterates an 8760-row hourly series. The loop refits
the model on a trailing `train_window`-day window each step — a bounded
look-back. I'll classify it as:

  DataAxis = BoundedHalo,  halo = train_window * 48

Looks right?  [Y / n / unsure]
```

On **unsure**, the skill falls back to `Sequential` — the fail-safe default (a serial run is slow, not wrong). It never auto-classifies as a splittable axis without explicit confirmation. On **Y**, it invokes `hpc-agent classify-axis`, which records the result into `.hpc/axes.yaml`'s `executors` block.

## Notes

- **`DataAxis` ≠ scheduling axes.** `axes.yaml` holds two unrelated things: the `executors.<run>.data_axis` block (this command — *how to split the series*) and `homogeneous_axes` / `axes` (`/hpc-axes-init` — *which sweep dimension goes on the task array*). They are orthogonal; this command never touches the scheduling axes.
- **The elision gate is the backstop.** A classification can be wrong — `/submit-hpc` runs `assert_elision_equivalent` (whole vs split, assert equality) before any cluster time is spent. Recommend the experiment repo wire it into CI.
- **Idempotent, one entry per run.** Re-running overwrites `executors.<run>` modulo the timestamp; a repo with several `@register_run` functions accumulates one entry each.
