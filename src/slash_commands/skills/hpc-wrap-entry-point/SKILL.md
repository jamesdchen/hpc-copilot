---
name: hpc-wrap-entry-point
description: "Onboard a repo for hpc-agent submission, autonomously. Given a partial `InterviewSpec` (goal + task_generator are required from the caller; everything else is detected from the repo), the skill: (a) detects an existing entry point or scaffolds one via `build-template --shape {script,notebook}` for greenfield repos, (b) prefers `@register_run` direct decoration on the user's existing function and falls back to materializing a wrapper at `.hpc/wrappers/<run_name>.py` only when direct decoration is structurally blocked (non-Python entry point, `@hydra.main` signature rewrite, vendor code), (c) detects frozen YAML configs by convention, (d) walks the data-axis decision tree, (e) invokes the `interview` primitive to persist `tasks.py` + `interview.json`. No `[Y/n]` prompts — every choice point has a deterministic resolution. Human-driven callers (the `/wrap-entry-point-hpc` slash) gather intent from the user *first* and pass a fully-resolved spec; the skill records what it was given."
allowed-tools: Bash Read Edit Write Glob
execution: inline
category: agent-autonomous
---

Agent-facing composition over the **[interview](../../docs/primitives/interview.md) primitive**. Autonomous mode: given a partial `InterviewSpec`, fill in the rest from repo inspection, apply edits, materialize artifacts. The slash command consumer (`/wrap-entry-point-hpc`) elicits the same fields from the human first and passes a fully-resolved spec — in that mode the skill just records.

