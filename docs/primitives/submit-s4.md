---
name: submit-s4
verb: workflow
side_effects:
- ssh: <cluster> (wave combine + rsync pull)
idempotent: true
idempotency_key: aggregate.run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: remote_command_failed
  category: cluster
  retry_safe: false
backed_by:
  cli: hpc-agent submit-s4 --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_blocks.submit_s4
---
## Purpose

Submit block **S4 — harvest** (docs/design/human-amplification-blocks.md §3).
Runs `aggregate-flow` (ensure every wave combined → pull partials → reduce) and
digests the reduced metrics into a code-extracted **results table**, plus an
empty slot for the LLM's proposed interpretations. Ends at the results-table
brief for the `y`/nudge loop: code extracts the results; the human concludes from
them (§2 — the #355 doctrine extended from *computing* results to *concluding*
from them). Results are never interpreted raw by the LLM.

## Inputs

A `SubmitS4Spec` JSON spec with:

- `aggregate` — a nested [`AggregateFlowSpec`](aggregate-flow.md) (run_id,
  output_dir, combine/pull/reduce knobs).
- `detach` (bool, default **true**) — detach-by-contract (design §3): the
  greenlight gate fires synchronously, then a durable detached worker owns the
  harvest (per-wave combine SSH + rsync pull + the breaker-deadline
  wait-and-retry can ride a throttled host for minutes) and the block returns a
  `{started, watch: journal, detached_pid}` handle immediately. The
  results-table brief is read from the journal on completion; await the worker
  with [`wait-detached`](wait-detached.md). `false` runs the harvest
  synchronously in-process (tests / CI).

## Outputs

A `SubmitBlockResult` (`block="s4"`, `needs_decision=true`) with a `brief`:

- `run_id`.
- `results_table` — a stable row-per-key projection of the reduced metrics
  (`[{key, metrics}, ...]`, sorted).
- `combined_waves`, `failed_waves`, `escalation_reason`,
  `nonempty_failing_task_ids`, `column_violations` — integrity signals from
  `aggregate-flow`.
- `proposed_interpretations` — handed over **empty**; the slot the LLM fills at
  the `y`/nudge boundary. Concluding is the human's decision.
- `scope_looks` — present ONLY when the run carries caller-attached evidence
  scopes: `{tag: {prior_looks, distinct_lineages}}`, plain counts of prior
  journaled reductions against each scope (`prior_looks` = total look records;
  `distinct_lineages` = distinct supersession-lineage roots). Core interprets
  nothing — no advice, no statistics; the relay renders the counts verbatim and
  the human concludes. **Absent** for a scope-less run (an old brief stays
  byte-identical).

`stage_reached` ∈ `harvested` (every wave combined cleanly) · `harvest_partial`
(some waves escalated — review the table before concluding) · `detached` (the
default handle return — the worker owns the harvest; the brief arrives via the
journal).

A re-invoke after the detached worker reached its terminal for the current tree
REPLAYS the recorded results brief (`state/block_terminal`, keyed on the sidecar
`cmd_sha`) — no new worker, no SSH.

## Scope gate

Before any harvest work, S4 asserts that none of the run's caller-attached
evidence scopes is currently **locked** (`assert_scopes_unlocked`, rigor-primitives
T3). The check fires **synchronously in the parent, pre-detach** — same gate →
detach ordering proof as the greenlight gate: a locked scope refuses loudly to
the caller (`ScopeLocked`, a `precondition_failed`-coded refusal naming the tag
and the lock timestamp), never inside a detached child's log where the refusal
would be invisible. One definition, two call sites — the detached child re-hits
the identical gate inside `aggregate-flow` (defense in depth). A scope-less run
(no sidecar `scopes`) passes silently. The single exit from a locked scope is a
human-journaled **unlock** decision on that scope.

## Errors

`spec_invalid`, `ssh_unreachable`, `remote_command_failed`.

## Idempotency

Idempotent on `aggregate.run_id` — `aggregate-flow` is safe to re-run (combine +
reduce are idempotent).

## Notes

Unit B is adding a `harvest_on_terminal` guarantee (§5, guaranteed harvest) in
parallel. S4 currently calls the EXISTING `aggregate-flow` entry; once the
guarantee lands, route S4 through the guaranteed-harvest path so every terminal
state — completion, anomaly, cap overrun, partial kill — ends in this table.

## Usage

```
hpc-agent submit-s4 --spec spec.json --experiment-dir <dir>
```

Present the `results_table`; the LLM drafts interpretation options into the empty
slot; the human answers `y` (accept an interpretation) or a nudge.
