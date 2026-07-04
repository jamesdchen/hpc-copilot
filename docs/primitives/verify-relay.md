---
name: verify-relay
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent verify-relay --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.decision.verify_relay.verify_relay
---
## Purpose

Deterministic audit of an agent's **draft relay text** against a run's durable
records — the mechanized counterpart of conduct rule 10 ("never relay
numbers/state that don't match the journal",
`docs/design/proving-run-2-hardening.md` §6). The doctrine holds the LLM to
relaying only code-digested briefs, but the relay itself is unguarded: a
rounded number, a swapped run-id, or a stale state claim ("running" when the
journal recorded "failed") can still reach the durable record. This verb
closes that seam by having *code audit the LLM against the record* — the
inversion of an LLM-audits-LLM reviewer.

Code extracts the factual claims from the relay — numbers, run-id/job-id
tokens, lifecycle/verification state words — and diffs each against the
decision journal, run sidecar, `RunRecord`, and per-run briefs log (read
tolerantly when present; never created or written). It is a pure AUDIT: it
returns a verdict and never blocks the turn itself. The extraction bar is
useful-conservative — prefer flagging to missing — so a claim with no
comparable source value is reported as `unverifiable`, never silently passed.
Conversational numbers (line-start `N.` list markers, `~`-prefixed durations)
are filtered before counting.

## Inputs

A `VerifyRelayInput` JSON spec with:

- `run_id` (str, strict run-id shape) — the run whose durable records are the
  authority.
- `relay_text` (str) — the agent's draft outgoing message; its factual claims
  are audited.
- `block` (str, optional) — block hint (e.g. `submit-s2`) for provenance only;
  the audit reads all durable sources regardless.

## Outputs

A `VerifyRelayResult`:

- `clean` (bool) — `false` iff any mismatch was found.
- `claims_checked` (int) — factual claims evaluated (conversational numbers
  excluded).
- `mismatches` — one entry per unsupported claim: `{claim, kind, detail,
  nearest_source_value}` with `kind` ∈ `number` (contradicts every source
  number, even under decimal-truncation tolerance; nearest source value
  attached) · `state` (contradicts the recorded run state) · `run_id`
  (run/job-id-shaped token matching no authoritative identifier) ·
  `unverifiable` (no source carries any comparable value).
- `sources_consulted` — only the durable records actually found and read
  (`decision_journal`, `run_sidecar`, `run_record`, `briefs`); a run with no
  records honestly reports a short/empty list.

## Errors

- `spec_invalid` — never fired for a well-formed spec (run-id shape is
  enforced at the wire boundary); declared for registry honesty.

## Idempotency

Idempotent — a pure read-only audit. It writes nothing (not even the briefs
log, which another agent owns); re-running over the same relay and records
returns the same verdict.

## Notes

- Pairs with rule 9's provenance gate (`append-decision` refuses a divergent
  brief); rule 10 audits the outgoing message at the same human trust seam,
  which is why it lives in `ops/decision/` rather than `ops/monitor/`.
- Verdict-only by design: hook-level enforcement (blocking a failing relay)
  is a staged follow-up, out of scope for this MVP.
