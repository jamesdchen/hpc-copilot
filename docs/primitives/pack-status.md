---
name: pack-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent pack-status --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.pack.status_op.pack_status
---
# pack-status

Report an experiment's **domain-pack state** — read-only. Given a pack name (or
omitted → every opted-in pack), `pack-status` returns, per pack:

- **the current bind** — the newest valid `pack-bind` in force, or `null` when
  the pack was never bound. A re-bind at a new manifest sha makes the old bind
  stale by construction, so the reported bind is always the one whose standards
  currently apply.
- **per-slot receipt status** — every caller-authored `receipt_bindings` slot
  reduced to one of `current` (a fresh, passed receipt), `failed` (a fresh
  receipt that reported `passed=false`), `stale` (a receipt exists but content it
  covered drifted — stale = missing by construction), or `missing` (no receipt).
  The reduction is the ONE currency definition (`state/pack_receipts.py`, routed
  through the attestation kernel), with each checked file's sha recomputed from
  disk on every call.
- **the unfillable-requirement report** — a slot the caller bound to a pack whose
  manifest `fills_slots` does not list it. **Advisory only**: `fills_slots` never
  becomes load-bearing (a requirement always originates with the caller — DP4),
  so this is an early warning that the pack does not claim it can fill the slot,
  never a gate.
- **dangling-reference findings** — an opted-in manifest that is
  missing / unreadable / sha-drifted, or a slot bound to a pack with no current
  bind.

## A query reports; it never raises

The loud refusals — `SpecInvalid` on a dangling manifest, `precondition_failed`
on an uncleared slot — live in the **mutate** verbs (`pack-bind`,
`pack-record-receipt`) and the **gate**. `pack-status` is a read: it surfaces
the exact same facts as data, so a human or agent can see a broken setup without
a submit being blocked. A dangling reference here is a `dangling` finding in the
result, not an error envelope.

## Not opted in → empty and silent

No `packs` block on `interview.json` returns an empty result with zero filesystem
probes beyond the single `interview.json` read (the D7 posture). A repo that
never opted into any pack behaves byte-identically and pays nothing.

## The result shape

Keyed by pack (the `scope-status` precedent): `{packs: {<name>: entry}}`, one
entry per reported pack. Each entry carries `bind` (or `null`), `slots`,
`unfillable`, and `dangling`. Core reports identity and counts only — it never
interprets a pack value: a slot status is a mechanical reduction, an unfillable
finding is an identity comparison against `fills_slots`, and a dangling finding
is a missing/mismatched hash. The status is derived state, recomputed on every
call; there is no cache and no served digest.
