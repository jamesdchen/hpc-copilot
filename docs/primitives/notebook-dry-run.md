---
name: notebook-dry-run
verb: mutate
side_effects:
- file_write: <experiment>/.hpc/notebooks/<audit_id>.decisions.jsonl (SAMPLED render
    receipts, only when audit_id is given)
idempotent: false
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent notebook-dry-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.notebook.dry_run_op.notebook_dry_run
---
# notebook-dry-run

The drafting-loop **PREVIEW** run. The maintainer's affordance, verbatim: the human
"needs to be able to run it on small pieces of data to see what it will do." Given a
percent-format `.py` (`# %%` cells, `# hpc-audit-section:` markers), this verb
executes it **section by section in one namespace, in the CURRENT LOCAL
environment**, and returns a deterministic, code-rendered per-section outcome the
human reads to decide whether to revise or enter the audit (draft → **dry-run** →
revise → audit). It is **experiment-AGNOSTIC**: some code the LLM drafts never ends
up on a cluster run, so the source need not be bound to any audit (`audit_id` is
optional; standalone mode touches no `.hpc` state).

## This slice changes NO trust semantics (the load-bearing boundary)

A dry-run is a **SAMPLE, not a proof**. It journals its render receipts with
`execution_scope="sampled"`, and `read_render_receipts` — the ONE reader feeding the
D-attention tier / `notebook-auto-clear` / the graduation gate — filters that class
out. So a sampled run can **never** green / auto-clear an assertion-bearing section
the way a full run (`notebook-render --execute`, `execution_scope="full"`) can. A
full receipt written after a sampled one still clears; a sampled receipt never
revokes an earlier full one. This is the maintainer's explicit constraint: the
preview must not clear an assertion-bearing section the way a full run can.

## Sample bounding is a DISCLOSED CONTRACT, never a silent full run

`sample_n` is exposed to the source through the `HPC_NOTEBOOK_SAMPLE_N` env var. Core
does **not** mechanically truncate an arbitrary source's inputs — inferring how to
cap an opaque reader would require the reader-function vocabulary the Q1 boundary
forbids core to grow. So the env var **plus** a prominent `sample_disclosure` IS the
contract: the result says the cap is *advisory* (the source must read the var to
honor it), and never claims the data was capped on the source's behalf.

## Inputs

A `NotebookDryRunSpec` (`hpc_agent._wire.actions.notebook_dry_run`):

- `source` (string, required) — experiment-relative path to the percent-format `.py`.
- `audit_id` (string, optional) — when present, the observation plan (declared
  observables) is read from the recorded audit config and one SAMPLED (non-clearing)
  receipt is journaled per executed section. When absent, standalone: no `.hpc` state
  read or written.
- `sample_n` (int, default 50, ≥ 1) — the advisory cap exposed via
  `HPC_NOTEBOOK_SAMPLE_N`.
- `sections` (list of string, optional) — a section-slug filter; execution runs every
  section in source order up to AND including the last named one (dependencies still
  run), later sections read `skipped`. A named slug the source lacks is a loud
  `spec_invalid`.
- `timeout_sec` (int, default 300, ≥ 1) — hard wall-clock cap; a runaway source is
  abandoned and the in-progress section reported `timeout`, and the verb always
  returns within the bound.

## Outputs

`data` is a `NotebookDryRunResult`: `executed_scope` (always `"sampled"`),
`env_disclosure` + `interpreter` (ran in the current local env, not the cluster),
`sample_n` / `sample_env_var` / `sample_disclosure` / `sample_cap_consumed`,
`timed_out`, `receipts_recorded` (slugs a sampled receipt was journaled for), a
`sections` list of per-section outcomes, `observables` (declared observables measured
in the final namespace, audit mode only), and `markdown` — the code-rendered
projection relayed VERBATIM. Each section outcome carries `outcome`
(`ran` / `raised` / `skipped` / `timeout`), `ran`, `error`, `elapsed_sec`, the
verbatim `traceback_tail` on a raise, the `stdout_tail`, an `output_sha` (sha256 of
captured stdout — the receipt's opaque hash), and `assertions` — each declared
`assert` with its **executed** verdict (`passed` / `failed` / `not_run`), distinct
from the static assertion table.

## Errors

- `spec_invalid` — an unreadable `source` path, a malformed percent-format module
  (a bad/duplicate/misplaced `# hpc-audit-section:` marker — the parser's boundary
  guards), or a section filter naming a slug the source lacks. Not retry-safe.

## Idempotency

Deliberately **not idempotent** (like the other receipt writers): in audit mode each
call appends a fresh SAMPLED receipt per executed section. Standalone mode writes
nothing. A raising section stops the run; later sections read `skipped`.

## Usage

```
hpc-agent notebook-dry-run --spec spec.json --experiment-dir .
```

where `spec.json` is `{"source": "<py relpath>", "audit_id": "<id>", "sample_n": 50}`
(drop `audit_id` for the standalone, experiment-agnostic case). Read the `markdown`
and relay it verbatim; then revise the source, or enter the audit loop.
