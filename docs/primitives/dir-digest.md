---
name: dir-digest
verb: query
side_effects:
- ssh: '<cluster> when set: one read-only bounded digest probe'
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: remote_command_failed
  category: cluster
  retry_safe: false
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent dir-digest --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.dir_digest.dir_digest
---
# dir-digest

Bounded, code-rendered digest of a directory tree — the replacement for raw
`ls`/`find` on large trees (the context-budget rule: an 800-task run's log
directory is a listing nobody should ship through an agent transcript).

Reports file/dir counts, total size, the newest N entries, a top-10
extension histogram, and (opt-in `marker_scan`) per-marker line-hit counts
across `*.log`/`*.err` files, reusing `worker-log-digest`'s `KNOWN_MARKERS`.
LOCAL by default, path-confined to the experiment dir. With `cluster` set,
the digest is computed CLUSTER-SIDE by one throttled read-only ssh probe
(scratch-confined) and only the numbers return — never a listing. Fails
open on a missing/unreadable root. The `render` field is relayed verbatim.

Origin: run #11 queue item 9 (`docs/design/notebook-audit.md`, Addendum 5).
