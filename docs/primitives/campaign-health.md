---
name: campaign-health
verb: query
side_effects: []
idempotent: true
idempotency_key: none
error_codes: []
backed_by:
  cli: hpc-mapreduce campaign-health [--campaign-id <id>] [--since-iso <ts>]
  python: claude_hpc.atoms.campaign_health.campaign_health
---

## Purpose

Structured campaign-health summary for an LLM agent. Aggregates
run-history signals (per-run sidecars, runtime_prior samples) into one
payload that surfaces patterns the calling agent investigates:

* walltime cliff rate by GPU type — *jobs are timing out on a100s*
* GPU utilization — *p50 elapsed is 1/3 of asked walltime, right-size*
* failure breakdown by category — *5 OOMs, recommend mem bump*

claude-hpc itself does not call an LLM. The payload includes a
`suggested_prompt` string the calling agent feeds verbatim to its model.

## Outputs

See `schemas/campaign_health.output.json`. The envelope\'s `data` block
carries:

* `n_runs`, `n_complete`, `n_failed` — campaign-level totals.
* `walltime_cliff_rate` — `{gpu_type: fraction-of-jobs-at->=95%-walltime}`.
* `failure_breakdown` — `{FailureCategory: count}`.
* `gpu_utilization` — `{gpu_type: {n_runs, p50_elapsed_sec}}`.
* `suggested_prompt` — ready-to-feed-LLM prompt summarizing the above.

## Compose with

* Predecessor: any campaign that has emitted runs. The primitive is a
  diagnostic — there\'s no required `submit-flow` predecessor.
* Successor: the calling agent feeds `suggested_prompt` to its LLM and
  re-submits with adjusted resources via [submit-flow](submit-flow.md).
