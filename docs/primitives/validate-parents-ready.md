---
name: validate-parents-ready
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.parents_ready.validate_parents_ready
---
# validate-parents-ready

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly; `submit-pipeline` composes it and returns a typed
> `parents_not_ready` refusal on findings. `submit-pipeline` is the
> agent-facing verb.

Pre-submit DAG readiness check (the readiness piece of
[`docs/design/dag-kernel.md`](../design/dag-kernel.md)): every run_id the
about-to-submit spec declares in `parents` must have reached terminal-success
(`complete` in the journal). A child submitted earlier materializes its tasks
from partial or absent parent outputs — silently, because the child's
`tasks.py` reads whatever the parents' result dirs hold at import time.

The check is the ∀-parents quantifier over per-run machinery that already
exists: sidecar presence (the dependency exists locally) and journal lifecycle
(it finished, successfully). It is a lifecycle predicate only — what flows
across the edge (which files, what format) is the experiment's, never checked
here.

## Composers

- **`submit-pipeline`** — composes it mechanically when the embedded submit
  spec declares `parents`, and self-skips otherwise (a 0-parent spec never
  reaches it). The pipeline turns findings into a typed `parents_not_ready`
  refusal.
- The bare `submit-flow` verb stays **unenforced**: callers who hand-walk the
  verbs compose this one themselves, BEFORE a parented submit.

Predecessors: `monitor-flow` (drives parents to terminal), `find-prior-run`.
Successors: `submit-flow` / `submit-pipeline` with `parents` set, whose
`tasks.py` reads `parent_records(experiment_dir, parent_run_ids)` for the
lineage paths.

## Contract surface

Takes `parent_run_ids` (list of run_id strings, required, ≥1 — the spec's
`parents`; order irrelevant, duplicates checked once).

Returns a `ValidateParentsReadyResult` with:

- `findings` — empty = every parent ready. One `error` finding per not-ready
  parent: `parent_run_missing` (no sidecar), `parent_failed` (terminal but
  `failed`/`abandoned`), `parent_not_terminal` (`in_flight`, or no journal
  record). Each carries the observed state as evidence and a state-specific
  `suggested_fix`.
- `parent_states` — run_id → observed state (`complete` / `in_flight` /
  `failed` / `abandoned` / `missing` / `unknown`) for every requested parent,
  whether or not it fired a finding, so a caller can render the whole
  dependency frontier.

## Invariants

- **Pure local read** of `.hpc/runs/` sidecars and the journal. Same inputs,
  same result; no SSH, no qsub.
- **`unknown` is conservative.** A sidecar without a journal record can mean
  the run is in flight on another machine or that the journal was wiped — the
  validator cannot prove terminal, so it refuses. If the run is known finished,
  `mark-run-terminal` repairs the journal and the check passes.
- **Identity is a separate concern.** Readiness says the parents *finished*;
  the composed `node_sha` (recorded at sidecar-write via `resolve_node_sha`)
  says which *version* of them the child consumed. This validator never reads
  identities.
- **Lifecycle only.** What crosses the edge — which files, what format — is
  the experiment's business and is never checked here.

## Coupling

- Reads the journal lifecycle states directly; adding a terminal state means
  classifying it here too, or it silently lands in `unknown` and refuses.
- Paired with `mark-run-terminal`, which is the documented repair for the
  `unknown` case.
- `submit-pipeline` owns the refusal shape (`parents_not_ready`); this
  primitive only reports findings.

## Failure modes

- **Parent finished on another machine** → no local journal record, classified
  `unknown`, submit refused. Intended: proving terminal is not possible from
  here. Repair with `mark-run-terminal`.
- **Caller uses bare `submit-flow` with `parents`** → the gate never runs.
  This is the known unenforced path; the agent-facing composer is
  `submit-pipeline`.
