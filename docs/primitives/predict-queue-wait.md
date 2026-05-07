---
name: predict-queue-wait
verb: query
inputs:
- name: profile
  type: string
  description: Profile key (matches the runtime-prior pool the diurnal-MA backend
    reads).
- name: cluster
  type: string
  description: Cluster key from clusters.yaml.
- name: at_iso
  type: string
  description: Reference timestamp the forecast is for (UTC ISO-8601). null/omitted
    means "now".
- name: backend
  type: string
  description: auto picks DES when prerequisites are present; des forces DES (falls
    back if no snapshot); diurnal_ma forces the v1 baseline.
  default: auto
- name: n_replications
  type: int
  description: Number of DES passes when the DES backend runs. Higher gives a tighter
    p10/p90 ladder.
  default: 64
- name: seed
  type: int
  description: Optional seed for deterministic DES sampling.
side_effects: []
idempotent: true
idempotency_key: none
error_codes:
- code: spec_invalid
  category: spec
  retry_safe: false
backed_by:
  cli: hpc-agent predict-queue-wait --profile <p> --cluster <c> [--backend auto|des|diurnal_ma]
    [--n-replications N] [--at-iso <iso>] [--seed N]
  python: claude_hpc.forecast.queue_wait_baseline.predict_queue_wait
exit_codes:
- 0: ok
- 2: spec_invalid
---

## Purpose

Forecast queue-wait seconds for a hypothetical submit. Two backends:

* `diurnal_ma` — runtime-prior pool bucketed by hour-of-week (the v1 baseline).
* `des` — discrete-event simulator (Phase 4) running the FIFO + EASY-backfill scheduler forward against the most recent persisted `ClusterSnapshot`, sampling future arrivals per-user (non-homogeneous Poisson) and residual lifetimes per-user (Triangular over actual-over-ask). Returns the candidate's wait p10/p50/p90.
* `auto` (default) — DES when both a recent snapshot exists AND user profiles cover ≥80% of currently-running jobs' users; falls back to `diurnal_ma` otherwise. The result's `method` field reports which path won.

## Compose with

- Common predecessor: `inspect-cluster --persist` to lay down a fresh snapshot under `.hpc/cluster_history/<cluster>/`.
- Common successor: `score-submit-plan` (Phase 4f) layers the DES p50 alongside `sbatch --test-only` ETAs.

## Notes

- Cold-start (`method == "no_data"`) is signalled by `predicted_wait_sec == null`. Callers should treat null as "no answer" and either fall back to a heuristic or simply submit.
- `method == "des_no_snapshot"` means DES was requested but no `.hpc/cluster_history/<cluster>/<ts>.json` was found; the diurnal numbers are reported but the tag tells the caller why DES didn't run.
- The DES path does NOT apply the order-book `current_features` adjustment — the snapshot's utilization is already in the simulator. The diurnal path still applies it (Phase 1c behaviour preserved).
- `p10/p50/p90` are seconds; on the diurnal_ma path they are `null`. On the DES path they are the candidate's wait quantiles across `n_replications` independent passes with sampled arrivals + sampled residual lifetimes.
- Determinism: with a fixed `--seed`, DES output is bit-stable across re-runs given the same snapshot and profiles. Useful for replay-mode validation (`scripts/validate_des_predictor.py`).
