---
name: hpc-wrap-entry-point
description: "Onboard a mature repo (`main.py`, `train.py`, `python -m pkg.cli`, a compiled binary, ... — plus optional YAML configs) for hpc-agent submission. Detects the entry point, then leads the experimenter through `@register_run` direct decoration as the default path; falls back to materializing a wrapper at `.hpc/wrappers/<run_name>.py` only when direct decoration isn't possible (non-Python entry point, Hydra/argparse decorator conflict, vendor code). After either pathway, conducts the rest of the interview (task generator, frozen configs, data-axis hint) and invokes the `interview` primitive to persist `tasks.py` + `interview.json`. Run this once before `/submit-hpc` when the repo doesn't already have a `@register_run` notebook."
allowed-tools: Bash Read Edit Write Glob
execution: inline
category: experimenter-intent
---

Agent-facing composition over the **[interview](../../docs/primitives/interview.md) primitive** for mature repos. The greenfield path is `@register_run` on a Python function; this skill is the analog for repos that don't have one yet. **For Python repos with an importable entry-point function, the right move is to put `@register_run` directly on that function** — a two-line code edit. Only when direct decoration isn't possible (non-Python entry point, a CLI library's decorator conflicts with `@register_run`, vendor code the user can't touch, or the user explicitly prefers it) does the skill fall back to materializing a wrapper around a shell-invokable command. Either way it conducts the conversational intake the headless `/submit-hpc` worker can't (the worker reads `interview.json`; this skill writes it) and then hands off to `/submit-hpc`.

The interview persists, in either pathway:
- A `tasks.py` (from the supplied `task_generator`) whose kwargs include `<stem>_sha` for every frozen YAML, so `cmd_sha` correctly distinguishes `exp_42.yaml` from `exp_43.yaml` and catches accidental in-place edits.
- An `interview.json` recording the entry-point shape (a `register_run` pointer for the direct-decoration path; a `shell_command` block with the materialized wrapper for the fallback path).
- **Only in the fallback path**: a `@register_run` **wrapper** at `<experiment>/.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the user's entry point with kwargs substituted. The wrapper's typed signature is what downstream framework primitives (`classify-axis`, `validate-executor-signatures`) introspect. The underlying entry point stays untouched.

## When to run

- The user's repo has any non-notebook entry point — `main.py`, `train.py`, `run_experiment.py`, `python -m pkg.cli`, `./simulator`, etc. — and no `@register_run` decoration anywhere.
- The user wants to scale a *frozen* experiment configured by one YAML (or a small number of them) across seeds / shards / replicates — not sweep over the YAMLs themselves.
- A fresh `/submit-hpc` would escalate with `mature_repo_needs_interview` because the worker can't conduct the conversational intake itself.

## Steps

### 1. Detect the entry point

Walk the repo to propose a candidate entry point. Probe shapes in order of likelihood:

```bash
# Conventional Python entry-point filenames at the root or under src/
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null

# Python package with __main__.py (runnable via `python -m pkg`)
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5

# pyproject.toml console_scripts entry points (e.g. `mytool = mypkg.cli:main`)
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null

# Shell scripts / compiled binaries — only consider these when no Python entry exists
ls run.sh launch.sh ./simulator 2>/dev/null
```

For each candidate Python file, read it and inspect the CLI surface — `argparse.ArgumentParser`, `@click.command` / `@click.group`, `@app.command` (typer), `fire.Fire(...)`, `@hydra.main`, or a bare `if __name__ == "__main__":` block calling something with `sys.argv`. For a package with `__main__.py`, propose the invocation as `python3 -m <pkg>`. For a `console_scripts` entry, propose the registered command name directly.

If multiple candidates are plausible, surface them to the experimenter and let them pick:

```
I see three plausible entry points in this repo:
  1. `train.py`       — argparse with --config, --seed, --epochs
  2. `eval.py`        — argparse with --model, --dataset
  3. `python -m mypkg.cli`  — Click app with `train` / `eval` subcommands

