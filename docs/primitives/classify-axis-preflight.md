---
name: classify-axis-preflight
verb: validate
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent classify-axis-preflight --experiment-dir <experiment_dir> [--run-name
    <run_name>] [--run-signature-sha <run_signature_sha>] [--root <root>] [--task-kind
    <task_kind>] [--data-axis-supplied]
  python: hpc_agent.ops.classify_axis_preflight.classify_axis_preflight
---
# classify-axis-preflight

Composite preflight at the top of every `hpc-classify-axis` invocation:
runs `discover-runs` → cache-check (`.hpc/axes.yaml` reuse) → (when no
cache hit and no caller-supplied `data_axis`) `recall` as one CLI call.
Collapses Steps 1–3 of the skill so the agent's role shrinks to one tool
call plus a branch on the returned `data`. Mirror of `status-preflight`
/ `submit-preflight`, with the conditional `recall` sub-call standing in
for `submit-preflight`'s `--cluster`-gated `check-preflight` — except the
gate here is data-driven (the cache-check result) rather than a flag.

## Inputs

See `hpc_agent/schemas/classify_axis_preflight.{input,output}.json`.
Input requires only `experiment_dir`. `run_name` + `run_signature_sha`
drive the cache-check; `root` + `task_kind` are forwarded to `recall`;
`data_axis_supplied` short-circuits `recall` for the interview / slash
path.

## Outputs

Output carries a `SubResult` per sub-call under `data.discover_runs`,
`data.cache_check`, `data.recall` — each the sub-call's verbatim
envelope plus `elapsed_sec` and an `ok` mirror. The `cache_check`
envelope's `data` carries `hit` (bool) plus the stored / current
`run_signature_sha` the hit decision compared, so the caller can read
the reusable classification straight out of `data.cache_check.envelope.data.stored`
on a hit. `data.recall` is `null` when the sub-call was skipped.

## Internal composition

Sequential. `discover-runs` and `recall` are plain `subprocess.run`
calls against the existing CLI verbs; the cache-check is an in-process
`axes.yaml` read (Step 2 of the skill is a plain file read — there is no
CLI verb for it) shaped into the same `SubResult` envelope so every
sub-call introspects uniformly.

Order is `discover-runs` first (resolves the `@register_run` functions),
then the cache-check (reads `executors.<run>`), then — only if needed —
`recall`. The `recall` sub-call is the "read the prior sub-call's output
to decide" branch: it runs only when neither `data_axis_supplied` is set
NOR the cache-check returned `hit: true`. Either condition makes
pre-filling from memory moot, so its slot is left `null`.

## Cache-check semantics

A hit requires both the resolved `run_name` and the run's current
`run_signature_sha`: `executors.<run_name>` must exist in `axes.yaml`
AND its stored `run_signature_sha` must equal the current one. Signature
drift (the user edited `run()`'s parameters), a missing entry, an absent
`axes.yaml`, or a `run_name` the skill couldn't resolve unambiguously
all report `hit: false` — never a hard error; the cache check is
advisory and a cold start is the normal case. Only a corrupt /
schema-violating `axes.yaml` flips the cache-check `ok: false` (with
`error_code: config_invalid`).

## Errors

The composite itself returns `ok: true` at the outer envelope regardless
of sub-call outcome. `overall: "pass"` iff every sub-call that ran
returned `ok: true`; any that returned `ok: false` flips
`overall: "fail"`. The failing sub-call's verbatim envelope is preserved
under `data.<subcall>.envelope` so the caller reads its `error_code`
without re-running. Sibling work is preserved on failure — a `recall`
failure doesn't lose the `discover-runs` or cache-check results. A
skipped `recall` (`null`) never contributes to `overall`.

## Idempotency

`idempotent: true`, no idempotency key. Every sub-call is a read-only
query (`discover-runs` walks `notebooks/`, the cache-check reads
`axes.yaml`, `recall` walks `interview.json` under `--root`); none reach
the cluster, so `requires_ssh` is `False`. Re-running is free and
side-effect-free.

## Notes

The agent's prose-discipline at the top of `hpc-classify-axis` used to be
three separate steps — "Step 1: discover-runs. Step 2: read axes.yaml.
Step 3: recall." Folding them into one verb makes a step omission
structurally impossible and hands the skill a single `data` block to
branch on: a cache hit returns early, a recall hit pre-fills
`classified_by: "recall"`, and a clean miss falls through to the
classifier.
