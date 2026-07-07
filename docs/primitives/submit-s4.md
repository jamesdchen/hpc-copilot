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

`stage_reached` ∈ `harvested` (every wave combined cleanly) · `harvest_partial`
(some waves escalated — review the table before concluding) · `detached` (the
default handle return — the worker owns the harvest; the brief arrives via the
journal).

A re-invoke after the detached worker reached its terminal for the current tree
REPLAYS the recorded results brief (`state/block_terminal`, keyed on the sidecar
`cmd_sha`) — no new worker, no SSH.

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
