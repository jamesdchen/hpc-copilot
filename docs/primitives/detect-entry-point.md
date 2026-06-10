---
name: detect-entry-point
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent detect-entry-point --experiment-dir <experiment_dir>
  python: hpc_agent.ops.detect_entry_point.detect_entry_point
---
# detect-entry-point

Scan an experiment directory for Python entry-point candidates, classify
each candidate's argv style, and locate files already carrying
`@register_run`. Collapses the six raw-shell probe block (`ls` / `find` /
`grep` / `head`) that `hpc-wrap-entry-point` SKILL.md duplicated verbatim
across Step 0 (the greenfield branch) and Step 1 (the mature-repo branch)
into one deterministic CLI call.

## Inputs / outputs

See `hpc_agent/schemas/detect_entry_point.{input,output}.json`. Input
requires only `experiment_dir`. Output carries `kind`
(`greenfield` | `detected`), `candidates` (one entry per discovered
entry point, each with a classified `argv_kind`), `decoration_found`
(the files containing `@register_run`), and the optional `materialized`
block surfaced from `interview.json` (see below).

## What it detects (one probe → one rule)

The verb faithfully reproduces the six shell probes the SKILL.md used to
run twice:

- `ls main.py train.py run.py experiment.py` — root-level Python
  candidates by conventional name.
- `ls src/main.py src/train.py src/run.py` — the same under `src/`.
- `find . -maxdepth 4 -name __main__.py -not -path '*/.*'` — package
  `__main__.py` modules (a `python -m <pkg>` invocation). The depth-4
  cap and the dotfile-dir exclusion (`.venv`, `.git`, …) are honored so
  the verb matches exactly the files `find` would.
- `test -f pyproject.toml && grep -A1 '[project.scripts]'` — declared
  console-script entry points. `path` is the registered command name
  (the target module is opaque to a filesystem scan).
- `ls run.sh launch.sh ./simulator` — non-Python (shell / binary) entry
  points.
- `grep -rln '@register_run' notebooks/ src/ *.py` — files already
  carrying `@register_run` decoration, surfaced as `decoration_found`.

## argv classification

Each Python candidate's `argv_kind` is read off its imports + decorators,
mirroring the SKILL.md prose:

- `@hydra.main` → `hydra` (checked first — hydra wraps the function and
  hides the signature, so it wins even when the file also imports
  `argparse`).
- `typer` import / `@app.command` → `typer`.
- `@click.…` / `import click` → `click`.
- `fire.Fire(…)` → `fire`.
- `argparse.ArgumentParser` / `import argparse` → `argparse`.
- A bare `if __name__ == "__main__":` block, or a package `__main__.py`
  with no recognized library → `__main__`.

Console scripts classify to `console_script`; shell / binary entry points
to `shell`.

## The materialized entry-point block

When a wrapper-fallback onboarding (`hpc-wrap-entry-point`) has already
run, it persists the chosen entry point to `interview.json` under
`_materialized.entry_point`. This verb surfaces that block as the
optional `materialized` field so ONE `detect-entry-point` call answers
the worker's submit-flow Step 0b — honor a materialized wrapper *and*
run the mature-repo fallback probe — without a native Read/Glob tool:

- `materialized.kind == "shell_command"` — a wrapper was materialized at
  `materialized.wrapper_path`; treat `materialized.run_name` as the
  picked run, use `materialized.executor_cmd` as the EXECUTOR, and feed
  `materialized.data_axis` (when present) into the axes write. Skip the
  discover scan.
- `materialized.kind == "register_run"` / `"python_module"` — pointers
  (no wrapper materialized); fall through to normal `discover-runs`,
  scoped to `run_name` or `module`/`function`.

The verb reads `interview.json` from the experiment-dir root (the
canonical location the `interview` primitive writes) and accepts a
`.hpc/interview.json` fallback. When no `interview.json` exists, it is
malformed, or it carries no `_materialized.entry_point`, the field is
absent and the repo-scan fields above are unchanged (the worker falls
through to the mature-repo `candidates` / `decoration_found` probe). The
internal `frozen_shas` identity detail is intentionally not surfaced.

## The greenfield branch

`kind` is `greenfield` only when the scan found NO entry-point candidate
of any kind AND no `@register_run` on disk — exactly the condition under
which `hpc-wrap-entry-point` Step 0 scaffolds a seed file via
`build-template --shape {script,notebook}` instead of onboarding an
existing entry point. A `@register_run` already on disk is a
non-greenfield signal in its own right (the repo is partially onboarded),
so a decorated helper with no conventional entry-point file still reports
`detected`.

## Why this exists

The agent's prose-discipline at the top of `hpc-wrap-entry-point` used to
be: "run these six probes, eyeball the output, branch on greenfield vs.
detected" — and the identical block was pasted into both Step 0 and
Step 1, so a change to one could silently drift from the other. Folding
the probes into one verb makes the scan deterministic (the agent can't
reorder or drop a probe), removes the duplication seam, and hands the
skill a single typed `data` block to branch on.
