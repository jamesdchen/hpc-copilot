---
name: reproduce-run
verb: workflow
side_effects:
- writes-sidecar: <experiment>/.hpc/runs/<repro_run_id>.json (the reproduction sidecar)
idempotent: true
idempotency_key: original_run_id
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent reproduce-run --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.reproduce_run.reproduce_run
---
# reproduce-run

The MINT half of the reproduction receipt (`docs/design/reproduction-receipt.md`
is the decision record). Given a FINISHED run, re-run it against a **pinned
identity** — same code, same params, same env — so a later `verify-reproduction`
can answer the one honest question: *did it reproduce?* The framework mints the
re-run, refuses to call a code-drifted re-run a reproduction, and hands off to
`submit-s2`; it never judges whether two numbers are "close enough" (that
tolerance and interpretation stay with the human above).

Given the original's `original_run_id` (and, optionally, an explicit
`new_run_name`), it:

1. **reads** the original's sidecar for the run-owned resolve inputs (a scope
   with no sidecar is refused — reproduce amends a RESOLVED prior);
2. **guards against drift** (both dimensions, below) — refusing a "reproduction"
   of a tree whose params OR code have moved since the original ran;
3. **re-resolves** under a NEW run_name (`<original_run_name>-repro`, code-derived
   — the LLM never authors it) reusing `revise-resolved`'s
   sidecar-reconstruction, under a **disjoint remote_path**, carrying the
   original's `scopes` verbatim, and threading `reproduction_of` so the resolve
   pierces the same-params dedup and stamps `reproduces` on the new sidecar;
4. **hands off** to `submit-s2` via `next_block` — returning in seconds. S2's
   detach-by-contract worker owns the re-canary poll; this verb NEVER runs the
   canary inline, which is what makes it safe as a curated MCP tool (the run-#8
   wedge: an agent unable to reach a blocking verb over MCP hand-ran a recovery
   against a throttled cluster).

Unlike `retarget-run`, **reproduce-run supersedes NOTHING** — a reproduction
closes nothing. The original stays valid, the thing being reproduced; the second
run is a one-directional `reproduces` provenance back-link, not a lineage
replacement (decision record, finding 2). `supersede_run` is never imported here.

## The drift guard — both dimensions

`cmd_sha` is PARAMETER identity only (#207): it hashes the materialized per-task
kwargs and deliberately excludes the executor body and `tasks.py` bytes. So a
`cmd_sha` match alone would happily "reproduce" **different code** and call the
mismatch a nondeterminism. `reproduce-run` refuses on either dimension, naming
the evidence:

- **Param drift.** It computes the CURRENT tree's `cmd_sha` for the original's
  run_name (`compute-run-id`) and compares it to the recorded one. A mismatch
  names BOTH shas + the **first differing task index** — derived from the
  sidecar's `trial_params` (the `cmd_sha` pre-image) — so the human sees exactly
  which task's params moved.
- **Code drift.** It routes `state.code_drift.detect_code_drift` over the
  recorded `executor` / `tasks_py_sha` versus the current tree's (the current
  executor is the interview's materialized command; the current `tasks_py_sha` is
  the on-disk file's hash). A drifted dimension refuses with the recorded value
  that changed.

A moved or edited tree **refuses with drift evidence** — v1 never
reconstructs-and-pretends (tree-snapshot storage is out of v1: a reconstruction's
failure modes would manufacture false reproductions).

## The disjoint remote_path (anti-contamination)

The reproduction resolves under `<original_remote_path>-repro` — a sibling root,
never nested under or a path-ancestor of the original's tree. The per-task
fallback reduce scans `record.remote_path` **recursively** for every
`metrics.json`, so a reproduction sharing the original's subtree would blend its
rows into the original's future mean (the run-#6 11-row-mean contamination
class). The `-repro` root keeps each run's recursive scan seeing only its own
rows; the derivation asserts the path is genuinely disjoint (a guard that can
fire were the convention ever changed to a nested path).

## The re-canary skip is legitimate

The reproduction's `(cmd_sha, version, cluster)` may be validated-fresh from the
original — that canary skip is CORRECT, because the tree is identical to the
original by construction. The always-canary override is deliberately not set;
the canary gate stays owned by `submit-s2`, and the main array behind the S3
greenlight.

## Outcomes

- **`repro_pending_canary`** — re-resolved against the pinned identity under a
  disjoint remote_path; `next_block` carries the `{verb: submit-s2, …}` hand-off.
  The human re-`y`s the brief through the EXISTING `append-decision` path (this
  verb produces the brief, it does not bypass the gates).
- **`resolve_blocked`** — the fresh resolve surfaced its OWN decision (an
  UNRELATED live same-params prior, or a needed scaffold). Nothing was minted and
  NOTHING was superseded; `next_block` is null (a human branch).
- **`prior_repro_exists`** — a COMPLETE reproduction of the same original already
  occupies the derived run_id. The reason directs the human to
  `verify-reproduction` (compare the existing pair) or to pass an explicit
  `new_run_name` for a fresh reproduction; `next_block` is null.

`needs_decision` is always True. The `next_block` field's presence on the Result
model is also what derives `reproduce-run` into the curated MCP catalog.
