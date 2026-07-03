---
name: aggregate-check
verb: workflow
side_effects:
- ssh: <cluster> (aggregate-preflight reconcile, when scheduler supplied)
idempotent: true
idempotency_key: run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: journal_corrupt
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-agent aggregate-check --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.aggregate_blocks.aggregate_check
---
# aggregate-check

Readiness + integrity block for the aggregate flow (human-amplification blocks,
`docs/design/human-amplification-blocks.md` §3 — the finer grain of submit's
S4). A thin orchestrator that composes `aggregate-preflight` and
`verify-aggregation-complete`, then TERMINATES at a human decision point
carrying a code-digested *brief*. It answers one question — *is this run safe to
reduce?* — and never reduces anything itself (that is `aggregate-run`).

The load-bearing invariant: **integrity issues are never auto-masked** (§2, the
#355 doctrine extended from computing to concluding). Missing waves/tasks,
cross-run contamination, provenance mismatches, and column violations are each
surfaced as a decision point carrying a conservative `recommendation` the LLM
drafts a proposal around and the human greenlights or nudges. Code digests the
evidence; the human decides.

## Inputs

- `run_id` (string) — the run to check. Matches the `.hpc/runs/<run_id>.json`
  sidecar stem.
- `run_preflight` (bool, default `true`) — run `aggregate-preflight`
  (install-commands ∥ load-context, optional reconcile) and fold its overall
  pass/fail into the brief. Disable for a pure local readiness+integrity check.
- `reconcile_scheduler` (string | null) — forwarded to `aggregate-preflight`.
  When supplied AND load-context reports `next_step_hint == "monitor"`,
  reconcile the journal-only in-flight run against the cluster before the
  readiness gate trusts the journal.
- `allow_partial` (bool, default `false`) — the operator's stance on a partial
  aggregate. When `false`, missing waves are a blocking integrity decision; when
  `true`, the missing-waves issue is still surfaced (never auto-masked) but its
  recommendation reflects the operator's explicit choice to proceed.
  Contamination / provenance / column issues block regardless.

## Outputs

`AggregateBlockResult` — `{block: "check", stage_reached, needs_decision, reason,
run_id, brief}`.

`stage_reached` is one of:

- `not_ready` — no journal record, or the run is not terminal, or preflight
  failed. `needs_decision: true` (reconcile / keep watching / fix preflight).
- `integrity_review` — the integrity gate surfaced blocking issue(s). Each lives
  in `brief.integrity_issues` as `{issue, detail, recommendation, auto_masked:
  false}`. `needs_decision: true`.
- `ready` — terminal, preflight clean, no blocking integrity issues.
  `needs_decision: false` — greenlight straight to `aggregate-run`.

`brief` carries the readiness digest (`record_found`, `status`, `terminal`,
`combined_waves`, `failed_waves`), the raw integrity report
(`integrity_report`, `integrity_checked`), and the never-auto-masked
`integrity_issues` list.

The integrity gate runs only once a local `_combiner/` exists (post-pull /
re-check). A pre-run check where nothing is pulled yet reports
`integrity_checked: false` — `aggregate-run` verifies integrity itself after its
own pull.

## Errors

- `spec_invalid` — malformed spec.
- `ssh_unreachable` — the preflight reconcile leg could not reach the cluster.
- `journal_corrupt` — the journal could not be read.

## Idempotency

Pure read + digest — no cluster mutation, no journal write of its own (the
optional preflight reconcile leg writes the journal, itself idempotent).
Re-running on the same `run_id` produces the same readiness/integrity digest for
unchanged state.

## Notes

The block does NOT enforce the terminal gate by raising — it DIGESTS readiness
so a non-terminal run becomes a decision point rather than a crash.
`aggregate-run` is the block that enforces the gate (it raises
`precondition_failed` for a non-terminal run). A primitive owns its invariants:
`aggregate-run` never assumes `aggregate-check` ran.
