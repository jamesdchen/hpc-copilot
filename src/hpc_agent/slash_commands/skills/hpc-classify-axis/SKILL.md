---
name: hpc-classify-axis
description: "Classify a @register_run experiment's series axis as a DataAxis (Independent / Associative / BoundedHalo / Sequential) autonomously, and record the result into .hpc/axes.yaml. Callers supplying a pre-resolved data_axis (e.g. `/submit-hpc`'s interview phase, after a human-facing dialog) bypass the autonomous classifier and the skill just records what it was given."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Agent-facing composition over the **[classify-axis](../../../../docs/primitives/classify-axis.md) primitive**. Autonomous: reads `run()`, walks the decision tree, commits a `DataAxis`, records it. No `[Y/n]` prompts. Human-driven callers (`/submit-hpc`'s interview phase) pass a pre-resolved `data_axis` and the skill skips classification, just records.

`@register_run` captures entry point, CLI flags, and `gpu` — but **not** the parallel decomposition of the ordered series the experiment iterates. This skill closes that gap.

## Execution style

- **Batch independent tool calls into one assistant message.** Multiple Bash / Read / Grep / Glob tool-call blocks in one message run concurrently. Do NOT use shell-level concurrency (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`) — trips the permission classifier as a compound command.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Saves a round-trip + permission prompt. Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block.
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Return via the emit-skill-return file primitive — never via chat.** The Skill tool result is no longer the return mechanism; the parent (`hpc-submit`, `hpc-campaign`, …) reads your return envelope from `<experiment_dir>/.hpc/_returns/hpc-classify-axis.json`. The final step of this skill (Step 7 below) writes that envelope and invokes `hpc-agent emit-skill-return` as the LAST tool call — no closing chat message of any kind. A non-tool-call closing message fires the harness's end-of-turn signal, the parent never resumes, and the user has to type "keep going". The schema for the envelope lives at `hpc_agent/schemas/skill_returns/hpc-classify-axis.json` and is enforced by the emit verb.

## Inputs

Callers pass either a partial or fully-resolved spec:

| Field | Source | Required? |
|---|---|---|
| `run_name` | Caller, or auto-discovered if a single `@register_run` exists | Optional |
| `run_signature_sha` | Computed by Step 1 | — (filled here) |
| `data_axis` | Caller (slash, after human dialog) **or** filled by Step 4 (autonomous tree walk) | Optional |
| `classified_by` | `"interview"` when the caller supplied `data_axis`; `"recall"` when Step 3 reused a prior; `"agent"` otherwise | — (filled here) |

## Two unrelated "axis" concepts — do not conflate

| Concept | File | Question it answers |
|---|---|---|
| **`DataAxis`** (this skill) | `axes.yaml` → `executors.<run>.data_axis` | *How is it correct to split the totally-ordered series?* |
| **scheduling axes** | `axes.yaml` → `homogeneous_axes` / `axes` | *Which sweep dimension is promoted onto the SGE/SLURM task array?* |

They are orthogonal and live in the same file under different keys. This skill **only** writes the `executors` block.

## Steps

### 1–4a. Classify in one call — `classify-axis-auto`

Steps 1–4a (discover the run, cache-check, recall pre-fill, and the cheap AST match) are collapsed into **one composite verb** — [classify-axis-auto](../../../../docs/primitives/classify-axis-auto.md). It calls `classify-axis-preflight` → `classify-axis-easy` → the `classify-axis` recorder **directly in-process** (no subprocess fan-out), so the strict preflight-produces-`source_path` → matcher-consumes-it dependency is a code invariant — you no longer hand-sequence (and cannot mis-order) the chain. You make one call and only do work on the genuine long tail.

```bash
hpc-agent classify-axis-auto \
  --experiment-dir . \
  --spec <spec.json>
```

Pass `--spec` only when you have inputs to give; a bare `--experiment-dir .` call classifies the sole `@register_run` autonomously. The spec (`hpc_agent/schemas/classify_axis_auto.input.json`) is all-optional:

```json
{
  "run_name": "forecast",
  "data_axis": { "kind": "bounded_halo", "halo": { "expr": "train_window * 48" } },
  "root": "<experiments-root>",
  "task_kind": "<kind>"
}
```

- `run_name` — scope when multiple `@register_run` functions exist (else the sole one is used; multiple with no scope → `spec_invalid` `ambiguous_run`).
- `data_axis` — supply when the caller already resolved the classification (the human-driven slash path, after `/submit-hpc`'s interview resolved it); the composite records it directly as `classified_by: "interview"` and runs neither recall nor the matcher.
- `root` / `task_kind` — forwarded to the recall sub-call for memory pre-fill.

**Branch on the returned `data` — exactly two outcomes:**

#### `data.recorded == true` — done

The composite resolved and recorded the classification. `data` carries `{recorded: true, run_name, kind, classified_by, axes_path}`. `classified_by` is `"interview"` (you supplied `data_axis`), `"recall"` (a prior similar campaign classified the same run with a confident kind), or `"agent"` (the AST matcher committed a confident kind: `independent` / `bounded_halo` with a structurally-extracted halo / `sequential` / `cartesian` from a confident no-loop verdict). A still-valid cache hit also returns `recorded: true`, reusing the stored classification with **no re-write**. **Skip Steps 4b and 5** — the axis is already in `axes.yaml`. Carry `kind` + `classified_by` into Step 8's return envelope.

#### `data.needs_llm_tree == true` — walk the long tail

The matcher abstained (`unclassifiable` / `function_not_found`) and **nothing was recorded**. `data` carries `{needs_llm_tree: true, run_name, source_path, run_signature_sha, evidence, tried}`. This is the genuine-judgement case: proceed to **Step 4b**, reading the run body from `data.source_path`, then record via the `classify-axis` primitive (Step 5) using `data.run_name` / `data.run_signature_sha`.

**Halo expression syntax** (`hpc_agent.experiment_kit.axis_config`): only bare `run()` parameter names, numeric literals, `+ - * //`, and `min()` / `max()`. It is **never `eval()`'d** — a restricted AST interpreter walks it. Bias the estimate **large** — an over-wide halo is merely wasteful; a too-small halo is silent corruption.

**Note: the matcher does NOT autonomously classify `Associative`.** The framework provides task-array map-reduce via `combine-wave`; users who want to parallelize an inner reduction express it as a sweep dimension in their `task_generator`. Step 4b's LLM tree still recognizes Associative — the matcher just doesn't.

#### 4b. Walk the LLM decision tree (long-tail fallback)

Only invoked on `unclassifiable` / `function_not_found` from Step 4a. Covers novel patterns including **Associative** (which the matcher does not detect). Read the run's source. The classifying question: **is there carried state across the series, and is its transition associative?**

<!-- decision-content:axis-tree start -->
1. **Does each row's result depend on rows computed before it?**
   No → **`Independent`**. The loop body is a pure function of its row (a DOALL loop) — split anywhere.
2. **Yes → is the carried state a fixed-size summary combinable in any order** — a sum, a count, a mean/variance via moments?
   Yes → **`Associative`**. Pick the monoid: `sum` (additive) or `moments` (mean/variance via sufficient statistics). Default `moments`.
3. **Is the dependence a bounded look-back** — e.g. a rolling training window of N rows?
   Yes → **`BoundedHalo`**. Derive the halo as an arithmetic expression over `run()`'s parameters (bare names), e.g. `train_window * 48`. Bias the estimate **large** — an over-wide halo is merely wasteful; a too-small halo is silent corruption.
4. **Otherwise, or ambiguous → `Sequential`.** This is the fail-safe default and the autonomous-mode tiebreaker. From `axis.py`: *"When in doubt, classify as Sequential: the fail-safe outcome is slow, not wrong."*
<!-- decision-content:axis-tree end -->

Set `classified_by: "agent"`. Record one short sentence of reasoning (which branch of the tree resolved, which parameters were named) for the transcript in Step 6.

### 5. Record the classification

Build a spec and invoke [classify-axis](../../../../docs/primitives/classify-axis.md):

```json
{
  "run_name": "forecast",
  "run_signature_sha": "<sha from Step 1>",
  "data_axis": { "kind": "bounded_halo", "halo": { "expr": "train_window * 48" } },
  "classified_by": "agent"
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
| `cartesian` | — (no ordered series; plain cartesian sweep) |

On a `spec_invalid` envelope the most common cause is a `bounded_halo` whose `halo.expr` is not safe arithmetic over the run's parameters — fall back to `Sequential` and re-invoke. (A human-driven caller surfaces the message and re-elicits; an autonomous caller takes the fail-safe.)

### 6. Carry the *why* into the return envelope

The one-line rationale (which tree branch resolved, which parameters were referenced) is carried forward in the return envelope's `reasoning` field — written in Step 8, read by the parent skill. There is **no separate transcript CLI call** here: the `interview` primitive is one-shot (`--spec` / `--campaign-dir`), with no incremental add-turn surface. The interview transcript is owned by the composing slash command (`/submit-hpc`) for human-driven runs; agent-classified runs surface their reasoning through the return envelope, not through `interview.json`.

### 7. The elision gate is the backstop

A classification can be wrong — agent-side via heuristic mistake, human-side via misread. A misclassified axis runs fine and returns **plausible-but-wrong** numbers. `/submit-hpc` runs `hpc_agent.experiment_kit.assert_elision_equivalent` (whole run vs split run, assert equality) as a pre-submit gate. For autonomous callers (MARs), wire the elision gate into CI hard-blocking — without a fixture the gate currently warns rather than blocks, which is acceptable for the human-driven path but thin cover for the agent-driven one.

### 8. Emit the return envelope (final tool call)

The parent skill reads the return envelope from `<experiment_dir>/.hpc/_returns/hpc-classify-axis.json`, not from any chat message you might write. Stage it, then emit:

1. Use the `Write` tool to write the envelope to `<experiment_dir>/.hpc/_returns/hpc-classify-axis.staged.json`. Required fields on the Success branch: `ok: true`, `skill: "hpc-classify-axis"`, `run_name`, `run_signature_sha`, `data_axis` (the same shape Step 5 passed to the primitive), `classified_by` (`"interview"` / `"recall"` / `"agent"`). Optional: `reasoning` (the one-line rationale from Step 6). On a fatal error, write the standard `ErrorEnvelope` shape (`ok: false`, `error_code`, `message`, `category`, `retry_safe`) — same fields as any `hpc-agent` error envelope.

2. Invoke as your FINAL tool call:

   ```bash
   hpc-agent emit-skill-return --skill hpc-classify-axis --experiment-dir <experiment_dir>
   ```

   The verb validates the staged envelope against `hpc_agent/schemas/skill_returns/hpc-classify-axis.json`, then atomically renames `.staged.json` → `.json`. On schema failure the staged file is preserved for debugging and a `spec_invalid` envelope identifies the failing JSON path. Then **hand control back to the parent without ending your turn** — emit no summary or closing message. The parent's next action is `hpc-agent fetch-skill-return --skill hpc-classify-axis`.

## Notes

- **Two input modes, one output contract.** Autonomous callers supply only `run_name` (skill classifies and records); slash / human-driven callers supply a full `data_axis` (skill records what it was given). Both produce the same `axes.yaml` update.
- **Idempotent.** Re-running with the same classification overwrites `executors.<name>` byte-equivalently modulo the `classified_at` timestamp.
- **One entry per `@register_run` function.** A repo with several runs accumulates several `executors` entries; each is keyed by run name and carries its own `run_signature_sha`.
- **Signature drift invalidates the classification.** Editing `run()`'s parameters changes its `run_signature_sha`; the next `/submit-hpc` detects the mismatch and re-runs this skill.
- This skill writes to the experiment repo's `.hpc/axes.yaml` only — never to the framework repo.
