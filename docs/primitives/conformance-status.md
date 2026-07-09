---
name: conformance-status
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent conformance-status --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.conformance.status_op.conformance_status
---
# conformance-status

Report a registration's **live conformance** — how the production evidence
streaming in since sign-off compares against the REGISTERED evidence the
registration sealed. A registration is a hypothesis; production is the experiment
that never stops. This is the read-only comparator seat of live conformance
([`docs/design/live-conformance.md`](../design/live-conformance.md)): it loads
the ledger, the registration, and the sealed baseline, calls the one comparator
(`state/conformance.py::judge_window`), and returns per-key verdicts, the overall
tier, both sides' range-phrased evidence, and a deterministic brief.

The design center is **statistical process control rebuilt on attestations**: the
chart JUDGES, the operator ADJUSTS. This verb OBSERVES, JUDGES, and ROUTES — it
never actuates. A `nonconforming` window is a FINDING; it changes no registration
status, revokes nothing, and halts nothing. Every remedy is a human act above
core (re-register on fresh evidence, or revoke) — the agency boundary.

Verdicts are **DERIVED on every read** — there is no verdict store, no watermark,
nothing marked seen. Two reads over an unchanged ledger return the identical
report. The query creates and mutates nothing.

## The honest comparison

Point-in-time registered evidence (an order-statistics envelope over a fixed,
SEALED baseline) versus a ROLLING live window (different n, different regime,
autocorrelated samples) is apples-to-oranges. Core does **only comparison
arithmetic** and DISCLOSES both sides' evidence verbatim — it never fabricates a
σ, a p-value, or a confidence interval:

- **the registered side** is the sealed baseline's observed `[min, max]` per key
  plus its `n` and seal date. It never grows — live observations never widen it
  (re-baselining is re-registration, the full human bar).
- **the live side** is the window you select (`{since, until?}` or `last_n`),
  reduced per key to its own `[min, max]` + `n` + the distinct label sets.
- **the verdict** is range containment plus counting: `window_n >= min_window_n`
  and the reused well-evidenced bar `baseline_n >= 3`. A thin window or thin
  baseline never auto-verdicts — insufficient / novel / incomparable route to
  `needs_verdict` in BOTH directions, named by `tier_reason`.

## Inputs

- `registration_id` — the registration whose live conformance to report (a
  caller-authored slug keying the ledger).
- **exactly one window selection** — `last_n` (the trailing N receipts), or
  `since` (an ISO timestamp anchor) with optional `until` upper bound. `until`
  alone is refused: it only bounds a `since`-anchored window. Core never invents
  a default window.

## Outputs

- `overall` — the fold: `conforming` / `needs_verdict` / `nonconforming`.
- `keys` — one line per declared key, each dual-labelled with both sides'
  range-phrased order statistics and their ns, and the `tier_reason`
  (`within_envelope` / `outside_envelope` / `insufficient_window` /
  `thin_baseline` / `key_novelty` / `label_novelty` / `incomparable`).
- `window` / `baseline` — the two sides' evidence: the window's n, span, and
  observed label sets; the sealed baseline's n and seal date.
- `declaration_echo` — the sealed conformance declaration (keys, `min_window_n`,
  `review_horizon`) the comparison judged against.
- `render` — the deterministic code-composed markdown brief, range-phrased and
  dual-labelled, with no urgency or recommendation vocabulary.

## Refusals and disclosures

- **Absent registration, or a registration with no `conformance` declaration** —
  a loud refusal (`spec_invalid`): conformance is opt-in, and there is no sealed
  hypothesis to judge live evidence against.
- **A drifted or absent baseline artifact** — DISCLOSED in the brief and the
  report (a needs-attention finding), never a refusal. The reader verifies the
  on-disk artifact's raw sha against the sealed declaration; the append-time
  membership gate (that the artifact belongs to the dossier) is a separate job.

## The emitter contract

The live outcomes this verb judges are recorded by a CALLER-side **emitter** (see
`conformance-record`), which owns all domain I/O and reduces each observation to
an opaque `{key: scalar}` payload. Core never fetches, polls, or holds a
credential to any external system — the emitter is caller machinery, arms-length
forever.
