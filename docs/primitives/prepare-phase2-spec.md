---
name: prepare-phase2-spec
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent prepare-phase2-spec --spec <path>
  python: hpc_agent.ops.prepare_phase2_spec.prepare_phase2_spec
---
# prepare-phase2-spec

Derive the **Phase 2** (main-array) submit-flow spec from a **Phase 1**
two-phase-canary spec, by applying three deterministic flips and
validating the result locally against `SubmitFlowSpec`.

## The two-phase canary gate

`submit.md`'s two-phase canary gate splits a guarded submit into:

- **Phase 1** — submit ONLY the canary (`canary_only: true`), then hand
  off. The main array does NOT launch yet.
- **verify-canary** — confirm the 1-task canary actually succeeded.
- **Phase 2** — launch the main array, but only on a green canary.

The Phase-2 spec is the Phase-1 spec with **exactly three changes**, and
everything else identical:

| field               | Phase 1 | Phase 2 | why                                                        |
| ------------------- | ------- | ------- | ---------------------------------------------------------- |
| `canary`            | `true`  | `false` | the canary already ran in Phase 1                          |
| `canary_only`       | `true`  | `false` | Phase 2 IS the main-array launch, not another canary gate  |
| `skip_rsync_deploy` | `false` | `true`  | Phase 1 already rsync+deployed the tree to the same target |

## Why this exists

Before this verb the worker round-tripped to **rebuild** a spec that was
99% known the moment the canary handoff fired — re-resolving fields it
already held on the Phase-1 spec just to flip three booleans. That is a
pointless rebuild between `verify-canary` and the Phase-2 submit. This
primitive collapses it to one deterministic, local transform: build
`{**phase1, canary: false, canary_only: false, skip_rsync_deploy: true}`
and validate it. No SSH, no journal reads, no cluster round-trip — the
issue calls this "schema validation".

The caller hands the returned `phase2_spec` straight to
`hpc-agent submit-flow --spec <path>` for the main-array launch.

## Inputs / outputs

Input **reuses** `hpc_agent/schemas/submit_flow.input.json` (the Phase-1
`SubmitFlowSpec` shape) via the CLI `schema_ref` — there is no separate
input schema. Output matches
`hpc_agent/schemas/prepare_phase2_spec.output.json`:

- `phase2_spec` — the derived main-array spec (the Phase-1 spec with the
  three flips applied, everything else verbatim), already validated
  against `SubmitFlowSpec`.
- `flips_applied` — the three booleans (`canary`, `canary_only`,
  `skip_rsync_deploy`), echoed back so the caller can audit exactly what
  changed.

## Validation

The derived spec is validated by constructing the `SubmitFlowSpec`
Pydantic model in-process. A failing validation (a Phase-1 spec missing a
required field, or carrying `total_tasks=0`) surfaces as a typed
`spec_invalid` envelope error — a pydantic `ValidationError` is adapted to
`errors.SpecInvalid`, the same way `ops/submit_flow.py` adapts
`ValueError`.

## Invariant (#279)

The Phase-2 spec MUST be derivable from the Phase-1 spec with **no runtime
state from canary execution** — the three flips are static and every other
field is copied verbatim. That is exactly what lets the worker skip the
rebuild round-trip.

If a future change makes any Phase-2 field depend on what the canary *did*
at runtime — e.g. dynamic resource adjustment off the canary's observed
memory or walltime — this primitive's premise breaks: the Phase-2 spec
would no longer be knowable at handoff time, and this verb must not be used
to derive it.
