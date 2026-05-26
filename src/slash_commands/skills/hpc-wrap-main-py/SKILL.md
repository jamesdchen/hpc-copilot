---
name: hpc-wrap-main-py
description: "Set up a mature repo (main.py + optional YAML configs) for hpc-agent submission. Detects the entry point, conducts a short proposes-then-confirms interview about its signature / frozen configs / data-axis classification, then invokes the `interview` primitive to materialize a wrapper at `.hpc/wrappers/<run_name>.py` plus a starter `tasks.py`. Run this once before `/submit-hpc` on a repo that doesn't have a `@register_run` notebook."
allowed-tools: Bash Read Write Glob
execution: inline
category: experimenter-intent
---

Agent-facing composition over the **[interview](../../docs/primitives/interview.md) primitive** for mature repos. The greenfield path is `@register_run` in a notebook; this skill is the analog for repos that already have `main.py` + YAML configs and don't want to rewrite. It conducts the conversational intake the headless `/submit-hpc` worker can't (the worker reads `interview.json`; this skill writes it) and then hands off to `/submit-hpc`.

The interview persists:
- A `@register_run` **wrapper** at `<experiment>/.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the user's `main.py` with kwargs substituted. The wrapper's typed signature is what downstream framework primitives (`classify-axis`, `validate-executor-signatures`) introspect. `main.py` stays untouched.
- A `tasks.py` (from the supplied `task_generator`) whose kwargs include `<stem>_sha` for every frozen YAML, so `cmd_sha` correctly distinguishes `exp_42.yaml` from `exp_43.yaml` and catches accidental in-place edits.
- An `interview.json` with a `_materialized.entry_point` block carrying the wrapper path, `executor_cmd`, frozen-config shas, and (optionally) a pre-declared `data_axis` — read by the submit workflow's Step 0b.

## When to run

- The user's repo has `main.py` (or another shell-invokable entry point), not `@register_run` in a notebook.
- The user wants to scale a *frozen* experiment configured by one YAML (or a small number of them) across seeds / shards / replicates — not sweep over the YAMLs themselves.
- A fresh `/submit-hpc` would escalate with `mature_repo_needs_interview` because the worker can't conduct the conversational intake itself.

## Steps

### 1. Detect the entry point

Walk the repo to propose a candidate entry point. Probe in order:

```bash
# Common entry-point locations
ls main.py src/main.py 2>/dev/null
# Python package with __main__.py
find . -maxdepth 3 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
```

Read the candidate file and look at its CLI surface — `argparse.ArgumentParser`, `@click.command`, `@app.command` (typer), or a `if __name__ == "__main__":` block calling something with `sys.argv`. If detection is ambiguous, ask the experimenter: *"Is `main.py` your entry point, or somewhere else?"* Record the path.

### 2. Propose the argv template + signature

From the detected file's CLI surface, propose:

- An `argv` template list, e.g. `["python3", "main.py", "--config", "{config}", "--seed", "{seed}"]`
- A `signature` dict mapping each `{placeholder}` to a Python type (`str` / `int` / `float` / `bool`) — derive from argparse `type=int`, click `IntType`, typer annotations, etc.

Show the proposal to the experimenter with one sentence of reasoning:

```
Your `main.py` takes `--config PATH` and `--seed INT`. I'll wrap it as:

  argv:      python3 main.py --config {config} --seed {seed}
  signature: config: str, seed: int

The wrapper will subprocess-call this with kwargs from tasks.py; main.py stays as-is.

