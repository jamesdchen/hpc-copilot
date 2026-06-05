---
name: hpc-build-executor
description: "Scaffold a new executor file from the starter template into the experiment repo, then customize it. Autonomous: the caller supplies `--name` and `--output-dir`; the skill scaffolds, customizes, smoke-tests, classifies axes by heuristic, and invokes `axes-init`. No `[Y/n]` prompts."
allowed-tools: Bash Read Write
execution: inline
category: agent-autonomous
---

Agent-facing composition over the **[build-executor](../../../../docs/primitives/build-executor.md) primitive** (see that file for full input/output/error contract). Materializes the bundled starter template at a caller-supplied path; the skill then customizes it.

This skill also covers axis-init — the companion step that writes `.hpc/axes.yaml` so the framework can pick a parallelism axis automatically at submit time. The two are paired in practice: a new executor needs an `axes.yaml` describing which of its parallel dimensions belongs on the task array.

## Execution style

- **Batch independent tool calls into one assistant message.** "Parallel" here means **multiple Bash / Read / Grep / Glob tool-call blocks in a single message** — the harness runs them concurrently. It does NOT mean shell-level concurrency inside one Bash call (`cmd1 & cmd2 & wait`, `parallel`, `xargs -P`), which trips the permission classifier as a compound command and complicates output parsing. Multiple reads, greps, or `hpc-agent describe`/`--help` lookups with no data dependency should each be their own tool-call block in the same message, not chained inside a single shell invocation.
- **Chain sequential `hpc-agent` calls with `&&` in one Bash block when the next call does NOT branch on prior structured output** (e.g. `hpc-agent install-commands && hpc-agent load-context --experiment-dir .`). Each separate Bash tool call costs a round-trip + permission prompt; chaining unconditionally-sequential dependent invocations into one block saves both at no cost. Do NOT chain past a call whose envelope the next call's args depend on — read the envelope first, then issue the dependent call as its own block. (The framework's dispatched `hpc-worker` subagent blocks `&&` by a `PreToolUse` hook — one verb per envelope is its decision-boundary contract — but that block applies only to the spawned worker, NOT to this orchestrator skill.)
- **Be terse.** Lead with the action or result; skip filler ("Let me…", "I'll go ahead and…") and trailing restatements of what tool output already shows.
- **Return via the emit-skill-return file primitive — never via chat.** The Skill tool result is no longer the return mechanism; the parent (`hpc-submit`, `hpc-campaign`, …) reads your return envelope from `<experiment_dir>/.hpc/_returns/hpc-build-executor.json`. After Steps 5 (axes-init) finishes, write the envelope and invoke `hpc-agent emit-skill-return` as the LAST tool call — no closing chat message of any kind. A non-tool-call closing message fires the harness's end-of-turn signal, the parent never resumes, and the user has to type "keep going". The schema for the envelope lives at `hpc_agent/schemas/skill_returns/hpc-build-executor.json` and is enforced by the emit verb.

## Inputs

| Field | Source |
|---|---|
| `name` | Caller (filename stem, no `.py`) |
| `output_dir` | Caller (absolute path inside the experiment repo) |
| `force` | Caller (default `false`; set `true` to overwrite) |
| `homogeneous_axes` | Caller, or filled by Step 2 of the axes-init companion (heuristic) |

## Steps (build-executor)

1. **Validate inputs**. `name` is the filename stem (no `.py`). `output_dir` is an absolute path inside the experiment repo, NOT inside the framework repo. The skill refuses with `spec_invalid` if `output_dir` resolves inside the framework repo's `templates/` tree.

2. **Invoke** [build-executor](../../../../docs/primitives/build-executor.md). Add `--force` only if intentionally overwriting an existing file.

3. **Parse the envelope** per the primitive's `outputs:` contract (`path`, `type`, `source`).

4. **On error envelopes**, branch by `error_code` per the primitive's frontmatter table — common: `spec_invalid` (destination exists; pass `--force` or pick a new name), `config_invalid` (template missing on disk; packaging bug, surface to caller), `executor_not_found` (output_dir parent unwritable).

5. **After scaffold succeeds**, use the Read tool to load `data.path`, then customize: fill in `compute(args)` with the experiment's actual computation. **Do not** add an argparse parser here — under the new contract the per-executor CLI flag list lives in `.hpc/tasks.py` `FLAGS["<importable_module_path>"]`, not in the executor file. The dispatcher in `.hpc/cli.py` parses argv at runtime and calls `compute(args)`.

