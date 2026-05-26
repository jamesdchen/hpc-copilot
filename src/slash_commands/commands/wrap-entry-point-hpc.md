Invoke the `hpc-wrap-entry-point` skill via the Skill tool (`skills/hpc-wrap-entry-point/SKILL.md`) for the workflow. The skill handles two on-ramps in one place: **greenfield repos** (no entry point yet) get a seed scaffold via `build-template --shape {script,notebook}` (defaulting to a `.py` script), and **mature repos** (existing `main.py`, `train.py`, `python -m pkg.cli`, a compiled binary, ...) get walked through `@register_run` **direct decoration** on their existing function as the default path — a two-line code edit (an import and a decorator). Only when direct decoration isn't possible (non-Python entry point, a CLI library's decorator conflicts with `@register_run`, vendor code the user can't touch) does the skill fall back to materializing a wrapper at `.hpc/wrappers/<run_name>.py`. All paths end by invoking the `interview` primitive to persist a starter `tasks.py` + `interview.json`. The skill is the canonical SoT. The framework contract being onboarded is described in [`docs/internals/experiment-contract.md`](../../docs/internals/experiment-contract.md).

This slash command is the human-facing entry point for both states — scaffolding a fresh entry point for a greenfield repo, and onboarding an existing runner from a mature repo without rewriting it. The default move on the mature-repo path is direct decoration on the user's existing function; the wrapper is the rescue boat for cases where the decorator can't go directly on the entry point. `/submit-hpc` escalates with `mature_repo_needs_interview` on a cache miss; reasons to invoke standalone:

- **Greenfield**: bootstrap a new experiment repo with a `@register_run` seed (script or notebook) plus `tasks.py` + `interview.json`, ready for `/submit-hpc`.
- Pre-onboard a mature repo before its first `/submit-hpc` so the submission is one shot, not two.
- Walk an experimenter through `@register_run` direct decoration when they're new to the framework and want guidance on the two-line edit.
- Refresh after editing the entry point's flags or adding a new frozen config — on the wrapper path the declared signature needs to track the entry point's actual interface; on the direct-decoration path the framework already sees the function directly.
- Switch the frozen experiment (`configs/exp_42.yaml` → `configs/exp_43.yaml`) without leaving stale wrapper / interview state behind.

## Human-facing dialog

The intake is conversational: the agent finds the entry point, decides the pathway, and walks the user through it. The headline case is **direct `@register_run` decoration**:

```
I see `train.py` looks like your entry point — argparse with `--config PATH` and
`--seed INT`. The cleanest way to onboard this is to put `@register_run` directly
on the function the script ultimately calls. Two-line edit:

  # train.py
  from hpc_agent import register_run

  @register_run
  def run(config: str, seed: int) -> None:
      # ... the body that used to live below argparse.parse_args() ...
      ...

  if __name__ == "__main__":
      # existing argparse block stays as-is, calls `run(**vars(args))`
      ...

The CLI still works (`python3 train.py --config exp_42.yaml --seed 0`); the
framework now has a typed function to introspect.

Apply this edit?  [Y / n / show me first]
```

When direct decoration isn't viable (compiled binary, `@hydra.main` decorator conflict, vendor code), the skill falls back to wrapper materialization with the same proposes-then-confirms dialog: argv template, signature, frozen configs, scale axis, optional data-axis classification.

On **Y**, the skill invokes `hpc-agent interview`, which writes `tasks.py` + `interview.json` — and on the wrapper path also writes `.hpc/wrappers/<run_name>.py`.

## Notes

- **Direct decoration is the default.** The wrapper is the rescue boat. A two-line code edit beats a subprocess shim whenever it's possible; the skill prefers direct decoration unless something genuinely blocks it.
- **One YAML = one frozen experiment.** To run a different frozen pipeline, write `configs/exp_43.yaml` (don't edit `exp_42.yaml` in place) and re-run this command — the new content hash makes the framework correctly treat it as fresh.
- **`/submit-hpc` is the next step.** After this command completes, run `/submit-hpc`; on the direct-decoration path the worker's normal `@register_run` discovery picks up the freshly decorated function, and on the wrapper path the worker reads `interview.json` and uses the materialized wrapper as the executor.
- **Backstop is the canary (wrapper path).** The wrapper is the framework's contract; if its declared signature drifts from the entry point's actual flags, the canary catches it (one failed task, not a hundred). On the direct-decoration path the framework reads the real function's signature, so there's nothing to drift.
