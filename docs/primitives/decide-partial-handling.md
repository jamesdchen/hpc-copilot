---
name: decide-partial-handling
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent decide-partial-handling --failed-count <failed_count> --combined-count
    <combined_count> [--retries-exhausted]
  python: hpc_agent.ops.aggregate.decide_partial_handling.decide_partial_handling
---
# decide-partial-handling

Decide whether to proceed on incomplete aggregate waves, from observable
evidence rather than prose. Backs the aggregate `partial_handling`
decision point.

## Purpose

When `aggregate-flow` returns an `escalation_reason` (some waves failed
`combiner_max_retries`), part of the decision is a switch on facts, not a
feeling:

- Are combiner retries **spent**? If not, the fix is simply to *retry* —
  no decision needed.
- The **missing fraction** `failed / (failed + combined)` is a computed
  number.

So this primitive resolves the deterministic part in code
(`decided_by="code"`, `retry`) while retries remain, and escalates only
the genuine residue once they're exhausted: whether an *acceptable*
missing fraction is OK *for the experiment's purpose* — risk/intent the
framework cannot observe. The escalation carries the missing fraction so
the judgement is "accept this much loss or force-retry?".

## Output

`{decided_by, decision, missing_fraction, failed_count, combined_count,
reason, candidates}`:

- `decided_by` — `code` for `proceed` (nothing failed) / `retry`
  (retries remain); `judgement` once retries are exhausted with waves
  still failed.
- `decision` — the resolved branch on the code path; null on escalate.
- `missing_fraction` — `failed / (failed + combined)`, the evidence the
  acceptability call turns on.
- `candidates` — on escalate, `accept-partial` (carrying
  `missing_fraction`) / `force-retry-failed`.

Pure function over supplied evidence; never raises (`error_codes: []`).