6. **Smoke-test** the scaffolded executor via the [smoke-test-executor](../../../../docs/primitives/smoke-test-executor.md) verb — NOT an inline `python -c` (the execution-style header above forbids arbitrary `python -c`; this verb is the deterministic, classifier-permitted form of that exact import-and-`compute` recipe):

   ```bash
   hpc-agent smoke-test-executor --module-path <data.path> [--output-file /tmp/smoke.csv]
   ```

   The verb imports the module from the file path and calls `compute(Namespace(output_file=...))` in a subprocess, returning `data.{exit_code, stdout_tail, stderr_tail, timed_out}`. Branch on `data.exit_code`: `0` = clean, proceed; non-zero = fix-then-retry (the traceback tail is in `data.stderr_tail`); `null` with `data.timed_out: true` = the module spun past the timeout. (`--help` is not a useful smoke test for the new template — there's no `__main__` block; `compute` is the entry point, which is why the verb calls it rather than importing alone.)

## Steps (axes-init — companion)

The framework needs to know which parallel dimension to promote to the SLURM/SGE task array. The signal is **per-axis runtime homogeneity**: tasks within a task array share walltime + memory reservation, so heterogeneity within the array forces over-provisioning to the worst-case task. The most homogeneous axis is the right one.

1. **Inspect the experiment for parallel axes** in one call via the [inspect-parallel-axes](../../../../docs/primitives/inspect-parallel-axes.md) verb — instead of manually `Read`-ing `tasks.py` and `.hpc/axes.yaml` separately:

   ```bash
   hpc-agent inspect-parallel-axes --experiment-dir <output_dir's experiment root>
   ```

   It returns `data.tasks_py_body` (the raw `.hpc/tasks.py` text — never executed) plus the parsed `data.{axes, homogeneous_axes, executors, axes_yaml_present}`. Identify each parallel dimension from `data.tasks_py_body`. Common shapes: a `resolve(task_id)` function returning kwargs derived from `task_id` via cartesian product over named lists; a grid-search dict the executor reads; an explicit per-axis loop in driver code. `data.axes_yaml_present: true` means axes-init already ran (Step 3 will refuse-without-force). If you still need to consult a companion file the verb doesn't surface (`CLAUDE.md`, README, the executor script itself), `Read` that one file directly.

2. **Resolve `homogeneous_axes`.** If the caller supplied `homogeneous_axes` in the spec (the slash path, after `/hpc-axes-init` ran its propose-then-confirm dialog with the user), use it as-is — skip the heuristic. Otherwise classify each axis autonomously using the experiment's semantics. Heuristics that often hold:
   - Replicates / seeds / folds / cross-validation windows / time-series backtest windows → typically **homogeneous** (same compute on slightly different data).
   - Model class / architecture / algorithm → typically **heterogeneous** (orders-of-magnitude different cost).
   - Data type / dataset → depends on dataset sizes; usually mildly heterogeneous.
   - Hyperparameter sweeps → depends; learning rates rarely change cost; layer counts usually do.

3. **Invoke** [axes-init](../../../../docs/primitives/axes-init.md) with `--homogeneous-axes <comma-separated-names>`. Refuses to overwrite an existing `axes.yaml`; pass `--force` only when intentional.

4. **Parse the envelope** — confirm `wrote: true` and the resolved `axes_path`. On `wrote: false`, surface the existing file's contents to the caller (the slash, which re-prompts the user for `--force`; an autonomous caller decides programmatically). The skill itself does not prompt — the wrote-false envelope is the signal back to whoever invoked it.

## Step 5 — Emit the return envelope (final tool call)

The parent skill reads the return envelope from `<experiment_dir>/.hpc/_returns/hpc-build-executor.json`. Stage it, then emit:

1. Use the `Write` tool to write the envelope to `<experiment_dir>/.hpc/_returns/hpc-build-executor.staged.json`. Required fields on the Success branch: `ok: true`, `skill: "hpc-build-executor"`, `executor_path` (from `build-executor`'s `data.path`), `executor_type` (from `data.type`), `executor_source` (from `data.source`). Optional axes-init fields: `axes_path`, `axes_wrote`, `homogeneous_axes` — populate when the companion ran; leave as `null` (or omit) when it didn't. On a fatal error, write the standard `ErrorEnvelope` shape.

2. Invoke as your FINAL tool call:

   ```bash
   hpc-agent emit-skill-return --skill hpc-build-executor --experiment-dir <experiment_dir>
   ```

   The verb validates against `hpc_agent/schemas/skill_returns/hpc-build-executor.json` and atomically renames `.staged.json` → `.json`. Then **stop** — do not write a closing chat message. The parent's next action is `hpc-agent fetch-skill-return --skill hpc-build-executor`.

## Notes

- This skill writes to the experiment repo only — never to the framework repo's `templates/` dir. Confirm `--output-dir` is the experiment repo before invoking.
- After scaffolding and customizing, the executor is auto-discovered by [discover-executors](../../../../docs/primitives/discover-executors.md) (which `hpc-submit` invokes) if it lands in `executors/`, `scripts/`, or `src/` and either exports `compute(args)` (new contract) or has an `if __name__ == "__main__":` guard plus a CLI import (old contract; transitional).
- Per-task fan-out (Cartesian product, chunking, date windows, …) AND the new-contract executor's CLI flag list both live in `.hpc/tasks.py`, scaffolded by `/submit-hpc` Step 6 — not via this skill.
- **One-shot per repo** for axes-init under normal use. If the experiment's parallelism shape changes (axis added, semantics flipped), re-run with `--force`.
- **Cardinality is not yet recorded** in the v1 axes schema — only `homogeneous_axes` (a list of names). Cardinalities will land when submit-flow integration uses them to build the wave_map.
