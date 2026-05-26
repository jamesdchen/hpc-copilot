`/submit-hpc` triggers the **submit** workflow — submit a parameter-grid experiment to an HPC cluster.

This command is a thin trigger over `hpc-agent run`, the code-orchestrated entrypoint. Do not run the `hpc-submit` skill, and do not perform the workflow steps yourself in this conversation — the workflow runs in a fresh-context worker.

The submit workflow accepts two experiment shapes:

- **`@register_run`-decorated Python function** (the canonical input) — whether it lives in a notebook (`notebooks/<name>.ipynb`) or a `.py` file (`train.py`, `main.py`), the framework discovers it, introspects its signature, and classifies its parallel axis. No setup needed before invoking `/submit-hpc`. For a mature repo with an existing entry-point function, the right onboarding is to put `@register_run` directly on that function — a two-line code edit (an import and a decorator). The escalation playbook below walks the user through this when the worker can't proceed without it.
- **Mature repo with a non-Python or decorator-conflicting entry point** (`python -m pkg.cli` you can't edit, a compiled binary, a `@hydra.main`-wrapped function whose signature `@register_run` can't see through) — the fallback path is to materialize a `@register_run` wrapper at `.hpc/wrappers/<run_name>.py` that subprocess-invokes the real entry point. The playbook covers this too.

## Driving the workflow

1. Structure the user's request into a JSON object `<fields>` — the run or entry point to submit, plus any explicit choices they stated (`cluster`, `--no-canary`, `campaign_id`). No up-front interview is needed; pass whatever the user gave.
2. Run, via the `Bash` tool: `hpc-agent run submit --fields-json '<fields>'`. It validates the fields, generates the canonical worker prompt by code, and spawns a fresh-context worker that executes the `hpc-submit` skill. It prints a JSON envelope.
3. Surface to the user: `data.report.result` (run id, job ids, grid dimensions, verified scheduler state), `data.report.decisions` (each decision point the worker reached and why), and `data.report.anomalies`.
4. If a decision is an **escalation** — the worker needs an input only a human can give — consult the playbook below for the matching escalation code, gather the resolved fields, add them to `<fields>`, and re-run `hpc-agent run submit`. A fresh, unscaffolded experiment may take two round-trips.

## Escalation playbook

The worker can't actuate human authority. When it surfaces an escalation, the in-chat agent runs the matching dialog below, then invokes the relevant skill via the Skill tool with a fully-resolved spec, then re-invokes `/submit-hpc`. Skills are agent-autonomous (no `[Y/n]` in their bodies) — the dialogs that used to live in the now-deleted paired slashes are consolidated here.

### `mature_repo_needs_interview` — onboard the entry point

"No `@register_run` decoration anywhere AND no `interview.json`." Walk the user through `hpc-wrap-entry-point` (see `skills/hpc-wrap-entry-point/SKILL.md`).

**Step A. Detect or scaffold.** Probe whether the repo has an entry-point file:

```bash
ls main.py train.py run.py experiment.py 2>/dev/null
ls src/main.py src/train.py src/run.py 2>/dev/null
find . -maxdepth 4 -name __main__.py -not -path '*/.*' 2>/dev/null | head -5
test -f pyproject.toml && grep -A1 '\[project.scripts\]' pyproject.toml 2>/dev/null
ls run.sh launch.sh ./simulator 2>/dev/null
grep -rln '@register_run' notebooks/ src/ *.py 2>/dev/null | head
```

If nothing matches, greenfield — ask the user:

```
I don't see an entry-point file. I can scaffold one. Two shapes:
  [1] script   (default) — train.py with @register_run + argparse.
  [2] notebook — notebooks/experiment.ipynb with @register_run.
Which shape?  [1 / 2]
```

**Step B. Disambiguate entry point** if multiple candidates exist:

```
I see three plausible entry points in this repo:
  1. `train.py`       — argparse with --config, --seed, --epochs
  2. `eval.py`        — argparse with --model, --dataset
  3. `python -m mypkg.cli`  — Click app with `train` / `eval` subcommands

Which one should the cluster run?  [1 / 2 / 3 / other]
```

**Step C. Pathway: direct decoration vs. wrapper.** Direct decoration is the default — a two-line code edit (import + `@register_run`). Fall back to wrapper only when blocked (non-Python entry point, `@hydra.main`, consuming click/typer decorator, vendor code).

```
Your `train.py` parses --config and --seed from sys.argv via argparse. The
cleanest onboarding is `@register_run` direct decoration — a two-line edit:

  from hpc_agent import register_run

  @register_run
  def run(config: str, seed: int) -> None:
      # ... the body that used to live below argparse.parse_args() ...

  if __name__ == "__main__":
      # existing argparse block stays as-is, calls run(**vars(args))

Apply this edit?  [Y / n / show me first / use a wrapper instead]
```

On wrapper, propose argv template + signature; let the user edit.

**Step D. Frozen YAML configs.** For each `configs/*.yaml`, ask: "Treat as frozen experiment config? [Y / n / different file]". Collected into `frozen_configs`.

**Step E. Pick `task_generator`** (mandatory — skill refuses without it):