Looks right?  [Y / n / edit]
```

On **edit**, take the correction (a flag rename, a missing flag, a different invoker like `uv run`). On **n**, ask the experimenter to share `python3 main.py --help` so the agent can re-propose.

### 3. Identify frozen YAML configs (the experiment-identity inputs)

Scan for YAMLs the user's frozen experiment depends on:

```bash
ls configs/*.yaml configs/*.yml conf/*.yaml 2>/dev/null
```

For each candidate, propose:

```
I see `configs/exp_42.yaml`. The convention is *one YAML = one frozen experiment*.
I'll hash its bytes and thread `exp_42_sha` into every task's kwargs so cmd_sha
covers the YAML's content — two submits of the same YAML dedup; an in-place edit
makes cmd_sha differ.

Treat `configs/exp_42.yaml` as a frozen config?  [Y / n / different file]
```

Collect the list of confirmed paths into `frozen_configs`. If none, skip.

> **Constraint**: `frozen_configs` requires a `task_generator` (Step 4). If the experimenter wants a hand-written `tasks.py`, they have to include the shas themselves; `frozen_configs` is rejected at intake otherwise.

### 4. Pick the scale-up axis (the task_generator)

The wrapper handles *one task*. The `task_generator` enumerates the **N tasks** to fan out. Common shapes:

| Shape | When to use | Example |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes (e.g. seed × data_shard) | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied list of N task dicts | `items=[{...}, {...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

Propose the shape, get confirmation, collect the params.

### 5. Pre-declare the DataAxis (optional but recommended)

Because the wrapper body is `subprocess.check_call`, `classify-axis` cannot introspect it later. If the experimenter knows the classification, declare it now:

```
Your `main.py` runs an independent training job per seed — each task is a pure
function of its kwargs (no carried state between tasks).

I'll classify as: DataAxis = Independent

Looks right?  [Y / n / unsure]
```

Use the same decision tree as `hpc-classify-axis`:
- *Does each row's result depend on rows computed before it?* No → **`independent`** (DOALL).
- *Is the carried state a fixed-size summary combinable in any order?* Yes → **`associative`** (pick `sum` / `moments`).
- *Is the dependence a bounded look-back (e.g. trailing N rows)?* Yes → **`bounded_halo`** with `halo.expr` over parameter names (e.g. `train_window * 48`).
- Otherwise / unsure → **`sequential`** (fail-safe default; serial is slow, not wrong).

On **unsure**, omit `data_axis_hint` from the spec — `classify-axis` will surface the boundary on submit and the operator can decide later.

### 6. Build the spec and invoke the `interview` primitive

Assemble an `InterviewSpec` JSON:

```json
{
  "goal": "<one-line goal from Step 0>",
  "task_count": <N from Step 4>,
  "produced_by": {"kind": "human", "operator": "<git user.name>"},
  "task_generator": { "kind": "...", "params": { ... } },
  "entry_point": {
    "kind": "shell_command",
    "run_name": "<chosen, valid Python identifier — e.g. 'forecast' or 'train'>",
    "argv": [ ... from Step 2 ... ],
    "signature": { ... from Step 2 ... },
    "frozen_configs": [ ... from Step 3 ... ],
    "data_axis_hint": { ... from Step 5 if declared, else omit ... }
  }
}
```

Write to `/tmp/interview_spec.json` and invoke:

```bash
hpc-agent interview --spec /tmp/interview_spec.json --campaign-dir .
```

On `ok=True`: the envelope reports the materialized artifacts (`.hpc/wrappers/<run_name>.py`, `tasks.py`, `interview.json`), `total_tasks`, and `cmd_sha`. On `error_code=spec_invalid`: surface the message — most often a typo (argv placeholder not in signature) or a missing frozen config — and re-elicit.

### 7. Confirm and hand off to `/submit-hpc`

Summarize the materialization for the experimenter:

```
Wrote:
  .hpc/wrappers/forecast.py        (the wrapper main.py runs through)
  tasks.py                         (100 tasks: configs/exp_42.yaml × seeds 0..99)
  interview.json                   (entry_point + materialized executor_cmd)

cmd_sha: 5ac46c384ebb3202 (covers config_sha + every task's kwargs)

Next: run `/submit-hpc` — the worker will read interview.json and use the
wrapper as the executor.
```

The submit workflow's Step 0b picks up `_materialized.entry_point` and threads `executor_cmd` into the submit-flow spec — no further setup needed.

## Notes

- **Idempotent.** Re-running with the same intent overwrites `interview.json` and the wrapper byte-equivalently (modulo `_materialized.at`). Editing the user's `main.py` flags requires re-running this skill to re-elicit `signature`.
- **Signature drift safety.** The wrapper's typed signature is what `validate-executor-signatures` checks at submit time. If `main.py`'s actual flags drift from the declared signature, the canary catches the argparse error (one task, not a hundred).
- **The wrapper IS the contract.** The framework reads the wrapper's signature, not `main.py`'s. If the wrapper says `seed: int` but `main.py` accepts `--seed-num`, framework pre-submit lints can't catch it; the canary will. Keep the wrapper in sync.
- **One frozen experiment per YAML.** Each `configs/exp_NN.yaml` is its own experiment with its own `cmd_sha`. To run a different frozen experiment, re-run this skill against the new YAML — the new `<stem>_sha` makes the framework correctly treat it as fresh, not a dedup of the prior one.
- **No `@register_run` notebook required.** This skill is the alternative entry point. A repo with *both* a notebook `@register_run` AND a generated wrapper is fine — the submit worker picks based on what `interview.json` declares.
