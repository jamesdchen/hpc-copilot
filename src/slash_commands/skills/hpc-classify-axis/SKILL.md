---
name: hpc-classify-axis
description: "Classify a @register_run experiment's series axis as a DataAxis (Independent / Associative / BoundedHalo / Sequential) through a short proposes-then-confirms interview, and record it into .hpc/axes.yaml."
allowed-tools: Bash Read Write
execution: inline
category: experimenter-intent
---

Agent-facing composition over the **[classify-axis](../../docs/primitives/classify-axis.md) primitive**. Conducts the *classification interview* — the agent reads `run()`, proposes a `DataAxis` with one-sentence reasoning, the experimenter confirms or corrects — then records the resolved classification into `<experiment>/.hpc/axes.yaml`'s `executors` block.

`@register_run` already captures the entry point, the CLI flags (from the signature), and `gpu`. It does **not** capture the *parallel decomposition* of the totally-ordered series the experiment iterates. This skill closes that gap. It runs inside `/submit-hpc` Step 3, or standalone to pre-classify before a submission.

## Two unrelated "axis" concepts — do not conflate

| Concept | File | Question it answers |
|---|---|---|
| **`DataAxis`** (this skill) | `axes.yaml` → `executors.<run>.data_axis` | *How is it correct to split the totally-ordered series?* |
| **scheduling axes** | `axes.yaml` → `homogeneous_axes` / `axes` | *Which sweep dimension is promoted onto the SGE/SLURM task array?* |

They are orthogonal and live in the same file under different keys. This skill **only** writes the `executors` block. It never touches `homogeneous_axes` or the `pick_array_axis` machinery — `classify-axis` round-trips those untouched.

## Steps

### 1. Identify the run

Discover the `@register_run` functions over `notebooks/`:

```bash
python .hpc/scaffold.py discover
```

Each line is `<path>::<name>  gpu=<bool>  sha=<run_signature_sha>  flags=[...]`. If a specific run was named, scope to it; otherwise the caller picked one already. Record its `name` and `run_signature_sha`.

### 2. Cache check — reuse a still-valid classification

Read `<experiment>/.hpc/axes.yaml`. If `executors.<name>` exists **and** its `run_signature_sha` equals the run's current `run_signature_sha`, the stored `DataAxis` is still valid — **reuse it, skip the interview**, and report which classification was reused. A mismatch (or no entry) means the signature changed or the run was never classified → continue.

### 3. Pre-fill from memory (recall)

Before asking cold, query prior classifications:

```bash
hpc-agent recall --root <experiments-root> --task-kind <kind>
```

Each campaign summary now carries `data_axes: {run_name: {kind, halo_expr?, monoid?}}`, and the rollup carries a `data_axis_kinds` histogram. If a prior *similar* experiment classified an analogous series as `BoundedHalo` with `halo = train_window * 48`, **propose that** in Step 5 ("your `forecast_v2` run classified the same rolling-window shape as BoundedHalo(train_window*48) — same here?") instead of deriving from scratch. The experimenter still confirms.

### 4. Read `run()` and walk the decision tree

Read the run's source. The single question that classifies every axis (from `hpc_agent/template/axis.py`): **is there carried state across the series, and is its transition associative?**

1. **Does each row's result depend on rows computed before it?**
   No → **`Independent`**. The loop body is a pure function of its row (a DOALL loop) — split anywhere.
2. **Yes → is the carried state a fixed-size summary combinable in any order** — a sum, a count, a mean/variance via moments?
   Yes → **`Associative`**. Pick the monoid: `sum` (additive) or `moments` (mean/variance via sufficient statistics). Default `moments`.
3. **Is the dependence a bounded look-back** — e.g. a rolling training window of N rows?
   Yes → **`BoundedHalo`**. Elicit the halo as an arithmetic expression over `run()`'s parameters (bare names), e.g. `train_window * 48`. Bias the estimate **large** — an over-wide halo is merely wasteful; a too-small halo is silent corruption.
4. **Otherwise, or "unsure" → `Sequential`.** This is the fail-safe default. From `axis.py`: *"When in doubt, classify as Sequential: the fail-safe outcome is slow, not wrong."*

The halo expression is restricted: only bare parameter names, numeric literals, `+ - * //`, and `min()`/`max()`. It is **never `eval()`'d** — `hpc_agent.experiment_kit.axis_config` walks it with a restricted AST interpreter.

### 5. Propose, then confirm

Show the experimenter the proposal with one-sentence reasoning, mirroring `/hpc-axes-init`:

```
Your run `forecast` iterates an 8760-row hourly series. The loop refits
the model on a trailing `train_window`-day window each step — a bounded
look-back. I'll classify it as:

  DataAxis = BoundedHalo,  halo = train_window * 48

Looks right?  [Y / n / unsure]
```

On **n**, take the correction and re-propose. On **unsure**, fall back to `Sequential` — never auto-classify as anything but the `Sequential` fail-safe default without an explicit confirmation.

### 6. Record the classification

Build a spec and invoke [classify-axis](../../docs/primitives/classify-axis.md):

```json
{
  "run_name": "forecast",
  "run_signature_sha": "<sha from Step 1>",
  "data_axis": { "kind": "bounded_halo", "halo": { "expr": "train_window * 48" } },
  "classified_by": "interview"
}
```

```bash
hpc-agent classify-axis --experiment-dir <dir> --spec <spec.json>
```

`data_axis` shapes by kind:

| kind | extra fields |
|---|---|
| `independent` | — |
| `associative` | `monoid: "sum" \| "moments"` |
| `bounded_halo` | `halo: { expr: "<arithmetic over params>" }` |
| `sequential` | — |

Set `classified_by` to `recall` when the classification was reused from a prior experiment (Step 3), or `manual` when the operator stated it directly. On a `spec_invalid` envelope the most common cause is a `bounded_halo` whose `halo.expr` is not safe arithmetic over the run's parameters — surface the message and re-elicit.

### 7. Persist the *why* (transcript)

So the reasoning is durable, persist the interview transcript via the existing [interview](../../docs/primitives/interview.md) primitive — add the classification turns to the campaign's `interview.json` transcript (role `agent` for proposals, `operator` for confirmations). `classify-axis` records the *answer*; `interview` records the *conversation that reached it*.

### 8. The elision gate is mandatory before submit

A classification is a human (or LLM) program-analysis judgment — and program analysis is sometimes wrong. A misclassified axis runs fine and returns **plausible-but-wrong** numbers. `/submit-hpc` runs `hpc_agent.experiment_kit.assert_elision_equivalent` (whole run vs split run, assert equality) as a pre-submit gate. Tell the experimenter: if they register an experiment fixture, the gate catches a wrong answer here before any cluster time is spent; with no fixture the gate warns rather than blocks.

## Notes

- **Idempotent.** Re-running with the same classification overwrites `executors.<name>` byte-equivalently modulo the `classified_at` timestamp.
- **One entry per `@register_run` function.** A repo with several runs accumulates several `executors` entries; each is keyed by run name and carries its own `run_signature_sha`.
- **Signature drift invalidates the classification.** Editing `run()`'s parameters changes its `run_signature_sha`; the next `/submit-hpc` detects the mismatch and re-runs this skill.
- This skill writes to the experiment repo's `.hpc/axes.yaml` only — never to the framework repo.
