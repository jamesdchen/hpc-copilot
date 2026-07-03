---
name: submit-s1
verb: workflow
side_effects:
- ssh: <cluster> (preflight probe, when run_preflight)
idempotent: true
idempotency_key: walk.experiment_dir
error_codes:
- code: spec_invalid
  category: user
  retry_safe: false
- code: ssh_unreachable
  category: network
  retry_safe: true
- code: cluster_unknown
  category: user
  retry_safe: false
backed_by:
  cli: hpc-agent submit-s1 --spec <path> [--experiment-dir <dir>]
  python: hpc_agent.ops.submit_blocks.submit_s1
---
## Purpose

Submit block **S1 — resolve** (docs/design/human-amplification-blocks.md §3).
A thin orchestrator that runs `submit-preflight` then `walk-submit-ambiguities`
to the first human decision point, digesting the result into a **brief** for the
`y`/nudge propose loop. No decision is resolved by the LLM: S1 hands back the
brief; the human greenlights or nudges.

The block's load-bearing move (§6, line 181): each ambiguity's `safe_default`
survives as a **pre-filled recommendation** inside the brief — never
auto-applied. `apply-safe-defaults` (the silent actor) is NOT called; `resolved`
carries only caller-supplied / deterministically-resolved fields.

## Inputs

A `SubmitS1Spec` JSON spec with:

- `walk` — a nested [`WalkSubmitAmbiguitiesInput`](walk-submit-ambiguities.md).
  The ambiguity walk accumulates ALL decision points in one pass (one brief, not
  twenty questions).
- `run_preflight` (default `true`) — run `submit-preflight` first and fold its
  `overall` pass/fail into the brief. Disable for a pure local resolve.
- `resolve` (optional) — a [`ResolveSubmitInputsSpec`](resolve-submit-inputs.md).
  Run ONLY when the walk is clean (no ambiguities), to chain the deterministic
  input-resolution ring to its own terminator.

## Outputs

A `SubmitBlockResult` (`block="s1"`) with `stage_reached`, `needs_decision`
(always `true` — S1 ends at a human greenlight), and a `brief`:

- `preflight` — `{overall}` when `run_preflight`.
- `resolved` — every field the walk resolved (never a safe-defaulted field).
- `ambiguities` — each unresolved field with its `safe_default` AND a
  `recommendation` mirror (the pre-filled recommendation the human greenlights).
  A `REQUIRED_CALLER_FIELDS` ambiguity (`goal` / `task_generator`) has
  `recommendation=null` — genuine judgment the human must supply.
- `resolve` — the `resolve-submit-inputs` terminator when the resolve leg ran.

`stage_reached` ∈ `needs_resolution` (walk found ambiguities) · `resolved`
(clean) · `prior_run_found` / `needs_scaffold_interview` (from the resolve leg).

## Errors

- `spec_invalid` — malformed spec.
- `ssh_unreachable` — the preflight probe failed.
- `cluster_unknown` — a cluster referenced in the walk is not in `clusters.yaml`.

## Idempotency

Idempotent on `walk.experiment_dir` — a pure resolution pass (the only side
effect is the read-only preflight probe), safe to re-run.

## Usage

```
hpc-agent submit-s1 --spec spec.json --experiment-dir <dir>
```

On `needs_resolution`, present the brief's recommendations; the human answers
`y` (accept the recommendations) or a nudge. Re-invoke with the resolved values
folded into `walk` (and a `resolve` spec) to reach a clean `resolved` terminator.
