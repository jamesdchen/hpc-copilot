# The hpc-agent experiment contract

This page is the canonical answer to "what is an hpc-agent experiment?"
Every other doc that talks about "the notebook" or "the script" is a
specific shape of the same underlying thing described here.

## What is an experiment?

An experiment is a `@register_run`-decorated Python function with typed
kwargs:

```python
from hpc_agent.experiment_kit import register_run

@register_run
def run(seed: int, alpha: float) -> dict:
    return {"score": alpha, "seed": seed}
```

The framework's contract is **that function** — nothing else. Everything
downstream (`discover_runs`, `classify-axis`, `validate-executor-signatures`,
the dispatcher) introspects the function's signature; it does not care
where the function lives or how it got there.

## Where can the function live?

Three peer locations. The framework treats them identically —
`discover_runs` AST-walks all three (`src/hpc_agent/experiment_kit/discover.py`
recursively reads `*.py` and `*.ipynb` files under the scan root,
skipping `.hpc/`):

| Location | Shape | When |
|---|---|---|
| `notebooks/<name>.ipynb` | Literate, iteration-phase | Author the function alongside scratch cells, plots, smoke tests. The notebook *is* the source of truth. |
| `train.py` (or `<name>.py` at the repo root or under `src/`) | Already-finalized executor | The function is settled; you don't need a notebook around it. |
| `mypkg/runner.py` (a package module) | Imported by other code | The function lives inside a larger package that already exists. |

Pick whichever matches the maturity of the experiment. There is no
"right" location; the framework reads them all the same way.

## Notebook-specific step: `export-package`

The cluster runs a stdlib-only `.py` executor — no `.ipynb` import on
the compute node. For notebook-shaped repos, `hpc-agent export-package`
converts `notebooks/<name>.ipynb` → `src/<name>.py` (strict-AST for
`@register_run` executors, `# export`-marker for pipeline libraries),
content-hash-caches against `.hpc/.build-cache.json`, and ships the
built `src/` in the rsync bundle.

**For repos whose entry point is already a `.py` script, this step is a
no-op** — there is no notebook to export. The rest of the pipeline
(`tasks.py`, `cli.py`, the dispatcher) is unchanged.

## The `tasks.py` contract is unchanged

Regardless of where the function lives, the fan-out shape is declared
in `.hpc/tasks.py`:

```python
FLAGS: dict[str, list[Flag]]    # per-executor argparse declarations
def total() -> int               # how many tasks
def resolve(i: int) -> dict      # kwargs for task #i
```

`tasks.py` is the dispatch boundary; it has no opinion on whether the
target function was authored in a notebook or a script.

## Fallback for non-Python entry points

When the entry point is a **compiled binary**, a **shell script**, or
has a **decorator conflict** (e.g. `@hydra.main` rewrites the function
signature so `@register_run` can't be stacked on top), direct
decoration isn't possible. For those cases the `hpc-wrap-entry-point`
skill (invoked by `/submit-hpc`'s escalation playbook when the worker
escalates with `mature_repo_needs_interview`, or directly by another
agent harness) materializes a `@register_run` **wrapper** at
`.hpc/wrappers/<run_name>.py` whose body `subprocess.check_call`s the
underlying command with kwargs substituted in.

The wrapper is the **documented fallback**, not the default. The
default path is direct decoration on the function. The wrapper is the
rescue boat for entry points the framework genuinely can't decorate
directly — `src/hpc_agent/incorporation/wrap_entry_point.py` materializes
it on demand and `interview.json` records the `shell_command` pointer
so the submit worker uses the wrapper as the `EXECUTOR`.

## Summary

- The contract is a `@register_run`-decorated Python function with typed kwargs.
- The function can live in a notebook, a script, or a package module — framework-agnostic.
- `export-package` is the notebook-only build step; for script-shape repos it's a no-op.
- `tasks.py` is the same dispatch boundary in all shapes.
- The `shell_command` wrapper exists for non-Python entry points and decorator conflicts; it is a documented fallback, not the default.
