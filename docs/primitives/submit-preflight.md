---
name: submit-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent submit-preflight --experiment-dir <experiment_dir> --cluster <cluster>
    --profile <profile> [--campaign-id <campaign_id>] [--expected-cmd-sha <expected_cmd_sha>]
  python: hpc_agent.ops.submit_preflight.submit_preflight
---
# submit-preflight

Composite preflight primitive: fans `export-package` + `plan-throughput` +
`validate-campaign` out in parallel as one CLI call. Replaces the prose-
discipline contract where the agent had to remember to invoke all three
independently (and historically forgot `validate-campaign` — the demo class
that motivated this verb).

## Inputs / outputs

See `hpc_agent/schemas/submit_preflight.{input,output}.json`. Input requires
`experiment_dir`, `cluster`, `profile`; optional `campaign_id` +
`expected_cmd_sha` enable validate-campaign's stochastic-marker check.

## Internal composition

`asyncio.gather` over three `asyncio.create_subprocess_exec` calls to the
existing `hpc-agent` verbs. All three are independent (no shared file
writes; the cluster-side ssh in `export-package` is the long pole — running
the other two in its shadow is essentially free wall-clock).

The `fanout_strategy="sequential"` fallback runs them in series for
debugging or rare network-saturation-sensitive clusters.

## Failure semantics

A sub-call failure surfaces as `overall: "fail"` in the composite `data`
block, with the failing sub-call's verbatim envelope nested under its
`SubResult`. The composite itself still returns `ok: true` at the outer
envelope — the parallel siblings' successful work is preserved (no
partial-results loss). `overall: "warn"` surfaces when
`validate-campaign` returns `overall: "warn"` and no sub-call failed.

## Skipping sub-calls

`skip=["export-package", ...]` excludes the named sub-calls from dispatch.
The corresponding output slot is `null` (not a `SubResult` with `ok: false`)
so a re-run can target only the missing pieces.
