# ops/aggregate/

## What + why

`ops/aggregate/` is the "collect + verify" half of a campaign: pull per-task metrics off the cluster, reduce them to a campaign-level result, and verify that the canary task agrees with the local baseline. The subject owns the bridge from "tasks finished on the cluster" to "campaign-level result" — combining cluster-side reduction (`cluster_reduce`), per-wave combiner orchestration (`combine`, `runner`), end-to-end aggregation workflow (`flow`), post-aggregate invariant checks (`invariants`), and canary-vs-baseline verification (`canary_verify`).

## Invariant

`ops/aggregate/` promises: typed aggregate spec in → reduced campaign-level result out + canary-vs-baseline verdict; safe to re-run.

## Public vs internal

- `runner.py` — agent-facing primitive module (combiner preconditions / postconditions / provenance helpers).
- `combine.py` — agent-facing primitive module (registers `combine-wave`).
- `cluster_reduce.py` — agent-facing primitive module (registers `cluster-reduce`).
- `invariants.py` — agent-facing pure helper module (registers `verify-aggregation-complete` plus the pure `check_result_columns` helper).

The `aggregate-flow` workflow (composite) lives at `ops/aggregate_flow.py` (role-root sibling, per P5a), and the `verify-canary` atom lives at `ops/verify_canary.py` — both reach into this subject for atoms but sit one level up so the subject-imports lint short-circuits cleanly.