Which one should the cluster run?  [1 / 2 / 3 / other]
```

Record the picked entry point — the path (or `-m` invocation), and which CLI library it uses. The next step decides which pathway to take.

### 2. Decide the pathway: direct decoration (default) vs. wrapper materialization (fallback)

**The default pathway is `@register_run` direct decoration on the user's existing function.** This is a two-line code edit — an import and a decorator — and it gives the framework full introspection on the real function, not a subprocess shim. Fall through to the wrapper-materialization path only when direct decoration genuinely can't work.

Decide:

| Condition | Pathway |
|---|---|
| Python-importable function the user owns and can edit — even one that currently parses `sys.argv` via argparse | **Step 3a** (direct decoration) |
| Non-Python entry point (shell script, compiled binary) | **Step 3b** (wrapper fallback) |
| CLI-library decorator on the entry point conflicts with `@register_run` (e.g. `@hydra.main` rewrites the signature, some `@click.command` shapes consume the function) | **Step 3b** (wrapper fallback) |
| Vendor code / read-only repo the user can't add a decorator to | **Step 3b** (wrapper fallback) |
| User explicitly prefers the wrapper (asks to keep `main.py` untouched) | **Step 3b** (wrapper fallback) |

When in doubt, prefer **Step 3a**: direct decoration is reversible (one line to remove), the wrapper is heavier. If the conflict turns out to be real, fall back to Step 3b.

### 3a. `@register_run` direct decoration (the default path)

Guide the user through the two-line code edit. The decorator goes on the function the framework should treat as the entry point — not on the `if __name__ == "__main__":` block, but on the function it ultimately calls.

**Common shape: argparse-driven `main()` reading `sys.argv`.** The `@register_run` contract is that the decorated function takes typed kwargs (`def run(config: str, seed: int) -> ...`), not `sys.argv`. Suggest factoring out an inner function and decorating *that*:

```
Your `train.py` parses --config and --seed from sys.argv via argparse, then calls
into the actual work. The framework wants a function it can call directly with
kwargs — so the cleanest edit is to factor the work into an inner function and
decorate that:

  # train.py
  from hpc_agent import register_run

  @register_run
  def run(config: str, seed: int) -> None:
      # ... the body that used to live below argparse.parse_args() ...
      ...

  if __name__ == "__main__":
      import argparse
      ap = argparse.ArgumentParser()
      ap.add_argument("--config", required=True)
      ap.add_argument("--seed", type=int, required=True)
      args = ap.parse_args()
      run(config=args.config, seed=args.seed)

The CLI still works (`python3 train.py --config exp_42.yaml --seed 0`); the
framework now has a typed function to introspect.

Apply this edit?  [Y / n / show me first]
```

**Shape: function already takes kwargs.** Even simpler — just add the import and the decorator:

```
Your `train.py` already has `def train(config, seed):` taking kwargs. Two-line edit:

  # train.py
  from hpc_agent import register_run

  @register_run
  def train(config: str, seed: int) -> None:
      ...

Apply this edit?  [Y / n]
```

On confirmation, use the `Edit` tool to add the import and decorator. Record the picked `run_name` (the function name) and proceed to Step 4 — **do not** invoke the `interview` primitive with a `shell_command` entry_point. The interview in Step 4 will carry an `entry_point.kind = "register_run"` (a pure pointer; no wrapper materialization), and the submit worker's Step 1 discovery picks up the freshly decorated function via the normal flow.

### 3b. Wrapper materialization (fallback path)

This is the rescue boat — use it when direct decoration isn't possible (see the Step 2 decision table). The existing wrapper machinery (`entry_point.kind = "shell_command"`) materializes a thin `@register_run` wrapper at `.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the entry-point argv with kwargs substituted in.

**3b.i: Propose the argv template + signature.** From the detected entry point's CLI surface, propose:

- An `argv` template list — the shell command with `{placeholder}` for each kwarg the wrapper will pass through. The first element is the invocation form for the entry-point shape:
  - File-based Python script → `["python3", "train.py", ...]`
  - Package module → `["python3", "-m", "mypkg.cli", ...]`
  - Installed console script → `["mytool", ...]`
  - Shell script / binary → `["./run.sh", ...]` or `["./simulator", ...]`
- A `signature` dict mapping each `{placeholder}` to a Python type (`str` / `int` / `float` / `bool`) — derive from argparse `type=int`, click `IntType`, typer annotations, etc.

Show the proposal to the experimenter with one sentence of reasoning. Concrete shape depends on what was detected; for a Python script with argparse:

```
Your `train.py` takes `--config PATH` and `--seed INT`. Direct `@register_run`
decoration isn't a clean fit here (vendor code / Hydra conflict / you'd rather
not touch main.py), so I'll wrap it as:

  argv:      python3 train.py --config {config} --seed {seed}
  signature: config: str, seed: int

The wrapper will subprocess-call this with kwargs from tasks.py; train.py stays as-is.

Looks right?  [Y / n / edit]
```

On **edit**, take the correction (a flag rename, a missing flag, a different invoker like `uv run`, a different entry point file). On **n**, ask the experimenter to share `<entry-point> --help` so the agent can re-propose.

The wrapper's typed signature is what downstream `validate-executor-signatures` checks; the underlying entry point stays opaque to the framework. The submit workflow's Step 0b picks up `_materialized.entry_point` (with `kind == "shell_command"`) and uses `executor_cmd` as the job's `EXECUTOR`.

### 4. Identify frozen YAML configs (the experiment-identity inputs)

Regardless of pathway, scan for YAMLs the user's frozen experiment depends on:

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

> **Constraint**: `frozen_configs` requires a `task_generator` (Step 5). If the experimenter wants a hand-written `tasks.py`, they have to include the shas themselves; `frozen_configs` is rejected at intake otherwise.

### 5. Pick the scale-up axis (the task_generator)

The entry point handles *one task*. The `task_generator` enumerates the **N tasks** to fan out. Common shapes:

