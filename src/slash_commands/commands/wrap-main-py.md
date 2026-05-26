Invoke the `wrap-main-py` skill via the Skill tool (`skills/wrap-main-py/SKILL.md`) for the workflow: detect the user's `main.py` entry point, conduct the proposes-then-confirms interview about its argv / signature / frozen YAML configs / data-axis classification, then invoke the `interview` primitive to materialize a `@register_run` wrapper at `.hpc/wrappers/<run_name>.py` plus a starter `tasks.py` + `interview.json`. The skill is the canonical SoT.

This slash command is the human-facing entry point for **mature-repo onboarding** — taking a repo that already has `main.py` + YAML configs and giving the framework enough structure to scale it across the cluster, without rewriting `main.py` as a notebook.

Run this once before `/submit-hpc` on a repo that:

- Has `main.py` (or another shell-invokable entry point), not a `@register_run` notebook.
- Wants to scale a *frozen* experiment (configured by one YAML) across seeds / shards / replicates — not sweep over the YAMLs themselves.
- Hit a `mature_repo_needs_interview` escalation from `/submit-hpc`.

The skill conducts an interactive intake the headless `/submit-hpc` worker can't do — it reads `interview.json` but doesn't write it. This skill writes it.

After `wrap-main-py` completes, `/submit-hpc` finds the materialized `_materialized.entry_point` in `interview.json`, skips its `@register_run` discovery step, and uses the wrapper's `executor_cmd` directly. The frozen YAML's content hash rides through every task's kwargs so `cmd_sha` correctly distinguishes `exp_42.yaml` from `exp_43.yaml` — re-running the same YAML dedups, an accidental in-place edit doesn't.

## Notes

- **One YAML = one frozen experiment.** The skill's identity model assumes you don't edit a YAML between runs of the same experiment. For a new experiment, write `exp_43.yaml` (don't edit `exp_42.yaml`) and re-run this skill.
- **Wrapper is the framework's contract.** `main.py` stays opaque to the framework; the wrapper's typed signature is what `validate-executor-signatures` and `classify-axis` see. The canary backstops any drift between the wrapper and `main.py`'s actual flags.
- **Re-run to refresh.** If you change `main.py`'s CLI flags or add a new frozen config, re-run this skill to re-elicit the signature / re-hash the YAMLs. The interview overwrites `interview.json` and the wrapper byte-equivalently when nothing changed.
