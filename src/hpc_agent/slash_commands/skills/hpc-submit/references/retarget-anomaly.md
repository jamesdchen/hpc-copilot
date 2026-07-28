# Branch: a cluster retarget at an anomaly → `retarget-run`

Read when the human's nudge at an anomaly names a cluster RETARGET ("try
hoffman2 instead") — the SKILL's recovery trigger. That is ONE verb, not five
hand-choreographed steps.

Call `retarget-run` (MCP-direct) with `{old_run_id, patch: {cluster: <new>}}`.
The route is a function of the spec: a delta whose target field is `cluster`
at an anomaly terminator selects the retarget arm
(`block_chain.recovery_arm_verb`), exactly as a spec-changing nudge selects
`revise-resolved`. The verb:

1. re-resolves under a NEW run_name + the new cluster (re-deriving
   `job_env`/`ssh_target`/`backend`/activation);
2. SUPERSEDES the failed attempt — closing it AND its canary (a fresh
   `run_id` cleans up nothing on its own);
3. RE-CANARIES on the new cluster, returning an S2-shaped brief.

Proving run #4/#5 freelanced this as close-out → re-resolve → re-mint →
supersede → re-canary and fumbled three of the five (orphaned attempts,
dropped `job_env`); the verb sequences them in code so you don't.

Boundaries:

- `retarget-run` does NOT bypass the gates: it re-canaries (the cheap #160
  gate) but the main array stays behind the S3 greenlight — relay the
  returned brief and take the human's re-`y` through `append-decision` as
  usual.
- A same-cluster or resource-only change is a plain revision — use
  `revise-resolved` (`references/nudge-revision.md`), not `retarget-run`.
