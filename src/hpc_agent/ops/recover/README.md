# ops/recover/

## What and why

`ops/recover/` owns the "what went wrong, how do we recover" loop after a
run produces failed tasks. It classifies each per-task failure against a
shared signature catalog, clusters the failures by stderr fingerprint so
forty tasks with the same root cause surface as a single cluster, decides
the per-cluster retry policy from a sidecar-or-default auto-retry config,
and batches the resulting resubmit into compact scheduler array
expressions that honour the cluster's `max_array_size` /
`max_concurrent_jobs` limits.

## Invariant

`ops/recover/` promises: typed failure list in → retry-batched resubmit
plan plus per-cluster retry advice out; never makes a retry decision the
signature catalog or the configured auto-retry policy doesn't justify.

## Public vs internal

All six modules are agent-facing primitive modules:

- `failures_atom.py` — the `failures` query primitive (`fetch_failures`):
  re-polls run status, fetches stderr tails, returns the clustered failure
  rollup with retry advice attached.
- `runner_failures.py` — the orchestration that wraps
  `infra.parsing.categorize_failure` with the exit-code 130/143 →
  `preempted` override, layers the richer signature-catalog `classify()`
  result onto every cluster, and computes per-cluster retry advice
  (`cluster_failures_by_fingerprint`, `annotate_clusters_with_retry_advice`,
  `fingerprint_stderr_tail`, `DEFAULT_AUTO_RETRY_POLICY`). Also re-exports
  `_FAILURE_CATEGORY_PATTERNS` / `_categorize` from `infra.parsing` for
  back-compat with cross-subject contract tests.
- `failure_signatures.py` — the VASPilot-style signature catalog
  (`CATALOG`, `FailureSignature`, `classify`) that returns
  `{error_class, suggested_fix, matched_pattern}` per failure so callers
  can auto-resubmit with adjusted resources rather than asking the user.
- `runner.py` — the `resubmit-failed` mutate primitive
  (`resubmit_failed`, `derive_resubmit_request_id`): records a
  resubmission attempt in the journal, deduping on `request_id`.
- `batching.py` — pure, no-IO planner (`compact_task_ids`,
  `ResubmitBatch`, `ResubmitPlan`, `resubmit_plan`) that packs failed
  task IDs into compact `sbatch`/`qsub` array expressions and splits
  them into batches per cluster constraints. Retains its `# @pure: no-io`
  header (enforced by `scripts/lint_pure_files.py`).

The `resubmit_flow()` helper (a plain role-root composite, **not** a
registered `@primitive`) lives at `ops/recover_flow.py` (role-root
sibling per P5a) — it composes the atoms above plus preempted-detection
and the cluster-side qsub-per-batch loop. The registered recovery
primitive callers reach by name is `resubmit-failed` (`runner.py`).

No internal-only files in this subject.