The skill persists, in either pathway:
- A `tasks.py` (from the supplied `task_generator`) whose kwargs include `<stem>_sha` for every frozen YAML, so `cmd_sha` correctly distinguishes `exp_42.yaml` from `exp_43.yaml` and catches accidental in-place edits.
- An `interview.json` recording the entry-point shape (a `register_run` pointer for the direct-decoration path; a `shell_command` block with the materialized wrapper for the fallback path).
- **Only in the fallback path**: a `@register_run` **wrapper** at `<experiment>/.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the user's entry point with kwargs substituted. The wrapper's typed signature is what downstream framework primitives (`classify-axis`, `validate-executor-signatures`) introspect. The underlying entry point stays untouched.

## Inputs

Caller-supplied (the skill refuses with `spec_invalid` if these are absent):

| Field | Why the caller has to supply it |
|---|---|
| `goal` | One-line free-text intent — the skill cannot invent it. |
| `task_generator` | The shape of the scale-up axis (`items_x_seeds`, `cartesian_product`, `enumerated`, `numeric_linspace`/`logspace`) plus its params. Cannot be inferred from the repo. |

Caller may pre-resolve to skip detection:

| Field | Skill's default if absent |
|---|---|
| `entry_point.kind` (`register_run` \| `shell_command`) | Detected by Step 1 + decided by Step 2 |
| `entry_point.path` / `run_name` | Highest-likelihood candidate from Step 1's probes |
| `shape` (`script` \| `notebook`, greenfield only) | `script` (the dominant case at scale-up time) |
| `argv` template + `signature` (wrapper path only) | Derived from the entry point's CLI surface |
| `frozen_configs` | All `configs/*.yaml` / `configs/*.yml` / `conf/*.yaml` detected by Step 4 |
| `data_axis_hint` | Walked from the decision tree (Step 6); ambiguous → omitted (the framework re-asks at submit) |

## When to run

- The user's repo has any non-notebook entry point — `main.py`, `train.py`, `run_experiment.py`, `python -m pkg.cli`, `./simulator`, etc. — and no `@register_run` decoration anywhere.
- **The repo is greenfield** — no entry point yet — and the caller wants a seed scaffold.
- A fresh `/submit-hpc` escalated with `mature_repo_needs_interview`.

## Steps

### 0. Detect or scaffold an entry point (greenfield branch)

Probe whether the repo already has an entry-point file:

```bash
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null
ls run.sh launch.sh ./simulator 2>/dev/null
grep -rln '@register_run' notebooks/ src/ *.py 2>/dev/null | head
```

**If anything matches** — at least one file is a plausible entry point or a `@register_run` is already on disk — skip to Step 1.

**If nothing matches** — this is a greenfield repo. Use the caller-supplied `shape` (or default to `script`) and scaffold:

```bash
hpc-agent build-template --repo-dir . --shape script    # or --shape notebook
```

The primitive injects the chosen seed file (`train.py` at repo root or `notebooks/experiment.ipynb`) alongside the framework-owned `.hpc/` assets. Then proceed through Step 1 onwards against the freshly scaffolded file.

### 1. Detect the entry point

Walk the repo to identify candidates, in this order (the order encodes likelihood — a tie-break for autonomous selection):

```bash
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null
ls run.sh launch.sh ./simulator 2>/dev/null
```

For each candidate Python file, inspect the CLI surface — `argparse.ArgumentParser`, `@click.command` / `@click.group`, `@app.command` (typer), `fire.Fire(...)`, `@hydra.main`, or a bare `if __name__ == "__main__":` block calling something with `sys.argv`. For a package with `__main__.py`, the invocation is `python3 -m <pkg>`. For a `console_scripts` entry, the registered command name.

**Autonomous resolution**: pick the first candidate that matched a probe (probes are ordered by likelihood). If the caller supplied `entry_point.path`, that overrides detection.

**Multiple-candidates contract**: if multiple Python entry points exist *and* the caller did not supply `entry_point.path`, **return `spec_invalid` with `error_code: ambiguous_entry_point`** listing the candidates. The skill picks across `main.py` / `train.py` / `run.py` only when one of them is the unique match.

Record the picked entry point — the path (or `-m` invocation), and which CLI library it uses.

### 2. Decide the pathway: direct decoration (default) vs. wrapper materialization (fallback)

**The default pathway is `@register_run` direct decoration on the user's existing function.** Fall through to wrapper materialization only when direct decoration is structurally blocked.

Deterministic decision table:

| Condition | Pathway |
|---|---|
| Python-importable function the caller can edit — even one that currently parses `sys.argv` via argparse | **Step 3a** (direct decoration) |
| Non-Python entry point (shell script, compiled binary) | **Step 3b** (wrapper fallback) |
| `@hydra.main` on the entry point (rewrites the signature; `@register_run` cannot see through it) | **Step 3b** (wrapper fallback) |
| `@click.command` / `@app.command` that consumes the function (typer/click decorator forms that swap the callable) | **Step 3b** (wrapper fallback) |
| Caller's spec sets `entry_point.kind = "shell_command"` explicitly | **Step 3b** (wrapper fallback) |

`@click.command` and Typer commands are auto-detected by reading the decorator stack. When the decorator stack is `[@register_run, @click.command]`-compatible (click leaves the underlying callable intact for some shapes), 3a is still safe; only the consuming forms force 3b.

### 3a. `@register_run` direct decoration (the default path)

Apply the two-line edit autonomously. The decorator goes on the function the framework should treat as the entry point — not on the `if __name__ == "__main__":` block, but on the function it ultimately calls.

**Common shape: argparse-driven `main()` reading `sys.argv`.** Factor out an inner function and decorate that, keeping the existing argparse block intact so the CLI still works:

```python
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
```

**Shape: function already takes kwargs.** Just add the import and the decorator — no refactor.

Apply the edit via the `Edit` tool (autonomous; no confirmation). Record the picked `run_name` (the function name) and proceed to Step 4. The interview in Step 7 will carry `entry_point.kind = "register_run"`; the submit worker's Step 1 discovery picks up the freshly decorated function via the normal flow.

### 3b. Wrapper materialization (fallback path)

The rescue boat. The existing wrapper machinery (`entry_point.kind = "shell_command"`) materializes a thin `@register_run` wrapper at `.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the entry-point argv with kwargs substituted in.

**3b.i: Derive the argv template + signature autonomously.** From the detected entry point's CLI surface:

- `argv` template — the shell command with `{placeholder}` for each kwarg. First element by entry-point shape:
  - File-based Python script → `["python3", "train.py", ...]`
  - Package module → `["python3", "-m", "mypkg.cli", ...]`
  - Installed console script → `["mytool", ...]`
  - Shell script / binary → `["./run.sh", ...]` or `["./simulator", ...]`
- `signature` — `{placeholder: type}` derived from argparse `type=int`, click `IntType`, typer annotations, etc. Default to `str` when the source declares no type.

When the caller supplied `argv` / `signature` in the spec, use those instead of deriving (this is how the slash command passes through user-confirmed overrides).

The wrapper's typed signature is what downstream `validate-executor-signatures` checks; the underlying entry point stays opaque to the framework. The submit workflow's Step 0b picks up `_materialized.entry_point` (with `kind == "shell_command"`) and uses `executor_cmd` as the job's `EXECUTOR`.

### 4. Detect frozen YAML configs

Scan for YAMLs by convention:

```bash
ls configs/*.yaml configs/*.yml conf/*.yaml 2>/dev/null
```

Every match is added to `frozen_configs`. The convention is *one YAML = one frozen experiment* — the framework hashes each file's bytes and threads `<stem>_sha` into every task's kwargs so `cmd_sha` covers the YAML's content. Two submits of the same YAML dedup; an in-place edit makes `cmd_sha` differ.

When the caller supplied `frozen_configs` in the spec, use those instead — slash callers can drop a config the user said is not the experiment's identity.

> **Constraint**: `frozen_configs` requires a `task_generator` (caller-supplied; Step 5). If the experimenter wants a hand-written `tasks.py`, they have to include the shas themselves; `frozen_configs` is rejected at intake otherwise.

### 5. `task_generator` is caller-supplied (no autonomous derivation)

The entry point handles *one task*. The `task_generator` enumerates the **N tasks** to fan out. Common shapes:

| Shape | When to use | Params |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied list of N task dicts | `items=[{...}, {...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

The skill does **not** invent a `task_generator` — refuse with `spec_invalid` if absent. (The slash command elicits this from the user; MARs supplies it explicitly.)

### 6. Pre-declare the DataAxis hint (autonomous tree walk)

In the **direct-decoration path (3a)**, `classify-axis` can introspect the decorated function directly later — pre-declaring is optional, since the framework will infer the axis from the function body at submit time. The skill can still pre-fill the hint to short-circuit one round-trip.

In the **wrapper path (3b)**, the wrapper body is `subprocess.check_call`, so `classify-axis` cannot introspect it later. The hint here is load-bearing.

Apply the same decision tree as `hpc-classify-axis` Step 4 (autonomous; no confirmation):

- *Does each row's result depend on rows computed before it?* No → **`independent`** (DOALL).
- *Is the carried state a fixed-size summary combinable in any order?* Yes → **`associative`** (pick `sum` / `moments`).
- *Is the dependence a bounded look-back (e.g. trailing N rows)?* Yes → **`bounded_halo`** with `halo.expr` over parameter names.
- Otherwise / ambiguous → **`sequential`** (fail-safe default; serial is slow, not wrong).

When the tree resolves to ambiguous, **omit** `data_axis_hint` from the spec — `classify-axis` will surface the boundary at submit time and the caller can resolve it then. (Sequential as a default is correct for `hpc-classify-axis` recording; here we leave the field absent so the framework still has a chance to interview.)

### 7. Build the spec and invoke the `interview` primitive

Assemble the `InterviewSpec` JSON. The `entry_point` block differs by pathway:

**Direct-decoration path (3a):**

```json
{
  "goal": "<caller-supplied>",
  "task_count": <N from the resolved task_generator>,
  "produced_by": {"kind": "human", "operator": "<git user.name>"},
  "task_generator": { "kind": "...", "params": { ... } },
  "entry_point": {
    "kind": "register_run",
    "run_name": "<the function name decorated in Step 3a>"
  }
}
```

**Wrapper path (3b):**

```json
{
  "goal": "<caller-supplied>",
  "task_count": <N from the resolved task_generator>,
  "produced_by": {"kind": "human", "operator": "<git user.name>"},
  "task_generator": { "kind": "...", "params": { ... } },
  "entry_point": {
    "kind": "shell_command",
    "run_name": "<chosen, valid Python identifier — e.g. 'forecast' or 'train'>",
    "argv": [ ... from Step 3b.i ... ],
    "signature": { ... from Step 3b.i ... },
    "frozen_configs": [ ... from Step 4 ... ],
    "data_axis_hint": { ... from Step 6 if resolved, else omit ... }
  }
}
```

Write to `/tmp/interview_spec.json` and invoke:

```bash
hpc-agent interview --spec /tmp/interview_spec.json --campaign-dir .
```

On `ok=True`: the envelope reports the materialized artifacts (`tasks.py`, `interview.json`, plus `.hpc/wrappers/<run_name>.py` only on the wrapper path), `total_tasks`, and `cmd_sha`. On `error_code=spec_invalid`: surface the message to the caller — most often a typo (argv placeholder not in signature) or a missing frozen config. The skill does not loop on its own; the caller (slash or MARs) decides whether to re-supply.

### 8. Return the materialization summary

Return to the caller (no in-chat prose):
- `entry_point.kind` (`register_run` or `shell_command`)
- `run_name`
- `tasks_py_path`, `interview_json_path` (and `wrapper_path` on 3b)
- `total_tasks`, `cmd_sha`
- The list of files edited (Step 3a only)

The submit workflow's Step 0b picks up `_materialized.entry_point` and threads `executor_cmd` into the submit-flow spec (wrapper path) or runs its normal `@register_run` discovery (direct-decoration path) — no further setup needed.

## Notes

- **Two on-ramps, one contract.** Greenfield repos scaffold an entry point via `build-template --shape {script,notebook}` (Step 0); mature repos onboard the existing one (Steps 1+). Both paths end in the same place: a `@register_run`-decorated function on disk plus a materialized `tasks.py` + `interview.json`. The canonical description of the contract is `docs/internals/experiment-contract.md`.
- **Direct decoration is the default; the wrapper is a rescue boat.** A two-line code edit beats a subprocess shim whenever it's possible. The wrapper is for non-Python entry points, decorator conflicts, and read-only vendor code.
- **Idempotent.** Re-running with the same intent overwrites `interview.json` (and, on the wrapper path, the wrapper file) byte-equivalently (modulo `_materialized.at`). Editing the underlying entry point's flags requires re-running this skill.
- **Signature drift safety (wrapper path).** The wrapper's typed signature is what `validate-executor-signatures` checks at submit time. If the entry point's actual flags drift from the declared signature, the canary catches the argparse / CLI error (one task, not a hundred).
- **The wrapper IS the contract (wrapper path).** The framework reads the wrapper's signature, not the entry point's. Keep the wrapper in sync.
- **One frozen experiment per YAML.** Each `configs/exp_NN.yaml` is its own experiment with its own `cmd_sha`. To run a different frozen experiment, re-run this skill against the new YAML.
- **Ambiguity escalates, never auto-resolves silently.** Multiple Python entry points without a caller pick → `ambiguous_entry_point`. The skill refuses to guess across `main.py` / `train.py` / `run.py` when more than one matched.
