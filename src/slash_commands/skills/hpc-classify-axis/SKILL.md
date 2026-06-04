---
name: hpc-classify-axis
description: "Classify a @register_run experiment's series axis as a DataAxis (Independent / Associative / BoundedHalo / Sequential) autonomously, and record the result into .hpc/axes.yaml. Callers supplying a pre-resolved data_axis (e.g. the /classify-axis-hpc slash, after a human-facing dialog) bypass the autonomous classifier and the skill just records what it was given."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Agent-facing composition over the **[classify-axis](../../../../docs/primitives/classify-axis.md) primitive**. The skill is autonomous: it reads `run()`, walks the decision tree, commits a `DataAxis`, and records it. No `[Y/n]` prompts. Callers that need a human in the loop (the in-chat agent driving `/classify-axis-hpc`) conduct the interview *before* invoking the skill and pass the human-confirmed `data_axis` in as input — in that mode the skill skips its own classification and just records.

`@register_run` already captures the entry point, the CLI flags (from the signature), and `gpu`. It does **not** capture the *parallel decomposition* of the totally-ordered series the experiment iterates. This skill closes that gap.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" here means **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** — the harness runs them concurrently. It does NOT mean shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`), which trips the permission classifier as a compound command and complicates output parsing. Multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should each be their own tool-call block in the same message, not chained inside a single shell invocation.
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

### 1. Identify the run

Discover the `@register_run` functions over `notebooks/`:

```bash
hpc-agent discover-runs --experiment-dir .
```

The envelope's `data.runs` is a list of `{path, name, gpu, run_signature_sha, flags}`. Resolve to a single run:

- If the caller supplied `run_name`, scope to it.
- Else if exactly one run exists, use it.
- Else (multiple runs, no `run_name` supplied) **return `spec_invalid` with `error_code: ambiguous_run`** listing the candidates. The skill does not pick blindly across multiple registered runs.

Record `name` and `run_signature_sha`.

### 2. Cache check — reuse a still-valid classification

Read `<experiment>/.hpc/axes.yaml`. If `executors.<name>` exists **and** its `run_signature_sha` equals the run's current `run_signature_sha`, the stored `DataAxis` is still valid — **reuse it, return early**, and report which classification was reused. A mismatch (or no entry) means the signature changed or the run was never classified → continue.

### 3. Pre-fill from memory (recall)

**Skip this step if the caller already supplied `data_axis`** (the slash path; the classification is already resolved). Jump to Step 5.

Otherwise, before classifying cold, query prior classifications:

```bash
hpc-agent recall --root <experiments-root> --task-kind <kind>
```

Each campaign summary carries `data_axes: {run_name: {kind, halo_expr?, monoid?}}`, and the rollup carries a `data_axis_kinds` histogram. If a prior *similar* experiment classified an analogous series — same loop shape, same parameter names — adopt its classification (set `classified_by: "recall"`) and jump to Step 5. If no clean match, continue to Step 4.

### 4. Skip if caller supplied `data_axis`; otherwise classify

**If the caller already supplied `data_axis` in the spec** (the human-driven slash path, after `/classify-axis-hpc` ran its interview), set `classified_by: "interview"` and jump to Step 5 — do not re-classify.

**Otherwise, classify autonomously.** The classifier is hybrid: a stdlib AST pattern-matcher (`classify-axis-easy`) handles ~80% of cases without LLM reasoning; the LLM decision tree below is the long-tail fallback for novel patterns.

#### 4a. Try the cheap match first

```bash
hpc-agent classify-axis-easy --source-path <path-from-Step-1> --run-name <name>
```

The envelope's `data` carries `{kind, evidence, halo_expr?, tried}`. The matcher's autonomous scope is narrow: `independent`, `bounded_halo` (committed with a structurally-extracted `halo_expr`), or `sequential` (carried state but no recognized halo pattern — safe default). Anything outside that scope (`unclassifiable` / `no_loop_detected` / `function_not_found`) falls through to Step 4b.

Branch on `data.kind`:

