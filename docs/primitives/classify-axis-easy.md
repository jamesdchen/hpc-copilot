---
name: classify-axis-easy
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent classify-axis-easy --source-path <source_path> --run-name <run_name>
  python: hpc_agent.incorporation.classify_axis_easy.classify_axis_easy
---
# classify-axis-easy

Stdlib-only AST pattern-match for a `@register_run` function's
`DataAxis`. Read-only fast path used by the `hpc-classify-axis` skill:
on a confident hit the skill records the classification without
invoking the LLM decision tree; on `unclassifiable` /
`no_loop_detected` / `function_not_found` it falls through to the LLM
tree.

## Purpose

Classify the ~80% boilerplate cases — `Independent` append-loops,
`BoundedHalo` shapes from a fixed pattern library (first-order /
finite-order stencil, bounded-window deque, pandas rolling, EMA), and
`Sequential` for unrecognized carried state — deterministically and
cheaply, so the agent only spends context budget on novel patterns.

Returns `{kind, evidence, halo_expr?, tried}` where:

- `kind` is one of `independent`, `bounded_halo`, `sequential`,
  `no_loop_detected`, `unclassifiable`, `function_not_found`.
- `halo_expr` is a string in the axis-config halo-expression syntax
  (bare param names, numeric literals, `+ - * //`, `min` / `max`) when
  `kind == "bounded_halo"`; `null` otherwise.
- `tried` is the ordered list of pattern checks the matcher walked —
  the skill knows which cheap patterns were already ruled out before
  falling back to the LLM tree.

The matcher does **not** autonomously detect `Associative`. The
framework provides task-array map-reduce via `combine-wave`, and users
who want to parallelize an inner reduction express it as a sweep
dimension in their `task_generator`. The skill's LLM fallback still
recognizes Associative for the long tail.

## Compose with

- Predecessors: `discover` (find the `@register_run` function path and
  name to feed the matcher).
- Successors: `classify-axis` (record the classified `DataAxis` once
  resolved — either directly from this primitive's `kind` /
  `halo_expr`, or after the skill's LLM fallback resolves an
  `unclassifiable`).

## Notes

The matcher is conservative — patterns outside the recognized library
surface as `sequential` (the framework runs the inner loop serially,
which is safe) rather than a wrong-but-confident classification. A
misclassified axis is silent corruption; a fallback to Sequential or
the LLM tree is merely slower.

The defining characteristic of `BoundedHalo` is that iteration N reads
iteration N-1's *computed output*. Input-array windowing — patterns
like `for i: train = data[i-W:i]; model = fit(train); ...` where each
iteration refits from scratch on a slice of the *input* — is
`Independent`, not `BoundedHalo`.
