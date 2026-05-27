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
`no_loop_detected` it falls through to the LLM tree.

## Purpose

Classify the ~80% boilerplate cases (DOALL append-loops, `reduce` /
`accumulate` calls, rolling-window slices) deterministically and
cheaply, so the agent only spends context budget on novel patterns.

Returns `{kind, evidence, monoid?, tried}` where:

- `kind` is one of `independent`, `associative`, `sequential`,
  `needs_halo_expr`, `no_loop_detected`, `unclassifiable`,
  `function_not_found`.
- `monoid` is `"sum"` or `"moments"` when `kind == "associative"`,
  else `null`.
- `tried` is the ordered list of pattern checks the matcher walked —
  the skill knows which cheap patterns were already ruled out before
  falling back to the LLM tree.

## Compose with

- Predecessors: `discover` (find the `@register_run` function path and
  name to feed the matcher).
- Successors: `classify-axis` (record the classified `DataAxis` once
  resolved — either directly from this primitive's `kind`, or after
  the skill's LLM fallback resolves an `unclassifiable`).

## Notes

The matcher is conservative — uncertain matches surface as
`unclassifiable` rather than a wrong-but-confident classification. A
misclassified axis is silent corruption; a fallback to the LLM tree is
merely slower.