| `kind` | action |
|---|---|
| `independent` | Committed. Build `data_axis: {kind: "independent"}`. Jump to Step 5. |
| `bounded_halo` | Committed — the matcher recognized one of the pattern-library shapes (first-order / finite-order stencil, bounded-window deque, pandas rolling, EMA) and extracted `data.halo_expr`. Build `data_axis: {kind: "bounded_halo", halo: {expr: data.halo_expr}}`. Jump to Step 5. **No LLM call is needed** — the halo expression is already structurally derived. |
| `sequential` | Committed — the matcher saw carried outer-scope state but no halo pattern matched. Sequential is the safe default; the framework will run the inner loop serially. Build `data_axis: {kind: "sequential"}`. Jump to Step 5. |
| `no_loop_detected` | Committed — the matcher confidently found **no ordered-series loop** to split. Build `data_axis: {kind: "cartesian"}` (a plain cartesian sweep — distinct from `independent`, which has a parallelizable series). Jump to Step 5. **No LLM call needed**, and **not** a fall-through: a confident no-loop signal is a terminal verdict, recorded so the worker never re-infers it. |
| `unclassifiable` / `function_not_found` | Fall through to Step 4b — genuinely uncertain (a parse the matcher couldn't resolve); the LLM tree decides, or the run escalates. |

Set `classified_by: "agent"`. Carry `data.evidence` forward verbatim as the one-line reasoning for Step 6's transcript turn.

**Halo expression syntax** (`hpc_agent.experiment_kit.axis_config`): only bare `run()` parameter names, numeric literals, `+ - * //`, and `min()` / `max()`. It is **never `eval()`'d** — a restricted AST interpreter walks it. The matcher emits halo expressions that already conform to this syntax. Bias the estimate **large** — an over-wide halo is merely wasteful; a too-small halo is silent corruption.

**Note: the matcher does NOT autonomously classify `Associative`.** The framework provides task-array map-reduce via `combine-wave`; users who want to parallelize an inner reduction express it as a sweep dimension in their `task_generator`. Step 4b's LLM tree still recognizes Associative — the matcher just doesn't.

#### 4b. Walk the LLM decision tree (long-tail fallback)

Only invoked on `unclassifiable` / `function_not_found` from Step 4a (a confident `no_loop_detected` is already the terminal `cartesian` verdict — see 4a). The long tail covers novel patterns the matcher doesn't recognize — including **Associative** classifications (since the matcher does not detect Associative autonomously). Read the run's source. The single question that classifies every axis (from `hpc_agent/experiment_kit/axis.py`): **is there carried state across the series, and is its transition associative?**

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

### 6. Persist the *why* (transcript)

Persist the reasoning via the existing [interview](../../../../docs/primitives/interview.md) primitive — add a single turn (`role: agent`) summarising which tree branch resolved and which parameters were referenced. `classify-axis` records the *answer*; `interview` records the *one-line rationale*. When `classified_by: "interview"`, the slash command writes its own operator/agent turns to the transcript directly — this skill does not duplicate them.

### 7. The elision gate is the backstop

A classification can be wrong — agent-side via heuristic mistake, human-side via misread. A misclassified axis runs fine and returns **plausible-but-wrong** numbers. `/submit-hpc` runs `hpc_agent.experiment_kit.assert_elision_equivalent` (whole run vs split run, assert equality) as a pre-submit gate. For autonomous callers (MARs), wire the elision gate into CI hard-blocking — without a fixture the gate currently warns rather than blocks, which is acceptable for the human-driven path but thin cover for the agent-driven one.

### 8. Emit the return envelope (final tool call)

The parent skill reads the return envelope from `<experiment_dir>/.hpc/_returns/hpc-classify-axis.json`, not from any chat message you might write. Stage it, then emit:

1. Use the `Write` tool to write the envelope to `<experiment_dir>/.hpc/_returns/hpc-classify-axis.staged.json`. Required fields on the Success branch: `ok: true`, `skill: "hpc-classify-axis"`, `run_name`, `run_signature_sha`, `data_axis` (the same shape Step 5 passed to the primitive), `classified_by` (`"interview"` / `"recall"` / `"agent"`). Optional: `reasoning` (the one-line rationale from Step 6). On a fatal error, write the standard `ErrorEnvelope` shape (`ok: false`, `error_code`, `message`, `category`, `retry_safe`) — same fields as any `hpc-agent` error envelope.

2. Invoke as your FINAL tool call:

   ```bash
   hpc-agent emit-skill-return --skill hpc-classify-axis --experiment-dir <experiment_dir>
   ```

   The verb validates the staged envelope against `hpc_agent/schemas/skill_returns/hpc-classify-axis.json`, then atomically renames `.staged.json` → `.json`. On schema failure the staged file is preserved for debugging and a `spec_invalid` envelope identifies the failing JSON path. Then **stop** — do not write a closing chat message. The parent's next action is `hpc-agent fetch-skill-return --skill hpc-classify-axis`.

## Notes

- **Two input modes, one output contract.** Autonomous callers supply only `run_name` (skill classifies and records); slash / human-driven callers supply a full `data_axis` (skill records what it was given). Both produce the same `axes.yaml` update.
- **Idempotent.** Re-running with the same classification overwrites `executors.<name>` byte-equivalently modulo the `classified_at` timestamp.
- **One entry per `@register_run` function.** A repo with several runs accumulates several `executors` entries; each is keyed by run name and carries its own `run_signature_sha`.
- **Signature drift invalidates the classification.** Editing `run()`'s parameters changes its `run_signature_sha`; the next `/submit-hpc` detects the mismatch and re-runs this skill.
- This skill writes to the experiment repo's `.hpc/axes.yaml` only — never to the framework repo.
