---
name: inspect-parallel-axes
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent inspect-parallel-axes [--experiment-dir <dir>]
  python: hpc_agent.ops.inspect_parallel_axes.inspect_parallel_axes
---
# inspect-parallel-axes

Inspect an experiment's parallel axes in one call. Collapses the
multi-`Read` the `hpc-build-executor` / axes-init companion performs
once per executor build — reading `.hpc/tasks.py` and `.hpc/axes.yaml` —
into a single pure-query CLI verb the skill can branch on.

## Inputs / outputs

See `hpc_agent/schemas/inspect_parallel_axes.{input,output}.json`. Input
takes only `experiment_dir` (defaults to cwd on the CLI). Output carries
two halves:

- the parsed `.hpc/axes.yaml` — `axes_yaml_present`, `axes`,
  `homogeneous_axes`, `executors` — so the companion knows whether
  axes-init already ran (and would refuse-without-force) and what it
  recorded;
- the raw `.hpc/tasks.py` — `tasks_py_present` + `tasks_py_body` — for
  the agent to identify each parallel dimension from the FLAGS / grid /
  `resolve` shape, exactly as the companion's Step 1 does today.

## Pure query — reads, never executes

`side_effects: []`. The verb reads files and runs nothing. The
`tasks.py` body is returned as **text**, not imported — the same
no-arbitrary-execution discipline the skill follows with its `Read`
tool, and faithful to what the companion does today: it *Reads*
`tasks.py` to eyeball the grid / `resolve` shape; it does not run it.
The body is tail-capped (~8000 chars) so a pathological hand-grown
`tasks.py` can't bloat the envelope.

## Degradation

A fresh experiment with no `.hpc` artifacts returns the empty summary
(`*_present: false`, empty lists / maps), not an error — the companion
treats that as "nothing recorded yet, classify from scratch". A
corrupt / schema-violating `axes.yaml` is surfaced as a non-null
`axes_yaml_error` string rather than raised, so the `tasks.py` half is
still returned even when the YAML half is broken.

## Why this exists

The axes-init companion's Step 1 ("inspect the experiment for parallel
axes") was prose instructing a manual multi-`Read` of `tasks.py` plus
`axes.yaml`. Folding both reads into one deterministic verb removes the
prose seam where the agent had to remember which files to read and in
what shape — and gives the companion a single structured envelope to
branch on instead of re-deriving the shape from raw file contents each
time.
