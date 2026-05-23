---
name: campaign-health
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-agent campaign health [--experiment-dir <dir>] [--campaign-id <campaign_id>]
    [--since-iso <since_iso>] [--profile <profile>] [--cluster <cluster>]
  python: hpc_agent.atoms.campaign_health.campaign_health
---
# campaign-health

> **Internal primitive.** Diagnostic helper for debug tooling
> and ad-hoc agent calls; not composed by any workflow.

Aggregate run-history signals (per-run sidecars + `runtime_prior`
samples) into a structured health payload. Surfaces patterns
worth investigating: walltime cliff rate by GPU type, GPU
utilization, failure breakdown by category. The payload includes
a `suggested_prompt` string the calling LLM agent can feed
verbatim to its model — hpc-agent itself never calls an LLM.

## Composers

- `/campaign-hpc` slash command's "is this campaign healthy?"
  diagnostic step.
- Ad-hoc operator invocation
  (`hpc-agent campaign-health --campaign-id <id>`) when a
  human is debugging a stuck or anomalously-failing campaign.

No registered Python `composes=` references — this is a
diagnostic, never on the critical path.

## Invariants

- **Pure read.** Walks per-run sidecars under
  `<experiment>/.hpc/runs/`; optionally reads
  `runtime_prior` samples (when `profile` + `cluster` are both
  supplied). No journal mutation, no SSH.
- **`profile` + `cluster` are co-required.** Pass both or
  neither; passing one silently falls back to the per-sidecar
  path with a smaller sample pool.
- **Filter semantics**: `campaign_id=None` returns ALL runs in
  the experiment; `since_iso` is a string-comparison filter
  (works because ISO-8601 sorts lexically).

## Coupling

- The `failure_breakdown` keys are exactly
  `_shared.py:FailureCategory` (the classifier's enum) — adding
  a category there propagates through here automatically once
  the underlying classifier emits it.
- The `suggested_prompt` template is hand-crafted in this atom;
  it embeds magic numbers (e.g. "≥95% walltime"). Reformulating
  thresholds requires touching this atom AND any test that pins
  the prompt string.

## Failure modes

- Sidecars without `last_status.tasks[*].elapsed_sec` contribute
  zero to walltime/utilization stats. Cold-start campaigns
  (every run pre-v2-sidecar) report `n_runs > 0` with empty
  utilization dict — caller must not divide by zero.
- `runtime_prior` read failures (file missing, malformed) fall
  back to empty `samples` list — the structured payload still
  emits, just with thinner GPU-utilization signal.
