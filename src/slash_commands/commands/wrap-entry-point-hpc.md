Invoke the `hpc-wrap-entry-point` skill via the Skill tool (`skills/hpc-wrap-entry-point/SKILL.md`) for the workflow: detect the user's entry point (any shell-invokable command — `main.py`, `train.py`, `python -m pkg.cli`, a compiled binary, ...), conduct the proposes-then-confirms interview about its argv / signature / frozen YAML configs / data-axis classification, then invoke the `interview` primitive to materialize a `@register_run` wrapper + starter `tasks.py` + `interview.json`. The skill is the canonical SoT.

This slash command is the human-facing entry point for **mature-repo onboarding** — taking a repo that already has its own runner (anything that isn't a `@register_run` notebook) and giving the framework enough structure to scale it across the cluster, without rewriting the runner. `/submit-hpc` escalates with `mature_repo_needs_interview` on a cache miss; reasons to invoke standalone:

- Pre-onboard a mature repo before its first `/submit-hpc` so the submission is one shot, not two.
- Refresh after editing the entry point's CLI flags or adding a new frozen config — the wrapper's declared signature needs to track the entry point's actual interface.
- Switch the frozen experiment (`configs/exp_42.yaml` → `configs/exp_43.yaml`) without leaving stale wrapper / interview state behind.

## Human-facing dialog

The intake is conversational: the agent finds the entry point, proposes an entry-point shape, and asks for confirmation:

```
I see `train.py` looks like your entry point — argparse with `--config PATH` and
`--seed INT` — and `configs/exp_42.yaml` looks like a frozen pipeline. I'll wrap it as:

  argv:         python3 train.py --config {config} --seed {seed}
  signature:    config: str, seed: int
  frozen:       configs/exp_42.yaml  (sha threaded into every task's kwargs)
  scale axis:   seed × 100 (items_x_seeds)
  data_axis:    Independent (each seed is a pure function of its kwargs)

Looks right?  [Y / n / edit]
```

On **edit**, the skill takes the correction (a different entry point, a flag rename, a different YAML, a different scale-up axis). On **Y**, it invokes `hpc-agent interview`, which writes `.hpc/wrappers/<run_name>.py` + `tasks.py` + `interview.json`.

## Notes

- **One YAML = one frozen experiment.** To run a different frozen pipeline, write `configs/exp_43.yaml` (don't edit `exp_42.yaml` in place) and re-run this command — the new content hash makes the framework correctly treat it as fresh.
- **`/submit-hpc` is the next step.** After this command completes, run `/submit-hpc`; the worker reads `interview.json` and uses the materialized wrapper as the executor.
- **Backstop is the canary.** The wrapper is the framework's contract; if its declared signature drifts from the entry point's actual flags, the canary catches it (one failed task, not a hundred).
