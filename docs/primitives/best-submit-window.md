---
name: best-submit-window
verb: query
inputs:
- name: profile
  type: string
  description: Profile key (matches the runtime-prior pool the predictor reads).
- name: cluster
  type: string
  description: Cluster key from clusters.yaml.
- name: within_hours
  type: int
  description: Sweep horizon in hours; the predictor is evaluated at each hourly offset
    within (now, now + within_hours].
  default: 24
- name: top_k
  type: int
  description: Maximum candidates returned, sorted ascending by predicted_wait_sec.
  default: 5
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: internal
  category: internal
  retry_safe: false
backed_by:
  cli: hpc-mapreduce best-submit-window --profile <p> --cluster <c> [--within-hours
    N] [--top-k K]
  python: hpc_mapreduce.job.best_submit_window.best_submit_windows
exit_codes:
- 0: ok
- 3: internal
---

## Purpose

Sweep the diurnal moving-average queue-wait predictor over the next `within_hours` hours and surface the `top_k` lowest-wait submit windows. Intended as an optional pre-planning consultation in `/submit-hpc` Step 4c — the slash command can offer "your predicted wait now is 4h; in 6h the queue is significantly emptier — wait or submit now?" without forcing the agent to enumerate candidates itself.

## Compose with

- Common predecessor: a populated runtime-prior pool (the predictor needs at least `min_global_samples` populated samples or every window comes back cold).
- Common successor: `submit-flow` — the agent decides between "submit now" and the surfaced window then proceeds with normal submission.

## Notes

- Candidates with `predicted_wait_sec=None` (cold-start) are dropped from the ranking — there's no useful comparison. If every queried hour is cold, the result is an empty list and the caller falls back to "submit now" rather than synthesising a recommendation from nothing.
- The sweep starts at the next hour boundary (rounding `now` up). Hour-of-week buckets are 1h wide, so sub-hour resolution wouldn't change predictions; pinning to integer offsets keeps successive calls reproducible.
- The primitive does *not* re-rank by confidence — it ranks by predicted wait alone. Callers that prefer high-confidence windows can filter the `candidates` list on `confidence ∈ {"high", "medium"}` before consuming it.