| Shape | When | Example |
|---|---|---|
| `items_x_seeds` | One frozen config × N seeds | `items=[{config: "exp_42.yaml"}], seeds=[0..99]` |
| `cartesian_product` | Cross a few axes | `axes={seed: [0..9], shard: [0..3]}` |
| `enumerated` | Hand-supplied N task dicts | `items=[{...}, ...]` |
| `numeric_linspace` / `numeric_logspace` | Sweep one numeric hyperparameter | `param="lr", low, high, n` |

**Step F. Invoke the skill.** Assemble the full `InterviewSpec`, invoke `hpc-wrap-entry-point` via the Skill tool with the resolved spec. The skill materializes `tasks.py` + `interview.json` (+ the wrapper on the fallback path), then return to Step 4 of the main flow.

### `axis_unclassified` — classify a `@register_run`'s data axis

"The run has no `DataAxis` in `axes.yaml`, and the signature changed since the last classification." Walk the user through `hpc-classify-axis` (see `skills/hpc-classify-axis/SKILL.md`).

**Step A. Discover and cache check.**

```bash
python .hpc/scaffold.py discover
```

Read `.hpc/axes.yaml`. If `executors.<run_name>.run_signature_sha` matches the current sha, report the cached classification and skip the rest — no interview, no skill invocation.

**Step B. Walk the decision tree** against `run()`'s source. The single question (from `hpc_agent/experiment_kit/axis.py`): *is there carried state across the series, and is its transition associative?*

1. Each row independent of prior rows? → **`Independent`** (DOALL).
2. Carried state combinable in any order (sum / moments)? → **`Associative`** with monoid `sum` or `moments` (default `moments`).
3. Bounded look-back (rolling window of N rows)? → **`BoundedHalo`** with `halo.expr` over parameter names, e.g. `train_window * 48`. Bias the halo **large** — over-wide is wasteful, too-small is silent corruption.
4. Otherwise / unsure → **`Sequential`** (fail-safe; serial is slow, not wrong).

Halo expression syntax: bare parameter names, numeric literals, `+ - * //`, `min()` / `max()`. Never `eval()`'d.

**Step C. Propose, then confirm:**

```
Your run `forecast` iterates an 8760-row hourly series. The loop refits
the model on a trailing `train_window`-day window each step — a bounded
look-back. I'll classify it as:

  DataAxis = BoundedHalo,  halo = train_window * 48

Looks right?  [Y / n / unsure]
```

On **n**, take the correction. On **unsure**, fall back to `Sequential`.

**Step D. Invoke the skill** with the resolved `data_axis` and `classified_by: "interview"`. The skill records into `.hpc/axes.yaml`'s `executors` block.

### `no_axes_yaml` — initialize scheduling axes (homogeneity)

"`tasks.py` exists but `.hpc/axes.yaml` doesn't, so the framework can't pick a parallelism axis automatically." Walk the user through `hpc-build-executor`'s axes-init companion (see `skills/hpc-build-executor/SKILL.md`).

**Step A. Read `tasks.py` and enumerate parallel axes.** Identify each named dimension; count cardinality.

**Step B. Classify each axis as homogeneous or not** by heuristic:

- Replicates / seeds / folds / CV windows / backtest windows → typically **homogeneous** (same compute, different data).
- Model class / architecture / algorithm → typically **heterogeneous**.
- Data type / dataset → depends on dataset sizes; often mildly heterogeneous.
- Hyperparameter sweeps → depends; LR rarely changes cost, layer count usually does.

**Step C. Propose, then confirm:**

```
Found these parallel axes in your experiment:
 • `window` (20 values) — homogeneous (same model trained on a 6-month rolling window)
 • `model` (4 values) — heterogeneous (linear / ridge / xgboost / neural_net have very different runtimes)
 • `data_type` (3 values) — heterogeneous (equities are 10x larger than fx)

I'll write `.hpc/axes.yaml` with `homogeneous_axes: [window]`.

Looks right? [Y/n]
```

On **n**, abort. On **Y**, invoke `hpc-build-executor` via the Skill tool with the resolved `homogeneous_axes` list. If `axes.yaml` already exists, the primitive returns `wrote: false`; re-prompt for `--force`.

### `ambiguous_run`, `ambiguous_entry_point` — the skill refused to pick

The agent-autonomous skills refuse rather than silently choose across `main.py` / `train.py` / `run.py` or across multiple `@register_run` functions. Ask the user to pick from the candidates the envelope lists, then re-invoke the skill with the resolved `run_name` / `entry_point.path`.

### Other escalations

- **Cluster pick** — ask the user which cluster (the envelope lists the configured names from `clusters.yaml`).
- **`--no-canary` confirmation** — ask the user; the canary is the default safety net.
- **`campaign_id` collision** — ask whether to overwrite, resume, or rename.

## Notes

- After resolving any escalation, return to Step 4 of the main flow — re-invoke `hpc-agent run submit` with the augmented `<fields>`. A fresh, unscaffolded experiment typically takes two round-trips (intake → resolve → re-submit).
- The skills (`hpc-wrap-entry-point`, `hpc-classify-axis`, `hpc-build-executor`) are agent-autonomous. The dialogs above gather what only the human knows; the skills do the deterministic work after.
