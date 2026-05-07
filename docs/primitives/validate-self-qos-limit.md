---
name: validate-self-qos-limit
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce validate-self-qos-limit --spec <path>
  python: claude_hpc.atoms.validate_self_qos_limit.validate_self_qos_limit
---
# validate-self-qos-limit

Pre-submission self-DOS check: compare predicted total pending jobs (existing + new array) against the QOS's MaxJobsPerUser cap. Catches the lesson-6 bug class: a user submits a large task array that hits the cap, which not only blocks the new submission but drags the user's fair-share score and stalls existing pending jobs. It's cheaper to refuse pre-submit than to discover this mid-flight.

## Inputs

- `profile` (string) — Profile key (for context; not used in the computation).
- `cluster` (string) — Cluster key (for context; not used in the computation).
- `current_user_pending_count` (integer) — Number of existing pending jobs the user has on this cluster/QOS.
- `new_array_size` (integer) — Number of tasks the new submission would add.
- `qos_max_jobs_per_user` (integer) — The QOS's `MaxJobsPerUser` cap (from `sacctmgr show qos`).
- `warn_at_pct` (float, default 0.7, range 0.0–1.0) — Warning threshold as a fraction of the cap. Default 70%: warn when (existing + new) >= 0.7 * cap, because the next normal-sized array will likely hit the limit.

## Outputs

A `ValidateSelfQosLimitResult` object with:

- `findings` (list of `ValidatorFinding` objects) — Empty list = pass (plenty of headroom). Each finding (when present) has:
  - `validator` — `"validate-self-qos-limit"`
  - `severity` — `"error"` (at or above cap) or `"warning"` (between warn threshold and cap).
  - `code` — `"qos_max_jobs_exceeded"` or `"qos_max_jobs_near_limit"`.
  - `message` — Human-readable summary.
  - `suggested_fix` — Actionable hint (split into smaller submissions, wait for clears).
  - `evidence` — Raw values (current_user_pending_count, new_array_size, predicted_total, cap, fraction_of_cap).

## Errors

None declared on the primitive. Findings carry the diagnostic code instead:

- `qos_max_jobs_exceeded` (error) — predicted total at or above the cap; submission would self-DOS and drag fair-share.
- `qos_max_jobs_near_limit` (warning) — predicted total between `cap * warn_at_pct` and the cap; surfaced for operator awareness.

## Idempotency

Pure local arithmetic — calling twice with the same inputs produces the same result.

## Notes

- **Error regime**: When `predicted_total >= cap`, a single error finding is returned. Submission is blocked; the agent must split the array or wait for existing jobs to clear.
- **Warning regime**: When `cap * warn_at_pct <= predicted_total < cap`, a single warning finding is returned. Submission proceeds but the agent is alerted; the message suggests considering a split if other campaigns might submit before these clear.
- **Safe regime**: When `predicted_total < cap * warn_at_pct`, no findings are returned (pass).
- The suggested fix for errors recommends splitting into arrays of size `<= (cap - current_pending - 1)`, ensuring at least one slot remains for the new submission.
