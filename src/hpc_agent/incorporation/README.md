# `incorporation/` — the user authoring surface

## What + why

`incorporation/` is the user authoring surface: code that turns the
user's experiment design (`tasks.py`, axis spec, executor) into a
buildable campaign scaffold. It owns the templating engine, the
`build_*` atoms that emit scaffolded files, and (if present) the skill
registration glue. Other subjects (`ops/`, `meta/`) consume what
`incorporation/` produces; `incorporation/` never consumes from them.

## Invariant

`incorporation/` promises: user inputs in → scaffolded campaign
artifacts on disk out, with no remote I/O and no campaign-state
mutation (it produces the files a campaign will run, but does not run
it).

## Public vs internal

- `template/` re-exports its public API via `template/__init__.py` —
  the layer-1 notebook / CLI helpers (`register_run`, `save_artifact`,
  `export_notebook`, `discover_runs`, `flags_from_signature`,
  `flags_for_run`) and the layer-2 parallelization planner
  (`DataAxis`, `plan_tasks`, `load_series`, `check_elision`,
  `assert_elision_equivalent`).
- `build/executor.py`, `build/submit_spec.py`, `build/tasks_py.py`,
  `build/template.py` are public primitive modules — each exposes a
  single `build_*` entry point registered in the primitive registry
  via `@primitive(...)`.
