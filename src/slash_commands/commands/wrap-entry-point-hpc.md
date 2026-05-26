`/wrap-entry-point-hpc` is the **human-facing wrapper** around the `hpc-wrap-entry-point` skill.

The skill itself is agent-autonomous — given a partial `InterviewSpec`, it detects entry points, picks a pathway, applies edits, and materializes artifacts without `[Y/n]` prompts (a MARs experiment agent calls it with a `goal` + `task_generator` and lets the skill fill in the rest). This slash command sits *between* the user and the skill: it conducts the elicitation dialog up front, then invokes the skill with a fully-resolved spec.

Two on-ramps in one place: **greenfield repos** (no entry point yet) get a seed scaffold; **mature repos** (existing `main.py`, `train.py`, `python -m pkg.cli`, a compiled binary, ...) get walked through `@register_run` direct decoration as the default path, with the wrapper as the rescue boat. The framework contract being onboarded is described in [`docs/internals/experiment-contract.md`](../../docs/internals/experiment-contract.md).

`/submit-hpc` escalates with `mature_repo_needs_interview` on a cache miss; reasons to invoke standalone:

- **Greenfield**: bootstrap a new experiment repo with a `@register_run` seed (script or notebook) plus `tasks.py` + `interview.json`, ready for `/submit-hpc`.
- Pre-onboard a mature repo before its first `/submit-hpc`.
- Walk an experimenter through `@register_run` direct decoration when they're new to the framework.
- Refresh after editing the entry point's flags or adding a new frozen config.
- Switch the frozen experiment (`configs/exp_42.yaml` → `configs/exp_43.yaml`) without leaving stale state behind.

## Procedure (in-chat agent)

### 1. Detect or scaffold (greenfield branch)

Probe whether the repo has an entry-point file:

```bash
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null
ls run.sh launch.sh ./simulator 2>/dev/null
grep -rln '@register_run' notebooks/ src/ *.py 2>/dev/null | head
```