| Shape | When to use | Example |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes (e.g. seed × data_shard) | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied list of N task dicts | `items=[{...}, {...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

Propose the shape, get confirmation, collect the params.

### 6. Pre-declare the DataAxis (optional)

In the **direct-decoration path (3a)**, `classify-axis` can introspect the decorated function directly — pre-declaring is optional and usually unnecessary, since the framework will infer the axis from the function body.

In the **wrapper path (3b)**, the wrapper body is `subprocess.check_call`, so `classify-axis` cannot introspect it later. If the experimenter knows the classification, declaring it now saves an escalation at submit time:

```
Your `train.py` runs an independent training job per seed — each task is a pure
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

### 7. Build the spec and invoke the `interview` primitive

Assemble an `InterviewSpec` JSON. The `entry_point` block differs by pathway:

**Direct-decoration path (3a):**

```json
{
  "goal": "<one-line goal from Step 0>",
  "task_count": <N from Step 5>,
  "produced_by": {"kind": "human", "operator": "<git user.name>"},
  "task_generator": { "kind": "...", "params": { ... } },
  "entry_point": {
    "kind": "register_run",
    "run_name": "<the function name you decorated in Step 3a>"
  }
}
```

**Wrapper path (3b):**

```json
{
  "goal": "<one-line goal from Step 0>",
  "task_count": <N from Step 5>,
  "produced_by": {"kind": "human", "operator": "<git user.name>"},
  "task_generator": { "kind": "...", "params": { ... } },
  "entry_point": {
    "kind": "shell_command",
    "run_name": "<chosen, valid Python identifier — e.g. 'forecast' or 'train'>",
    "argv": [ ... from Step 3b.i ... ],
    "signature": { ... from Step 3b.i ... },
    "frozen_configs": [ ... from Step 4 ... ],
    "data_axis_hint": { ... from Step 6 if declared, else omit ... }
  }
}
```

Write to `/tmp/interview_spec.json` and invoke:

```bash
hpc-agent interview --spec /tmp/interview_spec.json --campaign-dir .
```

On `ok=True`: the envelope reports the materialized artifacts (`tasks.py`, `interview.json`, plus `.hpc/wrappers/<run_name>.py` only on the wrapper path), `total_tasks`, and `cmd_sha`. On `error_code=spec_invalid`: surface the message — most often a typo (argv placeholder not in signature) or a missing frozen config — and re-elicit.

### 8. Confirm and hand off to `/submit-hpc`

Summarize the materialization for the experimenter.

**Direct-decoration path:**

```
Edited:
  train.py                         (added `from hpc_agent import register_run` + decorator)

Wrote:
  tasks.py                         (100 tasks: configs/exp_42.yaml × seeds 0..99)
  interview.json                   (entry_point pointer: register_run/train)

cmd_sha: 5ac46c384ebb3202 (covers config_sha + every task's kwargs)

Next: run `/submit-hpc` — the worker discovers `@register_run def train(...)`
and uses it as the executor.
```

**Wrapper path:**

```
Wrote:
  .hpc/wrappers/forecast.py        (the wrapper the entry point runs through)
  tasks.py                         (100 tasks: configs/exp_42.yaml × seeds 0..99)
  interview.json                   (entry_point + materialized executor_cmd)

cmd_sha: 5ac46c384ebb3202 (covers config_sha + every task's kwargs)

Next: run `/submit-hpc` — the worker will read interview.json and use the
wrapper as the executor.
```

The submit workflow's Step 0b picks up `_materialized.entry_point` and threads `executor_cmd` into the submit-flow spec (wrapper path) or runs its normal `@register_run` discovery (direct-decoration path) — no further setup needed.

## Notes

- **Direct decoration is the default; the wrapper is a rescue boat.** A two-line code edit beats a subprocess shim whenever it's possible. The wrapper is for non-Python entry points, decorator conflicts, and read-only vendor code.
- **Idempotent.** Re-running with the same intent overwrites `interview.json` (and, on the wrapper path, the wrapper file) byte-equivalently (modulo `_materialized.at`). Editing the underlying entry point's flags requires re-running this skill — for direct decoration to update the decorated function's kwargs; for the wrapper to re-elicit `signature`.
- **Signature drift safety (wrapper path).** The wrapper's typed signature is what `validate-executor-signatures` checks at submit time. If the entry point's actual flags drift from the declared signature, the canary catches the argparse / CLI error (one task, not a hundred).
- **The wrapper IS the contract (wrapper path).** The framework reads the wrapper's signature, not the entry point's. If the wrapper says `seed: int` but the entry point accepts `--seed-num`, framework pre-submit lints can't catch it; the canary will. Keep the wrapper in sync.
- **One frozen experiment per YAML.** Each `configs/exp_NN.yaml` is its own experiment with its own `cmd_sha`. To run a different frozen experiment, re-run this skill against the new YAML — the new `<stem>_sha` makes the framework correctly treat it as fresh, not a dedup of the prior one.
- **A repo can mix pathways.** A repo with *both* a notebook `@register_run` AND a generated wrapper is fine — the submit worker picks based on what `interview.json` declares.
