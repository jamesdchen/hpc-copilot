---
name: classify-axis-auto
verb: scaffold
side_effects:
- writes-sidecar: <experiment>/.hpc/axes.yaml
idempotent: true
idempotency_key: experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent classify-axis-auto [--spec <path>] [--experiment-dir <dir>]
  python: hpc_agent.incorporation.classify_axis_auto.classify_axis_auto
---
# classify-axis-auto

The deterministic head of the `hpc-classify-axis` skill, collapsed into
**one call**. Composes `classify-axis-preflight` (`discover-runs` +
cache-check + `recall`) → `classify-axis-easy` (the stdlib AST fast-path
matcher) → `classify-axis` (the recorder that writes
`<experiment>/.hpc/axes.yaml`). The three functions are called **directly
in-process** — no subprocess fan-out — so the strict
preflight-produces-the-source-path → matcher-consumes-it dependency is a
code invariant, not a prose instruction an agent can mis-order.

This kills the hand-sequencing failure: an autonomous agent once
hand-walked preflight → easy → record and mislabelled the strict
dependency as "in parallel". The sequence is deterministic, so it belongs
in code. The LLM now makes one tool call and only does genuine judgement
on the long tail — an `unclassifiable` / `function_not_found` matcher
verdict.

## Inputs

See `hpc_agent/schemas/classify_axis_auto.{input,output}.json`. Every
field is optional:

- `run_name` — the `@register_run` function to classify. Omit it and the
  composite resolves the sole run from `discover-runs`; if several exist
  with no scope, it returns `spec_invalid` (`ambiguous_run`).
- `data_axis` — a caller-resolved classification (the interview / slash
  path, after a human-facing dialog). Same `{kind, halo?, monoid?}` shape
  the `classify-axis` recorder takes. When present the composite records
  it directly and runs neither `recall` nor the matcher.
- `root` / `task_kind` — forwarded to the preflight's `recall` sub-call.

`experiment_dir` is the framework-context argument (the repo root; the
file lands at `<experiment_dir>/.hpc/axes.yaml`).

## Outputs

A discriminated result over the two terminal shapes:

- **Recorded** — `{recorded: true, run_name, kind, classified_by,
  axes_path}`. `classified_by` is `interview` (caller supplied
  `data_axis`), `recall` (a prior similar campaign was reused), or `agent`
  (the AST matcher classified it). A cache-hit reuse also reports
  `recorded: true` carrying the stored `classified_by`.
- **Hand-off** — `{needs_llm_tree: true, run_name, source_path,
  run_signature_sha, evidence, tried}`. The matcher abstained; **nothing
  was written**. The caller reads the run body from `source_path`, walks
  the LLM decision tree, and records the result via the `classify-axis`
  primitive with the `run_signature_sha` echoed here.

## Internal branches

After preflight resolves the single run (`run_name` / `source_path` /
`run_signature_sha`):

| Branch | Trigger | Effect |
|---|---|---|
| A | caller supplied `data_axis` | record `classified_by="interview"` |
| B | preflight cache hit (`cache_check.data.hit`) | reuse the stored classification, **no re-write** |
| C | a prior campaign in `recall` classified the same `run_name` with a confident kind | record `classified_by="recall"` |
| D | `classify-axis-easy` returns a confident kind | map to a `data_axis`, record `classified_by="agent"` |
| E | matcher returns `unclassifiable` / `function_not_found` | **no record**, return `needs_llm_tree` |

Branch D maps the matcher's confident kinds: `independent` →
`{kind: independent}`, `bounded_halo` →
`{kind: bounded_halo, halo: {expr: <halo_expr>}}`, `sequential` →
`{kind: sequential}`, and `no_loop_detected` → `{kind: cartesian}` (the
terminal "no ordered series" verdict — a plain cartesian sweep, distinct
from `independent`, which has a parallelizable series).

Branch C's match is **code-checkable structural identity**: a prior
campaign's `data_axes` entry keyed by the *same* run name carrying a
confident recorded kind. The composite never re-derives a halo or guesses
across differently-named runs — that judgement stays in the LLM tree.

## Errors

- `spec_invalid` (`ambiguous_run`) — the run can't be resolved
  unambiguously: a caller-supplied `run_name` matched no discovered
  function, no `@register_run` functions exist, or several exist with no
  `run_name` to scope. The composite refuses to pick blindly.
- `spec_invalid` — a recorded `data_axis` is internally inconsistent (most
  often a `bounded_halo` whose `halo.expr` is not safe arithmetic over the
  run's parameters). Surfaced by the `classify-axis` recorder before any
  disk write.

## Idempotency

Keyed on `experiment_dir`. Re-running with the same inputs overwrites
`executors.<run_name>` byte-equivalently modulo the `classified_at`
timestamp, and a still-valid cache hit (branch B) is a pure read. The
entry merges via `upsert_executor`, so other executors' entries and the
scheduling-axis hints round-trip untouched.

## Notes

A classification can be wrong — the elision gate (`/submit-hpc` runs
`hpc_agent.experiment_kit.assert_elision_equivalent`) is the backstop.
The matcher's autonomous scope is narrow on purpose (`Independent`,
`BoundedHalo`, `Sequential`, plus the no-loop `cartesian` terminal); the
LLM tree on branch E is the only place `Associative` is recognized, and
the only place free-text judgement enters the pipeline.
