---
name: submit-speculate
verb: workflow
side_effects:
- scheduler-submit: <cluster> (speculative canary only)
- ssh: <cluster> (canary poll + log scan)
idempotent: true
idempotency_key: submit.submit.run_id
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
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent submit-speculate --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_speculate.submit_speculate
---
# submit-speculate

Run a **speculative canary** under the S1 brief's recommended defaults *while
the human is still reviewing that brief* (design §3, block interleaving). It
composes the same canary path as `submit-s2` but takes the S1 envelope's
recommended resolution as its input — the one sanctioned auto-apply, because
the human is concurrently reviewing those exact recommendations and the canary
is bounded. On a `y` with the spec unchanged, `submit-s2` finds the canary
already validated and returns near-instantly; on a nudge that changes the
spec, the stale canary simply drains and a fresh one runs. It never launches
the main array and never cancels anything.

## Inputs

- `spec` (SubmitSpeculateSpec) — the S1-resolved submit fields (recommendations
  applied), the `run_id`, and `detach` (default true).

## Outputs

`{ok, data: {stage_reached, run_id, started, watch, ...}}`. With `detach=true`
(default) returns a handle immediately (`stage_reached="detached"`); the
detached child owns the canary poll. With `detach=false` runs the canary
synchronously to `canary_verified` / `canary_failed`.

## Errors

- `spec_invalid` — malformed spec / no resolvable submit fields.
- `ssh_unreachable` / `remote_command_failed` — cluster transport during the
  canary submit or poll.
- `cluster_unknown` — the named cluster is not configured.

## Idempotency

Budget of **one** speculative canary per pending brief, enforced for free by
the canary TTL cache keyed on `(cmd_sha, version)`: an already-fresh canary
for this `cmd_sha` makes the verb a no-op-return rather than a second submit.
A nudge that changes the spec changes `cmd_sha` → cache miss → a fresh canary;
an unchanged spec keeps `cmd_sha` → the cached result is reused.

## Notes

Opt-in. Speculation touches the cluster before a greenlight by design (§3
decided policy: reads + staging + one speculative canary are permitted
pre-`y`; the main array never is). Mis-speculation is bounded — a single-task
canary that the cluster self-cleans.
