`/classify-axis-hpc` is the **human-facing wrapper** around the `hpc-classify-axis` skill.

The skill itself is agent-autonomous — it walks the `DataAxis` decision tree and commits a classification without any `[Y/n]` prompts (a MARs experiment agent calls it directly with a `run_name`). This slash command sits *between* the user and the skill: it conducts the propose-then-confirm dialog, then invokes the skill with the user-confirmed `data_axis` baked into the spec so the skill skips its own classifier and just records.

Reasons to invoke standalone (`/submit-hpc` walks through it automatically on a cache miss):

- Pre-classify a notebook's `run()` before committing to a submission.
- The experiment's loop structure changed (an accumulator added, a window widened) and the stored classification is stale — the run's `run_signature_sha` drifted.
- Inspect or correct a prior classification without re-running the whole submit flow.

## Procedure (in-chat agent)

### 1. Discover the runs and resolve the cache

```bash
python .hpc/scaffold.py discover
```

Each line is `<path>::<name>  gpu=<bool>  sha=<run_signature_sha>  flags=[...]`. If the user named a specific run, scope to it. If multiple runs exist and the user didn't pick, ask:

```
Three @register_run functions in this repo:
  1. forecast      (notebooks/forecasting.ipynb)
  2. backtest      (notebooks/backtesting.ipynb)
  3. train_model   (src/train.py)

Which one should I classify?  [1 / 2 / 3]
```

Read `<experiment>/.hpc/axes.yaml`. If `executors.<name>` exists and its `run_signature_sha` matches the run's current sha, **report the cached classification and exit** — no interview, no skill invocation. (The skill would short-circuit too, but the slash short-circuiting first avoids spawning it.)

### 2. Walk the decision tree against the run's source

Read the run's source. The single question that classifies every axis (from `hpc_agent/experiment_kit/axis.py`): **is there carried state across the series, and is its transition associative?**

1. **Does each row's result depend on rows computed before it?**
   No → **`Independent`**. The loop body is a pure function of its row.
2. **Yes → is the carried state a fixed-size summary combinable in any order** — a sum, a count, a mean/variance via moments?
   Yes → **`Associative`**. Pick the monoid: `sum` or `moments` (default `moments`).
3. **Is the dependence a bounded look-back** — e.g. a rolling training window of N rows?
   Yes → **`BoundedHalo`**. Derive the halo as an arithmetic expression over `run()`'s parameters (bare names), e.g. `train_window * 48`. Bias the estimate **large** — over-wide halos are merely wasteful; too-small halos are silent corruption.
4. **Otherwise → `Sequential`** as the proposal default.

The halo expression is restricted to bare parameter names, numeric literals, `+ - * //`, and `min()`/`max()`. It is never `eval()`'d.

Before proposing cold, query prior classifications to pre-fill:

```bash
hpc-agent recall --root <experiments-root> --task-kind <kind>
```

If a similar experiment classified an analogous series as `BoundedHalo(train_window * 48)`, propose the same and call it out.

### 3. Propose, then confirm

Show the experimenter the proposal with one-sentence reasoning:

```
Your run `forecast` iterates an 8760-row hourly series. The loop refits
the model on a trailing `train_window`-day window each step — a bounded
look-back. I'll classify it as:

  DataAxis = BoundedHalo,  halo = train_window * 48

Looks right?  [Y / n / unsure]
```

On **n**, take the correction and re-propose against the new branch. On **unsure**, fall back to `Sequential` — the fail-safe default (serial is slow, not wrong). Never finalize as a splittable axis without an explicit confirmation; the human is the source of truth for *which* tree branch resolves here. (The skill's autonomous mode does its own tie-break to `Sequential`; the slash mode requires affirmation.)

### 4. Invoke the skill with the resolved `data_axis`

Once the user confirms (or accepts the `Sequential` fallback), invoke the `hpc-classify-axis` skill via the Skill tool. The skill's input contract: when the caller supplies a fully-resolved `data_axis`, the skill skips its own classification (Step 4 of `skills/hpc-classify-axis/SKILL.md`) and just records.

Pass the spec:

```json
{
  "run_name": "forecast",
  "run_signature_sha": "<sha from Step 1>",
  "data_axis": { "kind": "bounded_halo", "halo": { "expr": "train_window * 48" } },
  "classified_by": "interview"
}
```

`classified_by` is `"interview"` because the human confirmed it. (Autonomous callers pass `"agent"`; cache hits pass `"recall"`.)

The skill records the result into `.hpc/axes.yaml`'s `executors` block and adds the transcript turns to `interview.json`.

## Notes

- **`DataAxis` ≠ scheduling axes.** `axes.yaml` holds two unrelated things: the `executors.<run>.data_axis` block (this command — *how to split the series*) and `homogeneous_axes` / `axes` (`/hpc-axes-init` — *which sweep dimension goes on the task array*). They are orthogonal; this command never touches the scheduling axes.
- **The elision gate is the backstop.** A classification can be wrong — `/submit-hpc` runs `assert_elision_equivalent` (whole vs split, assert equality) before any cluster time is spent. Recommend the experiment repo wire it into CI; for autonomous (MARs) callers, treat it as hard-blocking.
- **Idempotent, one entry per run.** Re-running overwrites `executors.<run>` modulo the timestamp; a repo with several `@register_run` functions accumulates one entry each.
- **Why this slash exists.** The skill is autonomous (it runs the same tree without confirmation). The slash exists so the human's read of `run()` — which is often more accurate than a heuristic walk of the AST — can override the autonomous default before the classification is recorded.
