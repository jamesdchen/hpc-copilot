---
name: dag-frontier
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent dag-frontier [--experiment-dir <dir>]
  python: hpc_agent.ops.dag_frontier.dag_frontier
exit_codes:
- 0: ok
---
# dag-frontier

Read-only view of the recorded run graph — the observation instrument for
caller-side topology walking ([`docs/design/dag-kernel.md`](../design/dag-kernel.md)
step 5). The lineage graph already exists on disk (every parented sidecar
carries `parent_run_ids`), so this verb reconstructs it from `.hpc/runs/` and
answers the walker's standing question in one call: *which runs can serve as
parents for the next submits?*

It is the ∀-nodes lift of [`validate-parents-ready`](validate-parents-ready.md)
(which answers the same question for ONE prospective child's declared
parents); the two share `observe_run_state`, so the surfaces cannot disagree
about what a state means. Deliberately NOT a walker: it computes the frontier
and stops — which child to submit, with what concurrency, stays the caller's
(the earn-it rule for a graph-runner composite is unchanged).

## Outputs

- `nodes` (list, sorted by run_id) — `{run_id, parent_run_ids, state,
  node_sha, blocking_ancestors}`. `state` is `observe_run_state`'s
  vocabulary: a journal status (`complete` / `in_flight` / `failed` /
  `abandoned`) or `unknown` (sidecar without a journal record).
  `blocking_ancestors` is the transitive not-yet-complete ancestry —
  informational; the authoritative pre-submit gate is
  `validate-parents-ready` over a child's *direct* parents.
- `frontier` (list of run_ids) — recorded runs at `complete`: eligible to
  serve as parents for the next submits.
- `summary` (dict, state → run_ids) — the whole graph grouped by state.

## Idempotency

Pure local read of `.hpc/runs/` sidecars + the journal; same inputs, same
result. No SSH, no scheduler.

## Notes

- **Dangling edges are reported, not hidden.** A parent whose sidecar was
  pruned (`MAX_RUNS` eviction) appears in descendants' `blocking_ancestors`
  as `missing` — the view cannot verify it, so it refuses to vouch for it.
  Referenced-only ids are not `nodes` entries (they have no sidecar to
  report).
- **A tainted-but-complete node stays on the frontier.** A node whose
  ancestor failed but which itself completed reports both facts
  (`frontier` membership AND the failed ancestor under
  `blocking_ancestors`); weighing them is caller judgment, per the kernel's
  no-edge-semantics rule.
- **Cycle-safe by construction and by guard.** The submit path cannot record
  a cycle (a parent's sidecar must exist before a child composes its
  identity); the ancestor walk still carries a visited-set so a hand-edited
  sidecar cannot hang it.
- **Compose with**: successors — `validate-parents-ready` (authoritative
  per-child gate) then `submit-pipeline` with `parents` set; `monitor-flow`
  on the in-flight nodes the view surfaces.