**If nothing matches** — greenfield. Ask the user (the skill's autonomous default would be `script`, but the human picks here):

```
I don't see an entry-point file (no main.py / train.py / __main__.py /
console_scripts / .ipynb-with-@register_run). I can scaffold one for
you. Two shapes — both produce a @register_run-decorated function the
framework can introspect; pick whichever matches where you are:

  [1] script   (default) — train.py with @register_run + argparse.
                Pick this when the work is already settled and you just
                want to scale it out.
  [2] notebook — notebooks/experiment.ipynb with @register_run.
                Pick this when you're still iterating literately —
                scratch cells, plots, smoke tests.

Which shape?  [1 / 2]
```

Record the chosen `shape` in the spec.

### 2. Disambiguate the entry point

If multiple Python entry points exist, ask the user:

```
I see three plausible entry points in this repo:
  1. `train.py`       — argparse with --config, --seed, --epochs
  2. `eval.py`        — argparse with --model, --dataset
  3. `python -m mypkg.cli`  — Click app with `train` / `eval` subcommands

Which one should the cluster run?  [1 / 2 / 3 / other]
```

Record the user's pick as `entry_point.path` in the spec. (Without this, the skill returns `ambiguous_entry_point`.)

### 3. Confirm the pathway: direct decoration vs. wrapper

The skill's decision table picks deterministically:

| Condition | Pathway |
|---|---|
| Python-importable function the user can edit | direct decoration (default) |
| Non-Python entry point, `@hydra.main`, consuming click/typer decorator, vendor code | wrapper fallback |

Surface the proposal to the user and let them override:

```
Your `train.py` parses --config and --seed from sys.argv via argparse. The
cleanest onboarding is `@register_run` direct decoration — a two-line edit:

  from hpc_agent import register_run

  @register_run
  def run(config: str, seed: int) -> None:
      # ... the body that used to live below argparse.parse_args() ...

  if __name__ == "__main__":
      # existing argparse block stays as-is, calls run(**vars(args))

The CLI still works; the framework now has a typed function to introspect.

Apply this edit?  [Y / n / show me first / use a wrapper instead]
```

On **n** or **wrapper**, switch to the wrapper path and propose the argv template + signature:

```
Wrapping `train.py` instead of editing it:

  argv:      python3 train.py --config {config} --seed {seed}
  signature: config: str, seed: int

Looks right?  [Y / n / edit]
```

On **edit**, take the correction (flag rename, missing flag, different invoker like `uv run`). Record the user's choices into the spec's `entry_point` block.

### 4. Identify frozen YAML configs

```bash
ls configs/*.yaml configs/*.yml conf/*.yaml 2>/dev/null
```

For each candidate, ask:

```
I see `configs/exp_42.yaml`. The convention is *one YAML = one frozen experiment*.
I'll hash its bytes and thread `exp_42_sha` into every task's kwargs so cmd_sha
covers the YAML's content.

Treat `configs/exp_42.yaml` as a frozen config?  [Y / n / different file]
```

Collect confirmed paths into `frozen_configs`. If the user says one of the YAMLs is *not* the experiment identity (e.g. it's a logger config), drop it.

### 5. Pick the scale-up axis (the `task_generator`)

The entry point handles *one task*. The `task_generator` enumerates the **N tasks** to fan out. Common shapes:

| Shape | When to use | Example |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes (e.g. seed × data_shard) | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied list of N task dicts | `items=[{...}, {...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

Propose the shape from context (e.g. one frozen config + the user said "100 seeds" → `items_x_seeds`), confirm, collect the params. **This step is mandatory** — the skill refuses without it.

### 6. Pre-declare the DataAxis hint (optional)

Walk the same decision tree as `/classify-axis-hpc` (`hpc_agent/experiment_kit/axis.py`):

- *Does each row's result depend on rows computed before it?* No → **`independent`** (DOALL).
- *Carried state combinable in any order?* Yes → **`associative`** (`sum` / `moments`).
- *Bounded look-back?* Yes → **`bounded_halo`** with `halo.expr` over parameter names.
- Otherwise / unsure → **`sequential`** (fail-safe default).

Propose with one sentence of reasoning:

```
Your `train.py` runs an independent training job per seed — each task is a pure
function of its kwargs (no carried state between tasks).

I'll classify as: DataAxis = Independent

Looks right?  [Y / n / unsure]
```

On **unsure**, omit `data_axis_hint` from the spec — `classify-axis` will surface the boundary on submit and the operator can decide later.

In the **direct-decoration path**, this hint is optional (the framework can introspect the function at submit time). In the **wrapper path**, the hint is load-bearing — the wrapper body is `subprocess.check_call`, opaque to `classify-axis`.

### 7. Invoke the `hpc-wrap-entry-point` skill

Assemble the spec from Steps 1-6 (the skill's input contract is documented in `skills/hpc-wrap-entry-point/SKILL.md` under "Inputs"). Invoke the skill via the Skill tool with the fully-resolved spec — every field the user confirmed is now pre-populated, so the skill's own elicitation paths short-circuit and it just executes:

- Apply the `@register_run` edit (direct-decoration path) or materialize the wrapper (fallback path)
- Detect frozen configs (or use the user-confirmed list)
- Build the `InterviewSpec` and invoke `hpc-agent interview`

On `ok=True`, the skill returns the materialized artifacts. Surface them to the user:

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

On `error_code: spec_invalid` — surface the message and loop back to the field the user can correct (most often: argv placeholder not in signature, missing frozen config, missing `task_generator`).

## Notes

- **Direct decoration is the default.** The wrapper is the rescue boat — non-Python entry points, decorator conflicts, read-only vendor code. A two-line code edit beats a subprocess shim whenever it's possible.
- **One YAML = one frozen experiment.** To run a different frozen pipeline, write `configs/exp_43.yaml` (don't edit `exp_42.yaml` in place) and re-run this command.
- **`/submit-hpc` is the next step.** After this command completes, run `/submit-hpc`; on the direct-decoration path the worker's normal `@register_run` discovery picks up the freshly decorated function, and on the wrapper path the worker reads `interview.json` and uses the materialized wrapper as the executor.
- **Backstop is the canary (wrapper path).** The wrapper is the framework's contract; if its declared signature drifts from the entry point's actual flags, the canary catches it (one failed task, not a hundred).
- **Why this slash exists.** The skill is autonomous (it detects, picks, edits, materializes without prompts). The slash exists so the human's intent — *which* entry point, *which* YAMLs are the experiment identity, *what* `task_generator` shape, *whether* to refuse direct decoration — overrides the autonomous defaults before the artifacts land.
