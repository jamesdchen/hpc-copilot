---
name: validate-self-qos-limit
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: (none — Python-only primitive)
  python: hpc_agent.ops.validate.self_qos_limit.validate_self_qos_limit
---
# validate-self-qos-limit

> **Internal primitive** — not surfaced in `capabilities --full`.
> No CLI subcommand and no MCP tool exists to invoke it, so no agent can
> call it directly — **and no composer calls it either.** See "Composers"
> below: this gate currently cannot fire in production.

Pre-submission self-DOS check: compare predicted total pending jobs (existing + new array) against the QOS's MaxJobsPerUser cap. Catches the lesson-6 bug class: a user submits a large task array that hits the cap, which not only blocks the new submission but drags the user's fair-share score and stalls existing pending jobs. It's cheaper to refuse pre-submit than to discover this mid-flight.

## Composers

**None.** This is the one validator in the family with no caller anywhere in
`src/` — it is registered and unit-tested, but nothing composes it, and it has
no CLI or MCP surface, so no production path can reach it. It is exercised only
by `tests/ops/validate/test_validate_self_qos_limit.py`.

Per the repo's own rule (*verify a guard can actually fire before classifying
it as intentional*, `docs/internals/engineering-principles.md`), that makes it
**inert**, not designed-in. It is retained rather than deleted because the
logic is sound and the bug class is real; the open decision is where it should
be wired:

- `validate-campaign`, alongside its sibling validators — but the cap and the
  current pending count are SSH-bound facts (`squeue --user`,
  `sacctmgr show qos`) and this primitive is deliberately pure, so the composer
  would have to fetch them and stay side-effect-free at the framework boundary;
- or `submit-preflight`, which already reaches the cluster.

Wiring it is a behavior change — campaigns that submit today would start
being refused at or above the cap — so it is a deliberate decision, not a
cleanup.

## Contract surface

Takes `profile` and `cluster` (context only; not used in the computation),
`current_user_pending_count` (existing pending jobs on this cluster/QOS),
`new_array_size` (tasks the new submission adds), `qos_max_jobs_per_user` (the
cap, from `sacctmgr show qos`), and `warn_at_pct` (float, default 0.7,
exclusive bounds `0.0 < x < 1.0`).

Returns a `ValidateSelfQosLimitResult` whose `findings` list is empty on pass.
A finding carries `validator` / `severity` / `code` / `message` /
`suggested_fix` / `evidence` (`current_user_pending_count`, `new_array_size`,
`predicted_total`, `cap`, `fraction_of_cap`).

## Invariants

- **Three regimes, one finding at most.** `predicted_total >= cap` → a single
  `error` finding, `qos_max_jobs_exceeded`. `cap * warn_at_pct <=
  predicted_total < cap` → a single `warning` finding,
  `qos_max_jobs_near_limit`. Below that → no findings.
- **Pure local arithmetic.** No SSH, no I/O. The caller fetches the SSH-bound
  data and passes it in — that is what keeps a composing `validate-campaign`
  side-effect-free at the framework boundary.
- **No envelope-level `error_code`.** Diagnostics ride in `findings[].code`.
- **The error fix leaves a slot free.** The suggested split size is
  `<= (cap - current_pending - 1)`, so at least one slot remains for the new
  submission.

## Coupling

- The `warn_at_pct` default of 0.7 encodes "the next normal-sized array will
  likely hit the limit" — a policy choice, not a scheduler fact.
- Whoever wires this in owns fetching `squeue --user` and
  `sacctmgr show qos`; the split keeps scheduler dialect out of this module.
- Shares the `ValidatorFinding` shape with the rest of the
  `validate-campaign` family.

## Failure modes

- **The gate does not run.** Today's dominant failure mode: the bug class it
  was written for is unguarded in production because nothing composes it.
- **Stale inputs.** The cap and pending count are point-in-time facts supplied
  by the caller; another submission between the fetch and the qsub invalidates
  the arithmetic. The gate narrows the window, it does not close it.

**Schemas:** [`validate_self_qos_limit.input.json`](../../src/hpc_agent/schemas/validate_self_qos_limit.input.json), [`validate_self_qos_limit.output.json`](../../src/hpc_agent/schemas/validate_self_qos_limit.output.json).
