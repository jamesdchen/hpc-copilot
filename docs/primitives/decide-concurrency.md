---
name: decide-concurrency
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent decide-concurrency [--supports-async] [--remaining-jobs <remaining_jobs>]
    [--in-flight <in_flight>] [--k-cap <k_cap>]
  python: hpc_agent.meta.campaign.atoms.decide_concurrency.decide_concurrency
---
# decide-concurrency

Decide how many campaign iterations to run in flight, from observable
evidence rather than prose. Backs the campaign `concurrency` decision
point.

## Purpose

Most of the concurrency decision is a switch on facts the framework
already has:

- **Can** the strategy run async? — `classify-campaign-path`'s
  `supports_async_concurrency` (a code fact: Optuna / explicit
  `constant_liar`).
- Is there **room**? — `campaign-budget`'s `remaining` headroom minus
  what's already in flight.

So this primitive resolves the deterministic majority in code
(`decided_by="code"`, `sequential`) — async unsupported, or no headroom —
and escalates only the genuine residue: *how aggressively* to parallelize
within the computed safe bound (a risk-appetite call). The escalation
carries `max_in_flight`, so the judgement is "pick K in [1, bound]", not
reasoning from scratch.

## Output

`{decided_by, decision, max_in_flight, supports_async, reason,
candidates}`:

- `decided_by` — `code` when it resolves to `sequential`; `judgement`
  when async + headroom leave the how-aggressive choice open.
- `decision` — the resolved branch (`sequential`) on the code path; null
  on escalate.
- `max_in_flight` — `1` on the sequential code branch; the computed safe
  bound `min(k_cap, headroom)` on escalate.
- `candidates` — on escalate, `sequential` / `parallel` (the latter
  carrying `max_in_flight`).

Pure function over supplied evidence; never raises (`error_codes: []`).
