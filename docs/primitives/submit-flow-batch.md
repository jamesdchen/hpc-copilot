---
name: submit-flow-batch
verb: workflow
side_effects:
- rsync: <ssh_target>:<remote_path>
- scheduler-submit: <cluster> (one qsub per spec)
- writes-journal: ~/.claude/hpc/<repo_hash>/runs/<run_id>.json (per spec)
idempotent: true
idempotency_key: (spec.run_id for spec in specs)
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: scheduler_throttled
  category: cluster
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-mapreduce submit-flow-batch --spec <path>
  python: claude_hpc.flows.submit_flow.submit_flow_batch
---

## Purpose

**Bundled `submit-flow`**. Takes a list of specs that share `(ssh_target, remote_path)` and collapses the per-spec pipeline (1 ssh probe + 1 rsync + 1 deploy + 1 qsub each) into:

* 1 ssh probe (preflight)
* 1 `rsync_push` — the codebase is identical across specs
* 1 `deploy_runtime` — the framework files are identical across specs
* N × (qsub + `submit_and_record`) — sequential, but multiplexed via the ssh `ControlMaster` socket established by the probe, so each additional qsub is ~free

This is the right shape whenever a campaign iteration or a multi-executor `/submit-hpc` produces >1 specs to the same cluster. The naïve `submit-flow`-per-spec path sends ~13×N ssh handshakes at the cluster's sshd and trips `MaxStartups` (CARC's default ratelimits at ~4 simultaneous fresh-start submissions; we've seen 11 parallel campaign submits land 2 successes + 9 SSH timeouts, with half-baked sidecars left behind).

Field-level contract: each list entry in the spec file matches `schemas/submit_flow.input.json`. The output envelope's `data.results` is a list of per-spec result records in input order; each entry has the same shape as a single `submit-flow` envelope's `data`.

## Compose with

- **Predecessors**: same as `submit-flow` ([check-preflight](check-preflight.md), [discover-executors](discover-executors.md), [score-submit-plan](score-submit-plan.md)) per spec. The caller is responsible for grouping specs by `(ssh_target, remote_path)` before calling — heterogeneous batches raise `spec_invalid`.
- **Successors**: per-spec `monitor-flow` / `aggregate-flow` invocations, the same as a regular submit. The batch only collapses the rsync + deploy fan-out; the per-run lifecycle stays one envelope per `run_id`.

## Notes

- **Per-spec idempotency is preserved.** Specs whose `run_id` is already on the journal contribute a `deduped=true` result without firing rsync/deploy. If every spec in the batch is already journaled, no ssh runs at all.
- **Single-spec callers.** `submit-flow` itself now delegates to `submit-flow-batch` with a 1-element list, so a single-spec call gets the same idempotency and ssh-backoff path. No reason to special-case N=1 at the call site.
- **Rsync excludes / `skip_preflight`.** The CLI reads these from the first spec in the list (they apply globally to the bundle). If they differ across specs in your input, surface that as a config bug — the bundle has one rsync.
- **Heterogeneous batches.** Different `(ssh_target, remote_path)` tuples in one call raise `spec_invalid`. Group by target and call once per group; that's what you want anyway, since rsync can only push to one place.
